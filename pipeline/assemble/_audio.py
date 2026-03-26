"""Audio processing: BPM detection, beat sync, speech tracks, music mixing, chapters."""

from __future__ import annotations

import logging
import math
import struct as _struct
import wave
from pathlib import Path

from ..edl import EDL

logger = logging.getLogger("vlog.assemble.audio")


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

    win = sample_rate // 100
    energy = []
    for i in range(0, len(samples), win):
        chunk = samples[i : i + win]
        if chunk:
            energy.append(math.sqrt(sum(s * s for s in chunk) / len(chunk)))

    if len(energy) < 200:
        return None

    windows_per_sec = 100
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


def beat_snap_edl(edl: EDL, music_path: Path) -> int:
    """Snap EDL transition points to music beats. Modifies edl in place.

    Improvements over naive approach:
    - #5: Segment boundaries are snapped (not just intra-segment transitions).
    - #4: Speech skip is per-item, not per-segment. Only the item whose duration
      would change is checked — if it has keep_audio=true, that transition is
      skipped, but other transitions in the same segment are still eligible.

    Returns the number of transitions snapped.
    """
    import bisect

    bpm = estimate_bpm(music_path)
    if not bpm:
        logger.info("Beat sync: could not estimate BPM, skipping")
        return 0

    logger.info("Beat sync: detected ~%d BPM", bpm)

    beat_interval = 60.0 / bpm
    half_beat = beat_interval / 2
    total_duration = edl.estimated_duration() + 10
    beats = [i * half_beat for i in range(int(total_duration / half_beat) + 1)]

    max_shift = 0.4
    min_photo_dur = 2.0
    min_video_dur = 3.0

    offset = 0.0
    if edl.intro_style == "title_card":
        offset += edl.intro_duration

    snapped = 0
    total_transitions = 0
    n_segments = len(edl.segments)

    for seg_idx, seg in enumerate(edl.segments):
        seg_max_shift = 0.2 if seg.mode == "montage" else max_shift

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

            min_dur = min_photo_dur if item.media_type == "photo" else min_video_dur
            new_dur = item.display_duration + shift
            if new_dur < min_dur:
                continue

            item.display_duration = round(new_dur, 3)
            offset = offset + shift
            snapped += 1

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


def write_chapters(edl: EDL, segment_durations: list[float], out_path: Path) -> None:
    """Write YouTube-compatible chapter markers from EDL segments.

    segment_durations: probed duration of each rendered segment file.
    """
    offset = edl.intro_duration if edl.intro_style != "none" else 0.0
    lines = []
    for seg_idx, seg in enumerate(edl.segments):
        minutes = int(offset) // 60
        seconds = int(offset) % 60
        lines.append(f"{minutes}:{seconds:02d} {seg.name}")
        if seg_idx < len(segment_durations):
            offset += segment_durations[seg_idx]

    out_path.write_text("\n".join(lines))
    logger.info("YouTube chapters: %s (%d chapters)", out_path.name, len(lines))
