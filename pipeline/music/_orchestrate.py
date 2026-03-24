"""Music generation for vlogs — Gemini Lyria RealTime backend.

Generates per-segment music based on EDL music_mood descriptions,
then crossfades them into a single composite track.

Falls back gracefully if API unavailable — vlog renders without music.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("vlog.music")


def generate_music(
    trip_type: str,
    style: str,
    target_duration: int,
    cache_dir: Path,
    mood: str = "",
) -> Path | None:
    """Generate background music via Gemini Lyria RealTime API."""
    from ._gemini import generate_music_gemini

    return generate_music_gemini(
        trip_type=trip_type,
        style=style,
        target_duration=target_duration,
        cache_dir=cache_dir,
        mood=mood,
    )


def _segment_duration(seg) -> float:
    """Calculate a segment's screen time from its items and transitions."""
    total = sum(item.display_duration for item in seg.items)
    if seg.transition != "cut" and len(seg.items) > 1:
        total -= (len(seg.items) - 1) * seg.transition_duration
    return max(total, 5)  # at least 5s per segment


def _build_composite_music(
    segment_tracks: list[tuple[float, Path]],
    output_path: Path,
    crossfade: float = 2.0,
) -> bool:
    """Build composite music from per-segment tracks with crossfades.

    segment_tracks: [(segment_duration, music_wav_path), ...]
    Returns True on success.
    """
    from ..media_utils import run_subprocess

    if not segment_tracks:
        return False

    if len(segment_tracks) == 1:
        # Single segment — just copy
        import shutil

        shutil.copy(str(segment_tracks[0][1]), str(output_path))
        return True

    # Trim each segment's music to its duration + crossfade overlap, then chain acrossfade
    trimmed: list[Path] = []
    inputs: list[str] = []
    for i, (dur, track) in enumerate(segment_tracks):
        trim_dur = dur + (crossfade if i < len(segment_tracks) - 1 else 0)
        trimmed_path = output_path.parent / f"_seg_music_{i}.wav"
        result = run_subprocess(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(track),
                "-t",
                str(trim_dur),
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
            logger.warning(
                "Trim failed for segment %d: %s", i, (result.stderr or "")
            )
            continue
        if trimmed_path.exists():
            trimmed.append(trimmed_path)
            inputs += ["-i", str(trimmed_path)]

    if len(trimmed) < 2:
        if trimmed:
            import shutil

            shutil.copy(str(trimmed[0]), str(output_path))
            for t in trimmed:
                t.unlink(missing_ok=True)
            return True
        return False

    # Chain acrossfade filters
    filter_parts = []
    for i in range(1, len(trimmed)):
        in_label = "[0:a]" if i == 1 else f"[a{i-1}]"
        out_label = f"[a{i}]" if i < len(trimmed) - 1 else "[out]"
        filter_parts.append(
            f"{in_label}[{i}:a]acrossfade=d={crossfade}:c1=tri:c2=tri{out_label}"
        )

    cmd = (
        ["ffmpeg", "-y"]
        + inputs
        + [
            "-filter_complex",
            ";".join(filter_parts),
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


def generate_music_for_edl(cfg, *, progress_callback=None) -> Path | None:
    """Generate per-segment music and build a composite track with crossfades.

    Called by the generate_music stage. Generates one Lyria track per
    segment based on its music_mood, then crossfades them into one file.

    Returns the composite music file path, or None if skipped/failed.
    """
    from ..edl import MusicTrack, load_latest_edl, save_edl

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
        seg_dur = int(_segment_duration(seg))
        mood = seg.music_mood or f"{edl.style} travel vlog background music"
        logger.info(
            '  Segment %d/%d: "%s" (%ds)', i + 1, len(edl.segments), seg.name, seg_dur
        )
        logger.info("    Mood: %s", mood)

        track = generate_music(
            trip_type=edl.trip_type,
            style=edl.style,
            target_duration=seg_dur,
            cache_dir=music_cache,
            mood=mood,
        )
        if track:
            segment_tracks.append((seg_dur, track))
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
    if not _build_composite_music(segment_tracks, composite_path, crossfade=2.0):
        # Fallback: use first segment's track
        logger.warning("Composite build failed, using first segment track")
        composite_path = segment_tracks[0][1]

    edl.music = MusicTrack(file=str(composite_path))
    save_edl(cfg, edl, version)
    logger.info("Per-segment music saved to EDL v%d: %s", version, composite_path)

    return composite_path
