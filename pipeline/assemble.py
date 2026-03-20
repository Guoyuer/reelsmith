"""Stage 4: Render the vlog from an EDL using FFmpeg."""

from __future__ import annotations

import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from tqdm import tqdm

from .config import Config
from .edl import EDL, EditItem
from .media_utils import convert_heic, run_subprocess, _zoompan_filter, _portrait_bg_filter


# ---------------------------------------------------------------------------
# GPU-accelerated encoding (NVENC on NVIDIA, VideoToolbox on macOS)
# ---------------------------------------------------------------------------

def _target_bitrate(width: int, height: int, fps: int, quality: float = 1.0) -> str:
    """Calculate target video bitrate based on resolution, fps, and quality multiplier.

    Base bitrates from YouTube's recommended upload settings for H.264 SDR:
      4K  (2160p) 30fps: 35-45 Mbps, 60fps: 53-68 Mbps
      2K  (1440p) 30fps: 16 Mbps,    60fps: 24 Mbps
      1080p       30fps: 8 Mbps,     60fps: 12 Mbps
      720p        30fps: 5 Mbps,     60fps: 7.5 Mbps

    quality: multiplier on top of base bitrate.
      0.5 = smaller files (draft/sharing), 1.0 = YouTube quality (default),
      2.0 = master/archive quality.

    Returns bitrate string for FFmpeg (e.g. "67M").
    """
    pixels = width * height
    if pixels >= 3840 * 2160:      # 4K
        base = 45
    elif pixels >= 2560 * 1440:    # 2K
        base = 16
    elif pixels >= 1920 * 1080:    # 1080p
        base = 8
    elif pixels >= 1280 * 720:     # 720p
        base = 5
    else:                          # smaller
        base = 3

    # Scale for high frame rates
    if fps > 30:
        base = int(base * 1.5)

    # Apply quality multiplier
    base = int(base * quality)

    return f"{max(base, 1)}M"


def _detect_hw_encoder(width: int = 3840, height: int = 2160, fps: int = 60,
                       quality: float = 1.0) -> list[str]:
    """Detect best hardware encoder: prefers HEVC (smaller files, same speed on GPU).

    Tries: hevc_nvenc → h264_nvenc → hevc_videotoolbox → h264_videotoolbox → libx264.
    CPU fallback stays H.264 (H.265 CPU encoding is too slow).
    """
    import sys
    h264_br = _target_bitrate(width, height, fps, quality)
    # HEVC achieves same visual quality at ~65% of H.264 bitrate
    hevc_br = f"{max(int(int(h264_br.rstrip('M')) * 0.65), 1)}M"

    _test_cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", "nullsrc=s=640x360:d=0.1:r=15"]

    if sys.platform == "darwin":
        try:
            test = run_subprocess(_test_cmd + ["-c:v", "hevc_videotoolbox", "-f", "null", "-"],
                                  capture_output=True, text=True)
            if test.returncode == 0:
                return ["-c:v", "hevc_videotoolbox", "-b:v", hevc_br]
        except Exception:
            pass
        return ["-c:v", "h264_videotoolbox", "-b:v", h264_br]

    try:
        result = run_subprocess(["ffmpeg", "-hide_banner", "-encoders"],
                                capture_output=True, text=True)
        encoders = result.stdout or ""

        # Try HEVC NVENC first (same speed as H.264 NVENC, ~35% smaller files)
        if "hevc_nvenc" in encoders:
            test = run_subprocess(_test_cmd + ["-c:v", "hevc_nvenc", "-f", "null", "-"],
                                  capture_output=True, text=True)
            if test.returncode == 0:
                return ["-c:v", "hevc_nvenc", "-preset", "p4",
                        "-rc", "vbr", "-b:v", hevc_br, "-maxrate", hevc_br]

        # Fall back to H.264 NVENC
        if "h264_nvenc" in encoders:
            test = run_subprocess(_test_cmd + ["-c:v", "h264_nvenc", "-f", "null", "-"],
                                  capture_output=True, text=True)
            if test.returncode == 0:
                return ["-c:v", "h264_nvenc", "-preset", "p4",
                        "-rc", "vbr", "-b:v", h264_br, "-maxrate", h264_br]
    except Exception:
        pass

    # CPU fallback: H.264 only (H.265 CPU encoding is too slow)
    return ["-c:v", "libx264", "-preset", "fast", "-b:v", h264_br]


# Cache per (width, height, fps, quality) so bitrate changes with settings
_HW_ENCODER_CACHE: dict[tuple, list[str]] = {}
_QUALITY: float = 1.0  # Set by assemble() before rendering
# Probe caches — cleared at start of each assemble run
_PROBE_DIM_CACHE: dict[str, tuple[int, int]] = {}
_PROBE_DUR_CACHE: dict[str, float] = {}


def _get_encoder(width: int = 3840, height: int = 2160, fps: int = 60) -> list[str]:
    """Get cached hardware encoder args for the given resolution/fps/quality."""
    key = (width, height, fps, _QUALITY)
    if key not in _HW_ENCODER_CACHE:
        _HW_ENCODER_CACHE[key] = _detect_hw_encoder(width, height, fps, _QUALITY)
    return _HW_ENCODER_CACHE[key]


# ---------------------------------------------------------------------------
# Portrait detection & filter helpers (pure functions, easily testable)
# ---------------------------------------------------------------------------

def _probe_dimensions(path: Path) -> tuple[int, int]:
    """Use ffprobe to get (width, height) of a media file. Cached."""
    key = str(path)
    if key in _PROBE_DIM_CACHE:
        return _PROBE_DIM_CACHE[key]
    result = run_subprocess(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0:s=x",
            str(path),
        ],
        capture_output=True, text=True,
    )
    try:
        parts = result.stdout.strip().split("x")
        dims = int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        dims = 0, 0
    _PROBE_DIM_CACHE[key] = dims
    return dims


def _is_portrait(src_w: int, src_h: int) -> bool:
    """Return True if the source is clearly portrait (height > width * 1.2)."""
    return src_w > 0 and src_h > src_w * 1.2


def _build_portrait_photo_filter(
    out_w: int, out_h: int, frames: int, fps: int, zoom_rate: float,
) -> str:
    """Build FFmpeg filter_complex for portrait photos: blurred BG + sharp FG + gentle Ken Burns."""
    return (
        f"[0:v]split[bg][fg];"
        f"[bg]scale=960:-1:force_original_aspect_ratio=increase,crop=960:540,"
        f"gblur=sigma=20,scale={out_w}:{out_h}[blurred];"
        f"[fg]scale=-1:{out_h}[sharp];"
        f"[blurred][sharp]overlay=(W-w)/2:(H-h)/2[comp];"
        f"[comp]zoompan=z='min(zoom+{zoom_rate:.6f},1.08)':d={frames}"
        f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":s={out_w}x{out_h}:fps={fps}"
    )


# ---------------------------------------------------------------------------
# Beat sync — snap transitions to music beats for pro feel
# ---------------------------------------------------------------------------

def _estimate_bpm(wav_path: Path, min_bpm: int = 60, max_bpm: int = 180) -> int | None:
    """Estimate BPM from WAV using energy envelope autocorrelation. Stdlib only."""
    import math
    import struct as _struct
    import wave

    try:
        with wave.open(str(wav_path)) as w:
            sr = w.getframerate()
            nc = w.getnchannels()
            sw = w.getsampwidth()
            n_frames = w.getnframes()
            # Read at most 30s for speed
            max_frames = min(n_frames, sr * 30)
            raw = w.readframes(max_frames)
    except Exception:
        return None

    # Decode to mono samples
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
        return None  # too short

    # Energy in 10ms windows
    win = sr // 100
    energy = []
    for i in range(0, len(samples), win):
        chunk = samples[i:i + win]
        if chunk:
            energy.append(math.sqrt(sum(s * s for s in chunk) / len(chunk)))

    if len(energy) < 200:
        return None

    # Autocorrelation for BPM range
    windows_per_sec = 100  # 10ms windows
    min_lag = int(60 / max_bpm * windows_per_sec)
    max_lag = int(60 / min_bpm * windows_per_sec)
    max_lag = min(max_lag, len(energy) // 2)

    if min_lag >= max_lag:
        return None

    mean_e = sum(energy) / len(energy)
    best_lag = min_lag
    best_corr = -1.0

    for lag in range(min_lag, max_lag):
        corr = sum((energy[i] - mean_e) * (energy[i + lag] - mean_e)
                   for i in range(len(energy) - lag))
        if corr > best_corr:
            best_corr = corr
            best_lag = lag

    bpm = round(60 / (best_lag / windows_per_sec))
    return bpm


def _beat_snap_edl(edl: EDL, music_path: Path, log_fn=None) -> int:
    """Snap EDL transition points to music beats. Modifies edl in place.

    Returns the number of transitions snapped.
    """
    import bisect

    _log = log_fn or print

    bpm = _estimate_bpm(music_path)
    if not bpm:
        _log("Beat sync: could not estimate BPM, skipping")
        return 0

    _log(f"Beat sync: detected ~{bpm} BPM")

    # Build beat grid at half-beat intervals for finer snap resolution
    beat_interval = 60.0 / bpm
    half_beat = beat_interval / 2
    total_dur = edl.estimated_duration() + 10  # padding
    beats = [i * half_beat for i in range(int(total_dur / half_beat) + 1)]

    # Walk the EDL and snap transitions
    max_shift = 0.4  # seconds — max adjustment per clip
    min_photo_dur = 2.0
    min_video_dur = 3.0

    offset = 0.0
    # Account for intro
    if edl.intro_style == "title_card":
        offset += 3.0
    elif edl.intro_style == "highlight_montage":
        offset += 5.0

    snapped = 0
    total_transitions = 0

    for seg in edl.segments:
        # Skip segments with speech (changing duration desynchronizes dialogue)
        has_speech = any(item.keep_audio for item in seg.items)
        if has_speech:
            offset += sum(item.display_duration for item in seg.items)
            total_transitions += max(0, len(seg.items) - 1)
            continue

        for i, item in enumerate(seg.items):
            offset += item.display_duration
            total_transitions += 1

            if i == len(seg.items) - 1:
                continue  # last item in segment — transition is segment boundary, handled by xfade

            # Find nearest beat to this transition point
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

            if abs(shift) > max_shift:
                continue

            # Apply shift: adjust this clip's duration
            min_dur = min_photo_dur if item.media_type == "photo" else min_video_dur
            new_dur = item.display_duration + shift
            if new_dur < min_dur:
                continue

            item.display_duration = round(new_dur, 3)
            offset = offset + shift  # update running offset
            snapped += 1

    if total_transitions > 0:
        pct = int(snapped / total_transitions * 100)
        _log(f"Beat sync: snapped {snapped}/{total_transitions} transitions ({pct}%) to {bpm} BPM grid")

    return snapped


# ---------------------------------------------------------------------------
# Main assemble entry point
# ---------------------------------------------------------------------------

def assemble(cfg: Config, *, version: int = 1, progress_callback=None, skip_broken: bool = False,
             resolution: tuple[int, int] | None = None, fps: int | None = None,
             quality: float = 1.0) -> Path:
    """Read latest edl_v{N}.json and render the vlog video."""
    global _QUALITY
    _QUALITY = quality
    _PROBE_DIM_CACHE.clear()
    _PROBE_DUR_CACHE.clear()
    cfg.ensure_dirs()
    from .edl import load_latest_edl
    edl, _ = load_latest_edl(cfg)

    clips_dir = cfg.workspace / "clips"
    output_dir = cfg.workspace / "output"
    output_path = output_dir / f"vlog_v{version}.mp4"

    w, h = resolution or edl.resolution
    fps = fps or edl.fps
    lang = edl.language

    # Beat sync: snap transitions to music beats (before rendering clips)
    if edl.music and Path(edl.music.file).exists():
        _beat_snap_edl(edl, Path(edl.music.file), log_fn=print)

    # Determine parallel workers based on encoder type
    encoder = _get_encoder(w, h, fps)
    encoder_str = " ".join(encoder)
    if "nvenc" in encoder_str:
        max_workers = int(os.environ.get("VLOG_PARALLEL_CLIPS", "3"))
    elif "videotoolbox" in encoder_str:
        max_workers = 2
    else:
        max_workers = max(1, (os.cpu_count() or 4) // 2)

    # Phase 1: Render each item as a normalized clip (parallel)
    t1 = time.monotonic()

    # Build ordered task list
    tasks: list[tuple] = []  # (order, seg_idx, item_idx, item, segment)
    for seg_idx, segment in enumerate(edl.segments):
        for item_idx, item in enumerate(segment.items):
            tasks.append((len(tasks), seg_idx, item_idx, item, segment))

    total_items = len(tasks)
    clip_results: list[Path | None] = [None] * total_items
    failed_clips: list[str] = []

    pbar = tqdm(total=total_items, desc=f"Rendering clips (x{max_workers})", unit="clip",
                bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]")

    def _do_render(task):
        order, seg_idx, item_idx, item, segment = task
        clip_name = f"seg{seg_idx:02d}_item{item_idx:02d}.mp4"
        clip_path = clips_dir / clip_name

        if not clip_path.exists():
            source = Path(item.source_file)
            if not source.exists():
                return order, clip_name, None

            ct = getattr(segment, "color_temp", "neutral") or "neutral"
            if item.media_type == "photo":
                _render_photo(item, clip_path, w, h, fps, color_temp=ct,
                             text_overlay=item.text_overlay, language=lang)
            else:
                _render_video(item, clip_path, w, h, fps, color_temp=ct,
                             text_overlay=item.text_overlay, language=lang)

        if not clip_path.exists():
            return order, clip_name, None
        return order, clip_name, clip_path

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_do_render, t): t[0] for t in tasks}
        for future in as_completed(futures):
            try:
                order, clip_name, clip_path = future.result()
                if clip_path is None:
                    failed_clips.append(clip_name)
                    pbar.write(f"  SKIP: {clip_name}")
                else:
                    clip_results[order] = clip_path
            except Exception as e:
                idx = futures[future]
                pbar.write(f"  ERROR ({idx}): {e}")
                failed_clips.append(f"task_{idx}")
            pbar.update(1)
            if progress_callback:
                progress_callback(pbar.n, total_items, "")

    pbar.close()
    t_clips = time.monotonic() - t1
    print(f"Phase 1 (clips): {t_clips:.1f}s ({max_workers} workers, "
          f"{total_items - len(failed_clips)}/{total_items} OK)")

    if failed_clips and not skip_broken:
        raise RuntimeError(f"Failed to render {len(failed_clips)} clips: {', '.join(failed_clips)}")

    # Build all_clips list with transitions (must be in order)
    all_clips: list[dict] = []
    idx = 0
    for seg_idx, segment in enumerate(edl.segments):
        for item_idx, item in enumerate(segment.items):
            clip_path = clip_results[idx]
            idx += 1
            if clip_path is None:
                continue

            is_montage = getattr(segment, "mode", "narrative") == "montage"
            if is_montage:
                transition = "cut"
                td = 0.0
            elif item_idx == 0 and seg_idx > 0:
                transition = "fade_black"
                td = 1.0
            elif item_idx > 0:
                transition = segment.transition
                td = segment.transition_duration if transition != "cut" else 0.0
            else:
                transition = "cut"
                td = 0.0

            all_clips.append({
                "path": clip_path,
                "duration": item.display_duration,
                "transition": transition,
                "transition_duration": td,
                "keep_audio": item.keep_audio,
            })

    if not all_clips:
        raise RuntimeError("No clips rendered — check source files in EDL")

    # Phase 1b: Render intro/outro clips
    if edl.intro_style == "title_card" and edl.title:
        intro_path = clips_dir / "intro_title.mp4"
        if not intro_path.exists():
            _render_title_card(edl.title, edl.date_range, intro_path, w, h, fps, duration=3.0, language=lang)
        if intro_path.exists():
            all_clips.insert(0, {
                "path": intro_path, "duration": 3.0,
                "transition": "cut", "transition_duration": 0.0,
            })
            if len(all_clips) > 1:
                all_clips[1]["transition"] = "fade_black"
                all_clips[1]["transition_duration"] = 1.0

    if edl.outro_style == "fade_title" and edl.title:
        outro_path = clips_dir / "outro_title.mp4"
        if not outro_path.exists():
            _render_title_card(edl.title, "", outro_path, w, h, fps, duration=3.0, language=lang)
        if outro_path.exists():
            all_clips.append({
                "path": outro_path, "duration": 3.0,
                "transition": "fade_black", "transition_duration": 1.0,
            })

    # Compute clip start times replicating _concat_xfade's offset math exactly.
    # The xfade loop: offset starts at dur[0]-td[1], then offset += dur[i]-td[i].
    actual_durs = [_probe_duration(c["path"]) or c["duration"] for c in all_clips]
    clip_offsets: list[float] = [0.0] * len(all_clips)
    if len(all_clips) > 1:
        xf_offset = 0.0
        for i in range(1, len(all_clips)):
            td = all_clips[i].get("transition_duration", 0.0)
            if all_clips[i].get("transition") == "cut":
                td = 0.0
            if i == 1:
                xf_offset = actual_durs[0] - td
            clip_offsets[i] = xf_offset
            xf_offset += actual_durs[i] - td

    speech_ranges: list[tuple[float, float]] = []
    speech_clips: list[tuple[float, Path]] = []
    for i, clip in enumerate(all_clips):
        if clip.get("keep_audio"):
            speech_ranges.append((clip_offsets[i], clip_offsets[i] + actual_durs[i]))
            speech_clips.append((clip_offsets[i], clip["path"]))

    # Phase 2: Concatenate with transitions (video only — xfade drops audio)
    t2 = time.monotonic()
    print(f"Concatenating {len(all_clips)} clips...")
    no_music_path = output_dir / f"vlog_v{version}_nomix.mp4"
    _concatenate(all_clips, no_music_path)
    print(f"Phase 2 (concat): {time.monotonic() - t2:.1f}s")

    # Phase 2b: Build speech audio track from keep_audio clips at correct offsets
    speech_audio_path = None
    if speech_clips:
        video_dur = _probe_duration(no_music_path)
        speech_audio_path = output_dir / f"vlog_v{version}_speech.wav"
        _build_speech_track(speech_clips, video_dur, speech_audio_path)
        print(f"Speech track: {len(speech_clips)} clips merged into {speech_audio_path.name}")

    # Phase 3: Mix music + speech audio
    t3 = time.monotonic()
    if edl.music and Path(edl.music.file).exists():
        music_dur = _probe_duration(Path(edl.music.file))
        video_dur = _probe_duration(no_music_path)
        print(f"Mixing music: video={video_dur:.1f}s, music={music_dur:.1f}s, "
              f"volume={edl.music.volume}, fade_in={edl.music.fade_in}s, fade_out={edl.music.fade_out}s")
        _add_music(no_music_path, edl.music, output_path,
                   speech_ranges=speech_ranges, speech_audio=speech_audio_path)
        no_music_path.unlink(missing_ok=True)
        if speech_audio_path:
            speech_audio_path.unlink(missing_ok=True)
    elif speech_audio_path:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(no_music_path),
            "-i", str(speech_audio_path),
            "-map", "0:v", "-map", "1:a",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            str(output_path),
        ]
        run_subprocess(cmd, capture_output=True)
        no_music_path.unlink(missing_ok=True)
        speech_audio_path.unlink(missing_ok=True)
    else:
        shutil.move(str(no_music_path), str(output_path))
    print(f"Phase 3 (audio): {time.monotonic() - t3:.1f}s")

    duration = _probe_duration(output_path)
    total_time = time.monotonic() - t1
    print(f"Done: {output_path} ({duration:.1f}s, rendered in {total_time:.0f}s)")

    # Generate YouTube chapter markers
    chapters_path = output_dir / f"chapters_v{version}.txt"
    _write_chapters(edl, all_clips, chapters_path)

    return output_path


def _build_speech_track(
    speech_clips: list[tuple[float, Path]],
    total_duration: float,
    output_path: Path,
) -> None:
    """Merge audio from keep_audio clips into one audio file at correct time offsets.

    Each clip's audio is placed at its offset in the timeline. Silence fills gaps.
    Output: WAV file with the same duration as the video.
    """
    if not speech_clips:
        return

    # Build FFmpeg command: input each clip, delay its audio to the right offset, then amix
    inputs = []
    filter_parts = []
    for i, (offset_s, clip_path) in enumerate(speech_clips):
        inputs += ["-i", str(clip_path)]
        # adelay takes milliseconds, pad to fill gaps with silence
        delay_ms = int(offset_s * 1000)
        # Fade in/out to avoid abrupt speech cuts at clip boundaries
        filter_parts.append(f"[{i}:a]afade=t=in:d=0.3,afade=t=out:st=99:d=0.3,adelay={delay_ms}|{delay_ms}[a{i}]")

    # Mix all delayed audio streams
    mix_inputs = "".join(f"[a{i}]" for i in range(len(speech_clips)))
    filter_parts.append(
        f"{mix_inputs}amix=inputs={len(speech_clips)}"
        f":duration=longest:dropout_transition=0[out]"
    )

    cmd = ["ffmpeg", "-y"] + inputs + [
        "-filter_complex", ";".join(filter_parts),
        "-map", "[out]",
        "-t", str(total_duration),
        "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
        str(output_path),
    ]
    run_subprocess(cmd, capture_output=True)


def _write_chapters(edl: EDL, clips: list[dict], out_path: Path) -> None:
    """Write YouTube-compatible chapter markers from EDL segments."""
    lines = []
    offset = 0.0
    clip_idx = 0

    # Account for intro
    has_intro = edl.intro_style != "none"
    if has_intro and clips:
        offset = clips[0]["duration"]
        clip_idx = 1

    for seg in edl.segments:
        minutes = int(offset) // 60
        seconds = int(offset) % 60
        lines.append(f"{minutes}:{seconds:02d} {seg.name}")
        for item in seg.items:
            if clip_idx < len(clips):
                td = clips[clip_idx].get("transition_duration", 0.0)
                offset += clips[clip_idx]["duration"] - td
                clip_idx += 1

    out_path.write_text("\n".join(lines))
    print(f"YouTube chapters: {out_path.name} ({len(lines)} chapters)")


def _find_font(language: str = "en") -> str:
    """Find a suitable font for title cards (cross-platform, CJK-aware)."""
    needs_cjk = language in ("cn", "both")
    if needs_cjk:
        candidates = [
            "/System/Library/Fonts/STHeiti Medium.ttc",              # macOS CJK
            "/System/Library/Fonts/PingFang.ttc",                    # macOS CJK
            "C\\:/Windows/Fonts/msyh.ttc",                           # Windows YaHei
            "C\\:/Windows/Fonts/simhei.ttf",                         # Windows SimHei
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",  # Linux CJK
            "/usr/share/fonts/truetype/droid/DroidSansFallback.ttf", # Linux fallback
        ]
    else:
        candidates = [
            "/System/Library/Fonts/Helvetica.ttc",                # macOS
            "C\\:/Windows/Fonts/segoeui.ttf",                     # Windows
            "C\\:/Windows/Fonts/arial.ttf",                       # Windows fallback
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",   # Linux
        ]
    for f in candidates:
        check_path = f.replace("\\:", ":")
        if Path(check_path).exists():
            return f
    return ""  # FFmpeg will use its built-in default


def _render_title_card(
    title: str, subtitle: str, out: Path, w: int, h: int, fps: int,
    duration: float = 3.0, language: str = "en",
) -> None:
    """Render a professional title card with gradient background and animated text."""
    safe_title = title.replace("'", "\u2019").replace(":", "\\:")
    font = _find_font(language)
    font_arg = f":fontfile='{font}'" if font else ""

    # Scale font for long titles
    title_size = int(h * 0.08)
    if len(title) > 25:
        title_size = int(title_size * 25 / len(title))

    # Gradient background: dark blue-purple (#0f0c29 → #302b63 → #24243e)
    gradient = (
        f"color=c=0x0f0c29:s={w}x{h}:d={duration}:r={fps}[bg1];"
        f"color=c=0x302b63:s={w}x{h//2}:d={duration}:r={fps}[bg2];"
        f"[bg1][bg2]overlay=0:h/4:format=auto[grad]"
    )

    # Title: fade in from 0.5s, with slight upward drift
    title_y = f"(h-text_h)/2-{int(h*0.03)}+{int(h*0.02)}*(1-t/{duration})"
    title_text = (
        f"drawtext=text='{safe_title}'{font_arg}"
        f":fontsize={title_size}:fontcolor=white"
        f":x=(w-text_w)/2:y={title_y}"
        f":alpha='if(lt(t,0.5),t/0.5,if(gt(t,{duration-0.8}),(({duration}-t)/0.8),1))'"
    )

    # Subtle line separator
    line_y = int(h * 0.55)
    line_w = int(w * 0.15)
    line_x = (w - line_w) // 2
    separator = (
        f",drawbox=x={line_x}:y={line_y}:w={line_w}:h=2"
        f":color=white@0.4:t=fill"
        f":enable='between(t,0.8,{duration-0.5})'"
    )

    # Subtitle: appears after title with slight delay
    sub_text = ""
    if subtitle:
        safe_sub = subtitle.replace("'", "\u2019").replace(":", "\\:")
        sub_text = (
            f",drawtext=text='{safe_sub}'{font_arg}"
            f":fontsize={int(h * 0.035)}:fontcolor=white@0.6"
            f":x=(w-text_w)/2:y={int(h*0.59)}"
            f":alpha='if(lt(t,1.0),max(0,(t-0.7)/0.3),if(gt(t,{duration-0.8}),(({duration}-t)/0.8),1))'"
        )

    # Overall fade
    fade = f",fade=t=in:d=0.5,fade=t=out:st={duration - 0.8}:d=0.8"

    cmd = [
        "ffmpeg", "-y",
        "-filter_complex",
        f"{gradient};[grad]{title_text}{separator}{sub_text}{fade}",
        *_get_encoder(w, h, fps), "-pix_fmt", "yuv420p",
        "-an",
        str(out),
    ]
    run_subprocess(cmd, capture_output=True, text=True)


def _color_grade(color_temp: str = "neutral") -> str:
    """Subtle color grade with optional temperature shift."""
    base = "eq=contrast=1.02:brightness=0.01:saturation=1.05"
    if color_temp == "warm":
        return f"{base},colorbalance=rs=0.02:gs=0.01:bs=-0.02"
    elif color_temp == "cool":
        return f"{base},colorbalance=rs=-0.02:gs=0.0:bs=0.02"
    return base


def _drawtext_filter(text: str, position: str, font_size: int,
                     clip_duration: float, language: str = "en") -> str:
    """Build a drawtext filter string for text overlay (no leading comma)."""
    y_positions = {"top": "50", "center": "(h-text_h)/2", "bottom": "h-text_h-60"}
    y_expr = y_positions.get(position, y_positions["bottom"])
    safe_text = text.replace("'", "\u2019").replace(":", "\\:")
    if len(text) > 20:
        font_size = int(font_size * 20 / len(text))
    end_time = min(clip_duration - 0.5, 3.0)
    font = _find_font(language)
    font_arg = f":fontfile='{font}'" if font else ""
    return (
        f"drawtext=text='{safe_text}'{font_arg}"
        f":fontsize={font_size}:fontcolor=white"
        f":borderw=2:bordercolor=black"
        f":x=(w-text_w)/2:y={y_expr}"
        f":enable='between(t,0.5,{end_time:.1f})'"
    )


def _render_photo(item: EditItem, out: Path, w: int, h: int, fps: int,
                   color_temp: str = "neutral",
                   text_overlay=None, language: str = "en") -> None:
    """Render a photo with Ken Burns effect as a video clip. Text overlay baked in."""
    source = Path(item.source_file)

    # Convert HEIC to JPEG first — FFmpeg can't use HEIC with -loop 1
    if source.suffix.lower() in {".heic", ".heif"}:
        try:
            source = convert_heic(source)
        except RuntimeError:
            print(f"    HEIC convert failed: {item.source_file}")
            return

    frames = int(item.display_duration * fps)
    # Vary zoom intensity by effect type for richer visual feel
    zoom_targets = {
        "ken_burns_in": 0.25,    # gentle zoom in
        "ken_burns_out": 0.20,   # subtle zoom out
        "ken_burns_left": 0.15,  # gentle pan
        "ken_burns_right": 0.15, # gentle pan
        "static": 0.0,
    }
    target = zoom_targets.get(item.effect, 0.25)
    # Add slight variation per-clip using hash of filename
    variation = (hash(item.source_file) % 10) / 100  # 0.00-0.09
    zoom_rate = 0.001 + ((target + variation) / frames) if target > 0 else 0

    # Text overlay suffix (empty if no overlay)
    dt = ""
    if text_overlay:
        dt = "," + _drawtext_filter(text_overlay.text, text_overlay.position,
                                     text_overlay.font_size, item.display_duration, language)

    # Probe dimensions (after HEIC conversion) to decide portrait vs landscape
    src_w, src_h = _probe_dimensions(source)
    portrait = _is_portrait(src_w, src_h)

    if portrait:
        # Portrait: blurred background + sharp foreground + gentle Ken Burns
        portrait_zoom_rate = 0.001 + (0.08 / frames)  # gentler zoom for portrait
        fc = _build_portrait_photo_filter(w, h, frames, fps, portrait_zoom_rate)
        cmd = [
            "ffmpeg", "-y", "-loop", "1", "-i", str(source),
            "-t", str(item.display_duration),
            "-filter_complex", f"{fc}{dt}",
            *_get_encoder(w, h, fps), "-pix_fmt", "yuv420p",
            "-an",
            str(out),
        ]
    else:
        # Landscape: Ken Burns zoompan with face-aware crop
        direction_map = {
            "ken_burns_in": "in",
            "ken_burns_out": "out",
            "ken_burns_left": "left",
            "ken_burns_right": "right",
            "static": "static",
        }
        direction = direction_map.get(item.effect, "in")
        zp = _zoompan_filter(zoom_rate, frames, w, h, fps, direction=direction)

        # Choose scaling strategy based on aspect ratio
        ow, oh = w * 2, h * 2
        src_ratio = src_w / src_h if src_h > 0 else 1.0
        out_ratio = ow / oh

        if abs(src_ratio - out_ratio) / out_ratio < 0.05:
            # Aspect ratio close enough — just scale, no crop or pad
            scale_filter = f"scale={ow}:{oh}"
        else:
            # Blurred background + sharp foreground (no black bars)
            scale_filter = (
                f"split[bg][fg];"
                f"[bg]scale={ow}:{oh}:force_original_aspect_ratio=increase,"
                f"crop={ow}:{oh},gblur=sigma=25[blurred];"
                f"[fg]scale={ow}:{oh}:force_original_aspect_ratio=decrease[sharp];"
                f"[blurred][sharp]overlay=(W-w)/2:(H-h)/2"
            )

        cg = _color_grade(color_temp)
        # Sharpen after zoompan to restore detail lost in resampling
        sharpen = ",unsharp=3:3:0.5:3:3:0.0"
        if "[bg]" in scale_filter:
            cmd = [
                "ffmpeg", "-y", "-loop", "1", "-i", str(source),
                "-t", str(item.display_duration),
                "-filter_complex", f"{scale_filter}[comp];[comp]{zp},{cg}{sharpen}{dt}",
            ]
        else:
            cmd = [
                "ffmpeg", "-y", "-loop", "1", "-i", str(source),
                "-t", str(item.display_duration),
                "-vf", f"{scale_filter},{zp},{cg}{sharpen}{dt}",
            ]
        cmd += [
            *_get_encoder(w, h, fps), "-pix_fmt", "yuv420p",
            "-an",
            str(out),
        ]

    result = run_subprocess(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"    Photo render failed: {result.stderr[-200:]}")


def _render_video(item: EditItem, out: Path, w: int, h: int, fps: int,
                   color_temp: str = "neutral",
                   text_overlay=None, language: str = "en") -> None:
    """Trim and normalize a video clip. Text overlay baked in. Preserves audio if keep_audio."""
    cmd = ["ffmpeg", "-y"]
    if item.start_time is not None:
        cmd += ["-ss", str(item.start_time)]
    cmd += ["-i", str(item.source_file)]

    duration = item.display_duration
    if item.start_time is not None and item.end_time is not None:
        duration = item.end_time - item.start_time
    cmd += ["-t", str(duration)]

    audio_args = ["-c:a", "aac", "-b:a", "192k"] if item.keep_audio else ["-an"]

    # Speed ramp: setpts for video, atempo for audio
    speed = getattr(item, "playback_speed", 1.0) or 1.0
    speed_vf = f",setpts={1/speed:.4f}*PTS" if speed != 1.0 else ""
    speed_af = f"-af atempo={speed}" if speed != 1.0 and item.keep_audio else ""

    # Text overlay suffix (empty if no overlay)
    dt = ""
    if text_overlay:
        dt = "," + _drawtext_filter(text_overlay.text, text_overlay.position,
                                     text_overlay.font_size, item.display_duration, language)

    # Probe dimensions to decide portrait vs landscape
    src_w, src_h = _probe_dimensions(Path(item.source_file))
    portrait = _is_portrait(src_w, src_h)

    cg = _color_grade(color_temp)
    if portrait:
        fc = _portrait_bg_filter(w, h)
        cmd += [
            "-filter_complex", f"{fc},{cg}{speed_vf}{dt}",
            *_get_encoder(w, h, fps), "-pix_fmt", "yuv420p",
            "-r", str(fps),
        ]
    else:
        cmd += [
            "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                   f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,{cg}{speed_vf}{dt}",
            *_get_encoder(w, h, fps), "-pix_fmt", "yuv420p",
            "-r", str(fps),
        ]
    if speed_af:
        cmd += speed_af.split()
    cmd += [*audio_args, str(out)]

    result = run_subprocess(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"    Video render failed: {result.stderr[-200:]}")



def _concatenate(clips: list[dict], output_path: Path) -> None:
    """Concatenate clips, using xfade for crossfades and fade_black."""
    if len(clips) == 1:
        shutil.copy(str(clips[0]["path"]), str(output_path))
        return

    # For large numbers of clips, use concat demuxer (simpler, more robust)
    # For smaller sets with transitions, use xfade filter chain
    if len(clips) > 30 or all(c["transition"] == "cut" for c in clips):
        _concat_demuxer(clips, output_path)
    else:
        _concat_xfade(clips, output_path)


def _concat_demuxer(clips: list[dict], output_path: Path) -> None:
    """Simple concatenation via concat demuxer (no transitions)."""
    list_path = output_path.parent / "concat_list.txt"
    with open(list_path, "w") as f:
        for clip in clips:
            f.write(f"file '{clip['path'].resolve()}'\n")

    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(list_path),
        *_get_encoder(), "-pix_fmt", "yuv420p",
        str(output_path),
    ]
    run_subprocess(cmd, capture_output=True)
    list_path.unlink(missing_ok=True)


def _concat_xfade(clips: list[dict], output_path: Path) -> None:
    """Concatenate with xfade transitions between clips."""
    inputs = []
    # Probe actual durations to avoid frame-rounding drift in offset calculation
    actual_durs = []
    for clip in clips:
        inputs += ["-i", str(clip["path"])]
        actual_durs.append(_probe_duration(clip["path"]) or clip["duration"])

    # Build xfade filter chain
    filter_parts = []
    offset = 0.0

    for i in range(1, len(clips)):
        td = clips[i]["transition_duration"]
        transition_type = clips[i]["transition"]

        if transition_type == "cut":
            td = 0.0

        xfade_transition = {
            "crossfade": "fade",
            "fade_black": "fadeblack",
            "wipe_left": "wipeleft",
            "dissolve": "dissolve",
            "smoothleft": "smoothleft",
            "smoothright": "smoothright",
            "circlecrop": "circlecrop",
            "cut": "fade",
        }.get(transition_type, "fade")

        if i == 1:
            in_label = "[0:v]"
            offset = actual_durs[0] - td
        else:
            in_label = f"[v{i-1}]"

        out_label = f"[v{i}]" if i < len(clips) - 1 else "[vout]"

        if td > 0:
            filter_parts.append(
                f"{in_label}[{i}:v]xfade=transition={xfade_transition}"
                f":duration={td}:offset={offset}{out_label}"
            )
        else:
            # No transition — just concat via overlay workaround
            filter_parts.append(
                f"{in_label}[{i}:v]xfade=transition=fade"
                f":duration=0.01:offset={offset}{out_label}"
            )

        offset += actual_durs[i] - td

    if not filter_parts:
        _concat_demuxer(clips, output_path)
        return

    filter_complex = ";".join(filter_parts)

    cmd = ["ffmpeg", "-y"] + inputs + [
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        *_get_encoder(), "-pix_fmt", "yuv420p",
        str(output_path),
    ]
    result = run_subprocess(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"xfade failed, falling back to concat demuxer: {result.stderr[-200:]}")
        _concat_demuxer(clips, output_path)


def _add_music(video_path: Path, music, output_path: Path, *,
               speech_ranges: list[tuple[float, float]] | None = None,
               speech_audio: Path | None = None) -> None:
    """Mix background music + speech audio into the video.

    speech_audio: pre-built WAV with speech clips at correct offsets.
    speech_ranges: time ranges where music should duck for speech.
    """
    total_dur = _probe_duration(video_path)
    music_dur = _probe_duration(Path(music.file))
    fade_out_start = max(0, total_dur - music.fade_out)

    # Loop music if shorter than video
    loop_filter = ""
    if music_dur > 0 and music_dur < total_dur - 1:
        loops = int(total_dur / music_dur) + 1
        loop_filter = f"aloop=loop={loops}:size={int(music_dur * 32000)},atrim=0:{total_dur},"

    # Build music volume: duck during speech
    if speech_ranges:
        duck_vol = music.volume * 0.3
        vol_expr = str(music.volume)
        for start, end in reversed(speech_ranges):
            vol_expr = f"if(between(t,{start:.1f},{end:.1f}),{duck_vol:.3f},{vol_expr})"
        music_vol_filter = f"volume='{vol_expr}':eval=frame"
    else:
        music_vol_filter = f"volume={music.volume}"

    if speech_audio and speech_audio.exists():
        # 3-way mix: video (no audio) + speech audio + music
        # Input 0: video, Input 1: music, Input 2: speech audio
        # Use volume-weighted amix so speech stays loud and music stays low
        audio_filter = (
            f"[1:a]{loop_filter}{music_vol_filter},"
            f"afade=t=in:d={music.fade_in},"
            f"afade=t=out:st={fade_out_start}:d={music.fade_out}[bg];"
            f"[2:a]volume=1.0,apad[speech];"
            f"[speech][bg]amix=inputs=2:duration=first:weights=3 1,"
            f"loudnorm=I=-16:TP=-1.5:LRA=11[a]"
        )
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-i", str(music.file),
            "-i", str(speech_audio),
            "-filter_complex", audio_filter,
            "-map", "0:v", "-map", "[a]",
            "-shortest",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            str(output_path),
        ]
    else:
        # Music only
        audio_filter = (
            f"[1:a]{loop_filter}{music_vol_filter},"
            f"afade=t=in:d={music.fade_in},"
            f"afade=t=out:st={fade_out_start}:d={music.fade_out},"
            f"loudnorm=I=-16:TP=-1.5:LRA=11[a]"
        )
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-i", str(music.file),
            "-filter_complex", audio_filter,
            "-map", "0:v", "-map", "[a]",
            "-shortest",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            str(output_path),
        ]
    run_subprocess(cmd, capture_output=True)


def _probe_duration(path: Path) -> float:
    """Get video duration in seconds. Cached."""
    key = str(path)
    if key in _PROBE_DUR_CACHE:
        return _PROBE_DUR_CACHE[key]
    result = run_subprocess(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            str(path),
        ],
        capture_output=True, text=True,
    )
    try:
        dur = float(result.stdout.strip())
    except ValueError:
        dur = 0.0
    _PROBE_DUR_CACHE[key] = dur
    return dur
