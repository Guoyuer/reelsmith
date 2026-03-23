"""Audio processing: BPM detection, beat sync, speech tracks, music mixing, chapters."""

from __future__ import annotations

import logging
import math
import shutil
import struct as _struct
import wave
from pathlib import Path

from ..edl import EDL
from ..media_utils import run_subprocess
from ._encoder import RenderContext

logger = logging.getLogger("vlog.assemble.audio")


# ---------------------------------------------------------------------------
# BPM estimation & beat sync
# ---------------------------------------------------------------------------


def estimate_bpm(wav_path: Path, min_bpm: int = 60, max_bpm: int = 180) -> int | None:
    """Estimate BPM from WAV using energy envelope autocorrelation. Stdlib only."""
    try:
        with wave.open(str(wav_path)) as w:
            sr = w.getframerate()
            nc = w.getnchannels()
            sw = w.getsampwidth()
            n_frames = w.getnframes()
            max_frames = min(n_frames, sr * 30)
            raw = w.readframes(max_frames)
    except (OSError, wave.Error) as e:
        logger.warning("BPM estimation: could not read %s: %s", wav_path, e)
        return None

    n_samples = len(raw) // sw
    if sw == 2:
        samples = _struct.unpack(f"<{n_samples}h", raw)
    elif sw == 4:
        samples = _struct.unpack(f"<{n_samples}i", raw)
    else:
        return None

    if nc == 2:
        samples = [(samples[i] + samples[i + 1]) / 2 for i in range(0, n_samples - 1, 2)]
    elif nc > 2:
        return None

    if len(samples) < sr * 2:
        return None

    win = sr // 100
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
        corr = sum((energy[i] - mean_e) * (energy[i + lag] - mean_e) for i in range(len(energy) - lag))
        if corr > best_corr:
            best_corr = corr
            best_lag = lag

    bpm = round(60 / (best_lag / windows_per_sec))
    return bpm


def beat_snap_edl(edl: EDL, music_path: Path) -> int:
    """Snap EDL transition points to music beats. Modifies edl in place.

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
    total_dur = edl.estimated_duration() + 10
    beats = [i * half_beat for i in range(int(total_dur / half_beat) + 1)]

    max_shift = 0.4
    min_photo_dur = 2.0
    min_video_dur = 3.0

    offset = 0.0
    if edl.intro_style == "title_card":
        offset += edl.intro_duration

    snapped = 0
    total_transitions = 0

    for seg in edl.segments:
        seg_max_shift = 0.2 if seg.mode == "montage" else max_shift
        has_speech = any(item.keep_audio for item in seg.items)
        if has_speech:
            offset += sum(item.display_duration for item in seg.items)
            total_transitions += max(0, len(seg.items) - 1)
            continue

        for i, item in enumerate(seg.items):
            offset += item.display_duration
            total_transitions += 1

            if i == len(seg.items) - 1:
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
        logger.info("Beat sync: snapped %d/%d transitions (%d%%) to %d BPM grid", snapped, total_transitions, pct, bpm)

    return snapped


# ---------------------------------------------------------------------------
# Speech & music
# ---------------------------------------------------------------------------


def build_speech_track(
    speech_clips: list[tuple[float, Path]],
    total_duration: float,
    output_path: Path,
) -> None:
    """Merge audio from keep_audio clips into one WAV at correct time offsets."""
    if not speech_clips:
        return

    inputs = []
    filter_parts = []
    for i, (offset_s, clip_path) in enumerate(speech_clips):
        inputs += ["-i", str(clip_path)]
        delay_ms = int(offset_s * 1000)
        filter_parts.append(f"[{i}:a]afade=t=in:d=0.3,afade=t=out:st=99:d=0.3,adelay={delay_ms}|{delay_ms}[a{i}]")

    mix_inputs = "".join(f"[a{i}]" for i in range(len(speech_clips)))
    filter_parts.append(f"{mix_inputs}amix=inputs={len(speech_clips)}" f":duration=longest:dropout_transition=0[out]")

    cmd = (
        ["ffmpeg", "-y"]
        + inputs
        + [
            "-filter_complex",
            ";".join(filter_parts),
            "-map",
            "[out]",
            "-t",
            str(total_duration),
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
    if result.returncode != 0:
        raise RuntimeError(f"Speech track build failed: {result.stderr[-300:]}")


def add_music(
    video_path: Path,
    music,
    output_path: Path,
    *,
    ctx: RenderContext | None = None,
    speech_ranges: list[tuple[float, float]] | None = None,
    speech_audio_path: Path | None = None,
    duck_ratio: float = 0.3,
) -> None:
    """Mix background music + speech audio into the video."""
    total_dur = ctx.probe_duration(video_path) if ctx else 0.0
    music_dur = ctx.probe_duration(Path(music.file)) if ctx else 0.0
    fade_out_start = max(0, total_dur - music.fade_out)

    loop_filter = ""
    if music_dur > 0 and music_dur < total_dur - 1:
        loops = int(total_dur / music_dur) + 1
        loop_filter = f"aloop=loop={loops}:size={int(music_dur * 32000)},atrim=0:{total_dur},"

    if speech_ranges:
        vol_expr = f"{music.volume:.3f}"
        for start, end in speech_ranges:
            attack_start = max(0, start - 0.3)
            gain = f"clip((t-{attack_start:.1f})/0.3,0,1)-clip((t-{end:.1f})/1.0,0,1)"
            vol_expr = f"({vol_expr})*(1-{1-duck_ratio:.3f}*({gain}))"
        music_vol_filter = f"volume='{vol_expr}':eval=frame"
    else:
        music_vol_filter = f"volume={music.volume}"

    if speech_audio_path and speech_audio_path.exists():
        audio_filter = (
            f"[1:a]{loop_filter}{music_vol_filter},"
            f"afade=t=in:d={music.fade_in},"
            f"afade=t=out:st={fade_out_start}:d={music.fade_out}[bg];"
            f"[2:a]volume=1.0,apad[speech];"
            f"[speech][bg]amix=inputs=2:duration=first:weights=3 1,"
            f"loudnorm=I=-16:TP=-1.5:LRA=11[a]"
        )
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-i",
            str(music.file),
            "-i",
            str(speech_audio_path),
            "-filter_complex",
            audio_filter,
            "-map",
            "0:v",
            "-map",
            "[a]",
            "-shortest",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(output_path),
        ]
    else:
        audio_filter = (
            f"[1:a]{loop_filter}{music_vol_filter},"
            f"afade=t=in:d={music.fade_in},"
            f"afade=t=out:st={fade_out_start}:d={music.fade_out},"
            f"loudnorm=I=-16:TP=-1.5:LRA=11[a]"
        )
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-i",
            str(music.file),
            "-filter_complex",
            audio_filter,
            "-map",
            "0:v",
            "-map",
            "[a]",
            "-shortest",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(output_path),
        ]
    result = run_subprocess(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Music mixing failed: {result.stderr[-300:]}")


def mix_final_audio(
    video_path: Path,
    output_path: Path,
    music_track=None,
    speech_audio_path: Path | None = None,
    speech_ranges: list[tuple[float, float]] | None = None,
    duck_ratio: float = 0.3,
    ctx: RenderContext | None = None,
) -> None:
    """Mix music and/or speech audio into the final video.

    Handles three cases:
    - Music + optional speech: mix via add_music()
    - Speech only (no music): mux speech audio into video
    - Neither: just move the video file to output_path

    Cleans up intermediate files (video_path, speech_audio) after mixing.
    """

    has_music = music_track is not None and music_track.file and Path(music_track.file).exists()

    if has_music:
        add_music(
            video_path,
            music_track,
            output_path,
            ctx=ctx,
            speech_ranges=speech_ranges,
            speech_audio_path=speech_audio_path,
            duck_ratio=duck_ratio,
        )
        video_path.unlink(missing_ok=True)
        if speech_audio_path:
            speech_audio_path.unlink(missing_ok=True)
    elif speech_audio_path and speech_audio_path.exists():
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-i",
            str(speech_audio_path),
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(output_path),
        ]
        result = run_subprocess(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Final audio mix failed: {result.stderr[-300:]}")
        video_path.unlink(missing_ok=True)
        speech_audio_path.unlink(missing_ok=True)
    else:
        shutil.move(str(video_path), str(output_path))


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
