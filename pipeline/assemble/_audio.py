"""Audio processing: BPM detection, beat sync, speech tracks, music mixing, chapters."""

from __future__ import annotations

import bisect
import json
import logging
import math
import struct as _struct
import wave
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .. import constants as C
from ..edl import EDL
from ..utils.media import run_subprocess

logger = logging.getLogger("reelsmith.assemble.audio")


@dataclass(frozen=True)
class CueSheetRenderInfo:
    """Render-level metadata embedded into cue sheets for traceability."""

    output_fps: float | None = None
    output_file: str | None = None
    output_width: int | None = None
    output_height: int | None = None
    edl_version: int | None = None


# ---------------------------------------------------------------------------
# BPM estimation & beat sync
# ---------------------------------------------------------------------------


def estimate_bpm(wav_path: Path, min_bpm: int = 60, max_bpm: int = 180) -> int | None:
    """Estimate BPM from WAV using energy envelope autocorrelation. Stdlib only."""
    try:
        with wave.open(str(wav_path)) as w:
            sample_rate = w.getframerate()
            num_channels = w.getnchannels()
            sample_width = w.getsampwidth()
            n_frames = w.getnframes()
            max_frames = min(n_frames, sample_rate * 30)
            raw = w.readframes(max_frames)
    except (OSError, wave.Error) as e:
        logger.warning("BPM estimation: could not read %s: %s", wav_path, e)
        return None

    n_samples = len(raw) // sample_width
    if sample_width == 2:
        samples = _struct.unpack(f"<{n_samples}h", raw)
    elif sample_width == 4:
        samples = _struct.unpack(f"<{n_samples}i", raw)
    else:
        return None

    if num_channels == 2:
        samples = [
            (samples[i] + samples[i + 1]) / 2 for i in range(0, n_samples - 1, 2)
        ]
    elif num_channels > 2:
        return None

    if len(samples) < sample_rate * 2:
        return None

    win = sample_rate // (1000 // C.ENERGY_WINDOW_MS)
    energy = []
    for i in range(0, len(samples), win):
        chunk = samples[i : i + win]
        if chunk:
            energy.append(math.sqrt(sum(s * s for s in chunk) / len(chunk)))

    if len(energy) < C.MIN_ENERGY_WINDOWS:
        return None

    windows_per_sec = 1000 // C.ENERGY_WINDOW_MS
    min_lag = int(60 / max_bpm * windows_per_sec)
    max_lag = int(60 / min_bpm * windows_per_sec)
    max_lag = min(max_lag, len(energy) // 2)

    if min_lag >= max_lag:
        return None

    mean_e = sum(energy) / len(energy)
    best_lag = min_lag
    best_corr = -1.0

    for lag in range(min_lag, max_lag):
        corr = sum(
            (energy[i] - mean_e) * (energy[i + lag] - mean_e)
            for i in range(len(energy) - lag)
        )
        if corr > best_corr:
            best_corr = corr
            best_lag = lag

    bpm = round(60 / (best_lag / windows_per_sec))
    return bpm


def _build_beat_grid(music_path: Path) -> tuple[list[float], int] | None:
    """Build a half-beat timestamp grid from a music file.

    Returns (beats, bpm) or None if BPM cannot be estimated.
    """
    bpm = estimate_bpm(music_path)
    if not bpm:
        return None

    beat_interval = 60.0 / bpm
    half_beat = beat_interval / 2
    beats = [i * half_beat for i in range(int(3600 / half_beat) + 1)]
    return beats, bpm


def _snap_transitions(edl: EDL, beats: list[float]) -> tuple[int, int]:
    """Snap EDL transition points to the nearest beat in *beats*.

    Modifies edl items' display_duration in place.

    Returns (snapped_count, total_transitions_count).
    """
    offset = 0.0
    offset += edl.intro_duration

    snapped = 0
    total_transitions = 0
    n_segments = len(edl.segments)

    for seg_idx, seg in enumerate(edl.segments):
        seg_max_shift = (
            C.MONTAGE_MAX_SHIFT if seg.mode == "montage" else C.MAX_BEAT_SHIFT
        )

        for i, item in enumerate(seg.items):
            offset += item.display_duration

            # Is this a transition point?
            is_intra = i < len(seg.items) - 1
            is_boundary = i == len(seg.items) - 1 and seg_idx < n_segments - 1

            if not is_intra and not is_boundary:
                continue  # last item of last segment — no transition after it

            total_transitions += 1

            # #4: Only skip if THIS item has keep_audio — its duration is
            # anchored to speech timing and must not change.
            if item.keep_audio:
                continue

            idx = bisect.bisect_left(beats, offset)
            candidates = []
            if idx > 0:
                candidates.append(beats[idx - 1])
            if idx < len(beats):
                candidates.append(beats[idx])

            if not candidates:
                continue

            nearest = min(candidates, key=lambda b: abs(b - offset))
            shift = nearest - offset

            if abs(shift) > seg_max_shift:
                continue

            min_dur = (
                C.MIN_PHOTO_DURATION
                if item.media_type == "photo"
                else C.MIN_VIDEO_DURATION
            )
            new_dur = item.display_duration + shift
            if new_dur < min_dur:
                continue

            item.display_duration = round(new_dur, C.BEAT_SNAP_PRECISION)
            offset = offset + shift
            snapped += 1

    return snapped, total_transitions


def beat_snap_edl(edl: EDL, music_path: Path) -> int:
    """Snap EDL transition points to music beats. Modifies edl in place.

    Improvements over naive approach:
    - #5: Segment boundaries are snapped (not just intra-segment transitions).
    - #4: Speech skip is per-item, not per-segment. Only the item whose duration
      would change is checked — if it has keep_audio=true, that transition is
      skipped, but other transitions in the same segment are still eligible.

    Returns the number of transitions snapped.
    """
    result = _build_beat_grid(music_path)
    if result is None:
        logger.info("Beat sync: could not estimate BPM, skipping")
        return 0
    beats, bpm = result
    logger.info("Beat sync: detected ~%d BPM", bpm)
    snapped, total_transitions = _snap_transitions(edl, beats)
    if total_transitions > 0:
        pct = int(snapped / total_transitions * 100)
        logger.info(
            "Beat sync: snapped %d/%d transitions (%d%%) to %d BPM grid",
            snapped,
            total_transitions,
            pct,
            bpm,
        )
    return snapped


def _segment_boundaries(edl: EDL, segment_durations: list[float]) -> list[float]:
    """Final-video time boundaries of each segment's *item region*, in seconds.

    Returns ``len(segments) + 1`` offsets: ``boundaries[i]`` is where segment
    *i*'s items begin in the final video, and the last entry is where the last
    segment's items end.

    Critical: the rendered segment FILES embed the title cards — the intro card
    is prepended to the first segment and the outro card appended to the last —
    so their probed durations *include* those cards. We strip them here (the
    intro becomes the start offset; the outro is dropped from the tail) so the
    boundaries track item regions, not file spans. Without this the cards are
    double-counted and every boundary after the first drifts late by
    ``intro_duration`` (≈ the whole timeline is off by intro + outro).

    Falls back to the sum of a segment's item ``display_duration`` when a probed
    value is unavailable — display sums already exclude the cards, so no
    stripping is applied on that path.

    Single source of truth shared by ``write_chapters`` (per-segment) and
    ``write_cue_sheet`` (per-item), so the two artifacts agree exactly.
    """
    n = len(edl.segments)
    boundaries: list[float] = []
    offset = edl.intro_duration  # the first segment's items begin after the intro
    for seg_idx, seg in enumerate(edl.segments):
        boundaries.append(offset)
        if seg_idx < len(segment_durations):
            dur = segment_durations[seg_idx]
            if seg_idx == 0:
                dur -= edl.intro_duration  # already counted in the start offset
            if seg_idx == n - 1:
                dur -= edl.outro_duration  # outro card is not an item
            dur = max(0.0, dur)
        else:
            dur = sum(it.display_duration for it in seg.items)
        offset += dur
    boundaries.append(offset)
    return boundaries


def write_chapters(edl: EDL, segment_durations: list[float], out_path: Path) -> None:
    """Write YouTube-compatible chapter markers from EDL segments.

    segment_durations: probed duration of each rendered segment file.
    """
    boundaries = _segment_boundaries(edl, segment_durations)
    lines = []
    for seg_idx, seg in enumerate(edl.segments):
        offset = boundaries[seg_idx]
        minutes = int(offset) // 60
        seconds = int(offset) % 60
        lines.append(f"{minutes}:{seconds:02d} {seg.name}")

    out_path.write_text("\n".join(lines))
    logger.info("YouTube chapters: %s (%d chapters)", out_path.name, len(lines))


def _round_time(value: float | None) -> float | None:
    return round(value, 3) if value is not None else None


def _frame_at_time(value: float | None, fps: float | None) -> int | None:
    if value is None or fps is None or fps <= 0:
        return None
    return int(round(value * fps))


def _parse_rate(rate: str | None) -> float | None:
    if not rate or rate == "0/0":
        return None
    try:
        return float(Fraction(rate))
    except (ValueError, ZeroDivisionError):
        return None


def _source_media_metadata(path: str) -> dict:
    src = Path(path)
    meta: dict[str, object] = {
        "exists": src.exists(),
        "size_bytes": None,
        "mtime_ns": None,
        "fps": None,
        "fps_raw": None,
        "time_base": None,
        "duration": None,
        "nb_frames": None,
        "width": None,
        "height": None,
    }
    if not src.exists():
        return meta

    stat = src.stat()
    meta["size_bytes"] = stat.st_size
    meta["mtime_ns"] = stat.st_mtime_ns

    result = run_subprocess(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=avg_frame_rate,r_frame_rate,time_base,duration,nb_frames,width,height",
            "-of",
            "json",
            str(src),
        ],
        capture_output=True,
        text=True,
    )
    try:
        stream = json.loads(result.stdout)["streams"][0]
    except (json.JSONDecodeError, KeyError, IndexError):
        return meta

    fps_raw = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
    fps = _parse_rate(fps_raw)
    meta["fps"] = round(fps, 6) if fps is not None else None
    meta["fps_raw"] = fps_raw
    meta["time_base"] = stream.get("time_base")
    try:
        meta["duration"] = round(float(stream["duration"]), 6)
    except (KeyError, TypeError, ValueError):
        pass
    try:
        meta["nb_frames"] = int(stream["nb_frames"])
    except (KeyError, TypeError, ValueError):
        pass
    for key in ("width", "height"):
        try:
            meta[key] = int(stream[key])
        except (KeyError, TypeError, ValueError):
            pass
    return meta


def write_cue_sheet(
    edl: EDL,
    segment_durations: list[float],
    segment_item_durations: list[list[float]],
    out_path: Path,
    render_info: CueSheetRenderInfo | None = None,
) -> None:
    """Write a per-item timeline manifest mapping final-video seconds → source files.

    Unlike YouTube chapters (per-segment), this records every item's exact
    [record_in, record_out) window in the *final* video, so any timestamp can be
    mapped back to its source clip + trim points. Coverage is contiguous: each
    item's record_out is the next item's record_in, and the last item of each
    segment is anchored to the probed segment boundary, absorbing sub-frame
    container rounding.

    segment_durations: probed duration of each rendered segment FILE (ground
    truth for the segment boundaries; intro/outro cards stripped inside
    ``_segment_boundaries``).

    segment_item_durations: per-segment list of each item's *rendered* duration
    in the final video, as computed and exported by the renderer
    (``build_segment_graph`` via ``item_render_seconds``). This is the single
    source of truth — emphatically NOT ``item.display_duration``, which
    beat_snap_edl rewrites and the video renderer ignores. Using it keeps
    per-item boundaries frame-accurate instead of drifting by the beat-snap
    delta.

    render_info: optional render metadata. When output_fps is provided, the cue
    sheet also records half-open final-frame ranges. Existing seconds fields
    remain the primary human-readable timeline.
    """
    boundaries = _segment_boundaries(edl, segment_durations)
    items: list[dict] = []
    global_idx = 0
    output_fps = render_info.output_fps if render_info else None
    source_meta_cache: dict[str, dict] = {}

    for seg_idx, seg in enumerate(edl.segments):
        seg_start = boundaries[seg_idx]
        seg_end = boundaries[seg_idx + 1]
        # Renderer-exported per-item durations (ground truth). Fall back to
        # display_duration per item only when a value is unavailable (e.g. an
        # older call site that didn't thread them through) so the manifest stays
        # well-formed instead of crashing.
        durs = (
            segment_item_durations[seg_idx]
            if seg_idx < len(segment_item_durations)
            else []
        )

        local = seg_start
        last = len(seg.items) - 1
        for i, item in enumerate(seg.items):
            dur = durs[i] if i < len(durs) else item.display_duration
            record_out = seg_end if i == last else local + dur
            record_duration = record_out - local
            if item.source_file not in source_meta_cache:
                source_meta_cache[item.source_file] = _source_media_metadata(
                    item.source_file
                )
            source_meta = source_meta_cache[item.source_file]
            source_fps = source_meta.get("fps")
            source_fps_float = (
                float(source_fps) if isinstance(source_fps, (int, float)) else None
            )
            source_time_in = None
            source_time_out = None
            if item.media_type == "video":
                source_time_in = item.start_time or 0.0
                source_time_out = source_time_in + (
                    record_duration * item.playback_speed
                )
            items.append(
                {
                    "index": global_idx,
                    "segment_index": seg_idx,
                    "segment_name": seg.name,
                    "record_in": round(local, 3),
                    "record_out": round(record_out, 3),
                    "record_duration": round(record_duration, 3),
                    "record_frame_in": _frame_at_time(local, output_fps),
                    "record_frame_out": _frame_at_time(record_out, output_fps),
                    "source_file": item.source_file,
                    "media_type": item.media_type,
                    "trim_start": item.start_time,
                    "trim_end": item.end_time,
                    "source_time_in": _round_time(source_time_in),
                    "source_time_out": _round_time(source_time_out),
                    "source_fps": source_fps,
                    "source_frame_in": _frame_at_time(source_time_in, source_fps_float),
                    "source_frame_out": _frame_at_time(
                        source_time_out, source_fps_float
                    ),
                    "source_metadata": source_meta,
                    "playback_speed": item.playback_speed,
                    "keep_audio": item.keep_audio,
                    "render_duration": round(dur, 3),
                    "text_overlay": item.text_overlay.text
                    if item.text_overlay
                    else None,
                }
            )
            local += dur
            global_idx += 1

    data = {
        "schema_version": 2,
        "title": edl.title,
        "intro_duration": edl.intro_duration,
        "outro_duration": edl.outro_duration,
        "total_duration": round(boundaries[-1] + edl.outro_duration, 3),
        "render": {
            "output_file": render_info.output_file if render_info else None,
            "output_fps": output_fps,
            "output_width": render_info.output_width if render_info else None,
            "output_height": render_info.output_height if render_info else None,
            "edl_version": render_info.edl_version if render_info else None,
        },
        "items": items,
    }
    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    logger.info("Cue sheet: %s (%d items)", out_path.name, len(items))
