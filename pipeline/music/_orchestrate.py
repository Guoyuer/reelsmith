"""Music generation for vlogs — Gemini Lyria RealTime backend.

Generates per-segment music based on EDL music_mood descriptions,
then crossfades them into a single composite track.

Falls back gracefully if API unavailable — vlog renders without music.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import ProgressCallback

logger = logging.getLogger("vlog.music")

_MIN_SEGMENT_DURATION = 5  # seconds; floor for per-segment music
_DEFAULT_CROSSFADE = 2.0  # seconds; crossfade between segments


def _segment_duration(seg) -> float:
    """Calculate a segment's screen time from its items and transitions."""
    total = sum(item.display_duration for item in seg.items)
    if seg.transition != "cut" and len(seg.items) > 1:
        total -= (len(seg.items) - 1) * seg.transition_duration
    return max(
        total, _MIN_SEGMENT_DURATION
    )  # at least _MIN_SEGMENT_DURATION per segment


def _trim_music_segments(
    segment_tracks: list[tuple[float, Path]],
    output_dir: Path,
    crossfade: float,
) -> list[Path]:
    """Trim each segment's music to its duration + crossfade overlap.

    Returns a list of trimmed file paths (only those that were successfully created).
    """
    from ..utils.media import run_subprocess

    trimmed: list[Path] = []
    for i, (duration, track) in enumerate(segment_tracks):
        trim_duration = duration + (crossfade if i < len(segment_tracks) - 1 else 0)
        trimmed_path = output_dir / f"_seg_music_{i}.wav"
        result = run_subprocess(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(track),
                "-t",
                str(trim_duration),
                "-c:a",
                "pcm_s16le",
                "-ar",
                "48000",
                "-ac",
                "2",
                str(trimmed_path),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.warning("Trim failed for segment %d: %s", i, (result.stderr or ""))
            continue
        if trimmed_path.exists():
            trimmed.append(trimmed_path)

    return trimmed


def _build_crossfade_filter(n_tracks: int, crossfade: float) -> str:
    """Build the acrossfade filter chain string for *n_tracks* inputs.

    Returns the filter string (semicolon-joined acrossfade steps).
    """
    filter_parts = []
    for i in range(1, n_tracks):
        in_label = "[0:a]" if i == 1 else f"[a{i - 1}]"
        out_label = f"[a{i}]" if i < n_tracks - 1 else "[out]"
        filter_parts.append(
            f"{in_label}[{i}:a]acrossfade=d={crossfade}:c1=tri:c2=tri{out_label}"
        )
    return ";".join(filter_parts)


def _build_composite_music(
    segment_tracks: list[tuple[float, Path]],
    output_path: Path,
    crossfade: float,
) -> bool:
    """Build composite music from per-segment tracks with crossfades.

    segment_tracks: [(segment_duration, music_wav_path), ...]
    Returns True on success.
    """
    from ..utils.media import run_subprocess

    if not segment_tracks:
        return False

    if len(segment_tracks) == 1:
        # Single segment — just copy
        import shutil

        shutil.copy(str(segment_tracks[0][1]), str(output_path))
        return True

    # Trim each segment's music to its duration + crossfade overlap, then chain acrossfade
    trimmed = _trim_music_segments(segment_tracks, output_path.parent, crossfade)

    if len(trimmed) < 2:
        if trimmed:
            import shutil

            shutil.copy(str(trimmed[0]), str(output_path))
            for t in trimmed:
                t.unlink(missing_ok=True)
            return True
        return False

    inputs: list[str] = []
    for t in trimmed:
        inputs += ["-i", str(t)]

    # Chain acrossfade filters
    filter_str = _build_crossfade_filter(len(trimmed), crossfade)

    cmd = (
        ["ffmpeg", "-y"]
        + inputs
        + [
            "-filter_complex",
            filter_str,
            "-map",
            "[out]",
            "-c:a",
            "pcm_s16le",
            "-ar",
            "48000",
            "-ac",
            "2",
            str(output_path),
        ]
    )
    result = run_subprocess(cmd, capture_output=True, text=True)

    # Cleanup trimmed files
    for t in trimmed:
        t.unlink(missing_ok=True)

    if result.returncode != 0:
        logger.error("Composite music build failed: %s", result.stderr)
        return False

    logger.info(
        "Composite music: %d segments crossfaded into %s",
        len(segment_tracks),
        output_path.name,
    )
    return True


def generate_music_for_edl(
    cfg, *, edl_version: int | None = None, progress_callback: ProgressCallback = None
) -> Path | None:
    """Generate per-segment music and build a composite track with crossfades.

    Called by the generate_music stage. Generates one Lyria track per
    segment based on its music_mood, then crossfades them into one file.

    *edl_version*: specific EDL version to use (default: latest).

    Returns the composite music file path, or None if skipped/failed.
    """
    from ..edl import EDL, MusicTrack, load_latest_edl, save_edl

    if edl_version is not None:
        edl = EDL.model_validate_json(cfg.edl_path(edl_version).read_text())
        version = edl_version
    else:
        edl, version = load_latest_edl(cfg)

    if edl.music_mode != "auto":
        logger.info("Music mode is '%s', skipping generation", edl.music_mode)
        return None

    if edl.music and Path(edl.music.file).exists():
        logger.info("Music file already exists: %s", edl.music.file)
        return Path(edl.music.file)

    music_cache = cfg.music_dir

    # Generate per-segment music tracks
    logger.info("Generating per-segment music: %d segments", len(edl.segments))
    segment_tracks: list[tuple[float, Path]] = []

    for i, seg in enumerate(edl.segments):
        segment_duration = int(_segment_duration(seg))
        mood = seg.music_mood or f"{edl.style} travel vlog background music"
        logger.info(
            '  Segment %d/%d: "%s" (%ds)',
            i + 1,
            len(edl.segments),
            seg.name,
            segment_duration,
        )
        logger.info("    Mood: %s", mood)

        from ._gemini import generate_music_gemini

        track = generate_music_gemini(
            trip_type=edl.trip_type,
            style=edl.style,
            target_duration=segment_duration,
            cache_dir=music_cache,
            mood=mood,
        )
        if track:
            segment_tracks.append((segment_duration, track))
            logger.info("    Generated: %s", track.name)
        else:
            logger.warning("    FAILED — segment will be silent")
        if progress_callback:
            progress_callback(0, 0, f"{seg.name} ({i + 1}/{len(edl.segments)})")

    if not segment_tracks:
        logger.warning("No music generated for any segment")
        return None

    # Build composite with crossfades
    music_cache.mkdir(parents=True, exist_ok=True)
    composite_path = (
        music_cache
        / f"composite_{edl.trip_type}_{edl.style}_{int(edl.estimated_duration())}s.wav"
    )
    if not _build_composite_music(
        segment_tracks, composite_path, crossfade=_DEFAULT_CROSSFADE
    ):
        # Fallback: use first segment's track
        logger.warning("Composite build failed, using first segment track")
        composite_path = segment_tracks[0][1]

    edl.music = MusicTrack(file=str(composite_path))
    save_edl(cfg, edl, version)
    logger.info("Per-segment music saved to EDL v%d: %s", version, composite_path)

    return composite_path
