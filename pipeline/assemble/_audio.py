"""Audio processing: BPM detection, beat sync, speech tracks, music mixing, chapters."""

from __future__ import annotations

import bisect
import json
import logging
import math
import struct as _struct
import wave
from pathlib import Path

from .. import constants as C
from ..edl import EDL

logger = logging.getLogger("reelsmith.assemble.audio")


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
    """Final-video time boundaries of each segment, in seconds.

    Returns ``len(segments) + 1`` offsets: ``boundaries[i]`` is segment *i*'s
    start (intro + cumulative length of preceding segments), and the final
    entry is the end of the last segment (before any outro). Segment lengths
    come from the probed rendered durations, falling back to the sum of the
    segment's item ``display_duration`` when a probed value is unavailable.

    This is the single source of truth for the timeline shared by
    ``write_chapters`` (per-segment) and ``write_cue_sheet`` (per-item), so the
    two artifacts agree on segment boundaries exactly.
    """
    boundaries: list[float] = []
    offset = edl.intro_duration
    for seg_idx, seg in enumerate(edl.segments):
        boundaries.append(offset)
        if seg_idx < len(segment_durations):
            offset += segment_durations[seg_idx]
        else:
            offset += sum(it.display_duration for it in seg.items)
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


def write_cue_sheet(edl: EDL, segment_durations: list[float], out_path: Path) -> None:
    """Write a per-item timeline manifest mapping final-video seconds → source files.

    Unlike YouTube chapters (per-segment), this records every item's exact
    [record_in, record_out) window in the *final* video, so any timestamp can be
    mapped back to its source clip + trim points. Coverage is contiguous: each
    item's record_out is the next item's record_in, and the last item of each
    segment is anchored to the probed segment boundary (matches the chapters
    file exactly, absorbing any rounding drift).

    Assumes beat sync already ran — item.display_duration values are final.

    segment_durations: probed duration of each rendered segment file (ground
    truth for segment boundaries).
    """
    boundaries = _segment_boundaries(edl, segment_durations)
    items: list[dict] = []
    global_idx = 0

    for seg_idx, seg in enumerate(edl.segments):
        seg_start = boundaries[seg_idx]
        seg_end = boundaries[seg_idx + 1]

        local = seg_start
        last = len(seg.items) - 1
        for i, item in enumerate(seg.items):
            record_out = seg_end if i == last else local + item.display_duration
            items.append(
                {
                    "index": global_idx,
                    "segment_index": seg_idx,
                    "segment_name": seg.name,
                    "record_in": round(local, 3),
                    "record_out": round(record_out, 3),
                    "source_file": item.source_file,
                    "media_type": item.media_type,
                    "trim_start": item.start_time,
                    "trim_end": item.end_time,
                    "playback_speed": item.playback_speed,
                    "keep_audio": item.keep_audio,
                    "display_duration": item.display_duration,
                    "text_overlay": item.text_overlay.text
                    if item.text_overlay
                    else None,
                }
            )
            local += item.display_duration
            global_idx += 1

    data = {
        "title": edl.title,
        "intro_duration": edl.intro_duration,
        "outro_duration": edl.outro_duration,
        "total_duration": round(boundaries[-1] + edl.outro_duration, 3),
        "items": items,
    }
    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    logger.info("Cue sheet: %s (%d items)", out_path.name, len(items))
