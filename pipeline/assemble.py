"""Stage 4: Render the vlog from an EDL using FFmpeg."""

from __future__ import annotations

import shutil
from pathlib import Path

try:
    import cv2
except ImportError:
    cv2 = None  # Face detection disabled; falls back to center crop
from tqdm import tqdm

from .config import Config
from .edl import EDL, EditItem, Segment
from .media_utils import convert_heic, run_subprocess, _zoompan_filter, _portrait_bg_filter

# YuNet DNN face detector (loaded once, 100% recall on trip photos)
_YUNET_MODEL = Path(__file__).parent / "face_detection_yunet_2023mar.onnx"


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
    """Detect hardware encoder and return FFmpeg args with quality bitrate.

    Returns ["-c:v", "h264_nvenc", "-b:v", "67M", ...] on NVIDIA,
    ["-c:v", "h264_videotoolbox", "-b:v", "67M", ...] on macOS,
    or ["-c:v", "libx264", "-b:v", "67M", ...] as fallback.
    """
    import sys
    bitrate = _target_bitrate(width, height, fps, quality)

    if sys.platform == "darwin":
        return ["-c:v", "h264_videotoolbox", "-b:v", bitrate]

    # Check for NVIDIA NVENC
    try:
        result = run_subprocess(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True,
        )
        if "h264_nvenc" in (result.stdout or ""):
            test = run_subprocess(
                ["ffmpeg", "-y", "-f", "lavfi", "-i", "nullsrc=s=640x360:d=0.1:r=15",
                 "-c:v", "h264_nvenc", "-f", "null", "-"],
                capture_output=True, text=True,
            )
            if test.returncode == 0:
                return ["-c:v", "h264_nvenc", "-preset", "p4",
                        "-rc", "vbr", "-b:v", bitrate, "-maxrate", bitrate]
    except Exception:
        pass

    return ["-c:v", "libx264", "-preset", "fast", "-b:v", bitrate]


# Cache per (width, height, fps, quality) so bitrate changes with settings
_HW_ENCODER_CACHE: dict[tuple, list[str]] = {}
_QUALITY: float = 1.0  # Set by assemble() before rendering


def _get_encoder(width: int = 3840, height: int = 2160, fps: int = 60) -> list[str]:
    """Get cached hardware encoder args for the given resolution/fps/quality."""
    key = (width, height, fps, _QUALITY)
    if key not in _HW_ENCODER_CACHE:
        _HW_ENCODER_CACHE[key] = _detect_hw_encoder(width, height, fps, _QUALITY)
    return _HW_ENCODER_CACHE[key]


# ---------------------------------------------------------------------------
# Face detection & crop helpers
# ---------------------------------------------------------------------------

def _detect_face_center(path: Path) -> tuple[float, float] | None:
    """Detect faces and return their center as (cx_ratio, cy_ratio) in 0-1 range.

    Uses OpenCV's YuNet DNN detector. Returns None if no faces found or cv2
    is not installed.
    """
    if cv2 is None:
        return None
    img = cv2.imread(str(path))
    if img is None:
        return None
    h, w = img.shape[:2]
    scale = min(1.0, 640 / max(w, h))
    nw, nh = int(w * scale), int(h * scale)
    small = cv2.resize(img, (nw, nh))

    detector = cv2.FaceDetectorYN.create(str(_YUNET_MODEL), "", (nw, nh), 0.7)
    _, faces = detector.detect(small)
    if faces is None or len(faces) == 0:
        return None

    # Average center of all faces (in original image coordinates)
    cx = sum((f[0] + f[2] / 2) / scale for f in faces) / len(faces)
    cy = sum((f[1] + f[3] / 2) / scale for f in faces) / len(faces)
    return cx / w, cy / h


# ---------------------------------------------------------------------------
# Portrait detection & filter helpers (pure functions, easily testable)
# ---------------------------------------------------------------------------

def _probe_dimensions(path: Path) -> tuple[int, int]:
    """Use ffprobe to get (width, height) of a media file."""
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
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return 0, 0


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
# Main assemble entry point
# ---------------------------------------------------------------------------

def assemble(cfg: Config, *, version: int = 1, progress_callback=None, skip_broken: bool = False,
             resolution: tuple[int, int] | None = None, fps: int | None = None,
             quality: float = 1.0) -> Path:
    """Read latest edl_v{N}.json and render the vlog video."""
    global _QUALITY
    _QUALITY = quality
    cfg.ensure_dirs()
    from .edl import load_latest_edl
    edl, _ = load_latest_edl(cfg)

    clips_dir = cfg.workspace / "clips"
    output_dir = cfg.workspace / "output"
    output_path = output_dir / f"vlog_v{version}.mp4"

    w, h = resolution or edl.resolution
    fps = fps or edl.fps

    # Phase 1: Render each item as a normalized clip
    all_clips: list[dict] = []  # {"path": Path, "duration": float, "transition": str, "transition_duration": float}
    failed_clips: list[str] = []
    total_items = sum(len(seg.items) for seg in edl.segments)
    pbar = tqdm(total=total_items, desc="Rendering clips", unit="clip",
                bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]")

    for seg_idx, segment in enumerate(edl.segments):
        for item_idx, item in enumerate(segment.items):
            clip_name = f"seg{seg_idx:02d}_item{item_idx:02d}.mp4"
            clip_path = clips_dir / clip_name

            if not clip_path.exists():
                source = Path(item.source_file)
                if not source.exists():
                    pbar.write(f"  SKIP (missing): {item.source_file}")
                    failed_clips.append(clip_name)
                    pbar.update(1)
                    continue

                pbar.set_postfix_str(clip_name, refresh=True)
                ct = getattr(segment, "color_temp", "neutral") or "neutral"
                if item.media_type == "photo":
                    _render_photo(item, clip_path, w, h, fps, color_temp=ct)
                else:
                    _render_video(item, clip_path, w, h, fps, color_temp=ct)

            if not clip_path.exists():
                failed_clips.append(clip_name)
                pbar.update(1)
                continue

            # Apply text overlay if specified
            if item.text_overlay:
                overlaid = clips_dir / f"{clip_path.stem}_txt.mp4"
                if not overlaid.exists():
                    _add_text_overlay(
                        clip_path, overlaid,
                        item.text_overlay.text,
                        item.text_overlay.position,
                        item.text_overlay.font_size,
                        clip_duration=item.display_duration,
                    )
                if overlaid.exists():
                    clip_path = overlaid

            # Determine transition
            is_montage = getattr(segment, "mode", "narrative") == "montage"
            if is_montage:
                # Montage: hard cuts, no transitions
                transition = "cut"
                td = 0.0
            elif item_idx == 0 and seg_idx > 0:
                # Between segments: fade_black
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
            pbar.update(1)
            if progress_callback:
                progress_callback(pbar.n, total_items, clip_name)

    pbar.close()

    if failed_clips and not skip_broken:
        raise RuntimeError(f"Failed to render {len(failed_clips)} clips: {', '.join(failed_clips)}")

    if not all_clips:
        raise RuntimeError("No clips rendered — check source files in EDL")

    # Phase 1b: Render intro/outro clips
    if edl.intro_style == "title_card" and edl.title:
        intro_path = clips_dir / "intro_title.mp4"
        if not intro_path.exists():
            _render_title_card(edl.title, edl.date_range, intro_path, w, h, fps, duration=3.0)
        if intro_path.exists():
            # Intro is the first clip — its transition is "into" itself (not used).
            # The *next* clip's transition controls the fade from intro to content.
            all_clips.insert(0, {
                "path": intro_path, "duration": 3.0,
                "transition": "cut", "transition_duration": 0.0,
            })
            # Set the first content clip to fade from intro
            if len(all_clips) > 1:
                all_clips[1]["transition"] = "fade_black"
                all_clips[1]["transition_duration"] = 1.0

    if edl.outro_style == "fade_title" and edl.title:
        outro_path = clips_dir / "outro_title.mp4"
        if not outro_path.exists():
            _render_title_card(edl.title, "", outro_path, w, h, fps, duration=3.0)
        if outro_path.exists():
            all_clips.append({
                "path": outro_path, "duration": 3.0,
                "transition": "fade_black", "transition_duration": 1.0,
            })

    # Compute clip offsets and build speech audio track
    speech_ranges: list[tuple[float, float]] = []
    speech_clips: list[tuple[float, Path]] = []  # (offset, clip_path)
    offset = 0.0
    for clip in all_clips:
        if clip.get("keep_audio"):
            speech_ranges.append((offset, offset + clip["duration"]))
            speech_clips.append((offset, clip["path"]))
        offset += clip["duration"] - clip.get("transition_duration", 0.0)

    # Phase 2: Concatenate with transitions (video only — xfade drops audio)
    print(f"Concatenating {len(all_clips)} clips...")
    no_music_path = output_dir / f"vlog_v{version}_nomix.mp4"
    _concatenate(all_clips, no_music_path)

    # Phase 2b: Build speech audio track from keep_audio clips at correct offsets
    speech_audio_path = None
    if speech_clips:
        video_dur = _probe_duration(no_music_path)
        speech_audio_path = output_dir / f"vlog_v{version}_speech.wav"
        _build_speech_track(speech_clips, video_dur, speech_audio_path)
        print(f"Speech track: {len(speech_clips)} clips merged into {speech_audio_path.name}")

    # Phase 3: Mix music + speech audio
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
        # No music but we have speech — merge speech audio into video
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

    duration = _probe_duration(output_path)
    print(f"Done: {output_path} ({duration:.1f}s)")

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
        filter_parts.append(f"[{i}:a]adelay={delay_ms}|{delay_ms}[a{i}]")

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


def _find_font() -> str:
    """Find a suitable font for title cards (cross-platform)."""
    candidates = [
        "/System/Library/Fonts/STHeiti Medium.ttc",          # macOS
        "/System/Library/Fonts/Helvetica.ttc",                # macOS fallback
        "C\\:/Windows/Fonts/segoeui.ttf",                     # Windows
        "C\\:/Windows/Fonts/arial.ttf",                       # Windows fallback
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",   # Linux
    ]
    for f in candidates:
        # Unescape for Path check
        check_path = f.replace("\\:", ":")
        if Path(check_path).exists():
            return f
    return ""  # FFmpeg will use its built-in default


def _render_title_card(
    title: str, subtitle: str, out: Path, w: int, h: int, fps: int,
    duration: float = 3.0,
) -> None:
    """Render a title card — dark background with centered text, fade in/out."""
    frames = int(duration * fps)
    safe_title = title.replace("'", "\u2019").replace(":", "\\:")
    font = _find_font()

    font_arg = f":fontfile='{font}'" if font else ""
    drawtext = (
        f"drawtext=text='{safe_title}'{font_arg}"
        f":fontsize={int(h * 0.08)}:fontcolor=white"
        f":x=(w-text_w)/2:y=(h-text_h)/2-{int(h*0.03)}"
        f":enable='between(t,0,{duration})'"
    )
    if subtitle:
        safe_sub = subtitle.replace("'", "\u2019").replace(":", "\\:")
        drawtext += (
            f",drawtext=text='{safe_sub}'{font_arg}"
            f":fontsize={int(h * 0.04)}:fontcolor=white@0.7"
            f":x=(w-text_w)/2:y=(h)/2+{int(h*0.05)}"
            f":enable='between(t,0,{duration})'"
        )

    # Fade in for first 1s, fade out for last 1s
    fade = f",fade=t=in:d=1,fade=t=out:st={duration - 1.0}:d=1"

    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c=0x1a1a2e:s={w}x{h}:d={duration}:r={fps}",
        "-vf", drawtext + fade,
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


def _render_photo(item: EditItem, out: Path, w: int, h: int, fps: int,
                   color_temp: str = "neutral") -> None:
    """Render a photo with Ken Burns effect as a video clip."""
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
            "-filter_complex", fc,
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

        # Choose scaling strategy based on aspect ratio and face detection
        ow, oh = w * 2, h * 2
        src_ratio = src_w / src_h if src_h > 0 else 1.0
        out_ratio = ow / oh

        if abs(src_ratio - out_ratio) / out_ratio < 0.05:
            # Aspect ratio close enough — just scale, no crop or pad
            scale_filter = f"scale={ow}:{oh}"
        else:
            face = _detect_face_center(source)
            if face:
                # Faces found — scale to fill, crop centered on faces
                cx, cy = face
                crop_x = f"(iw-{ow})*{cx:.3f}"
                crop_y = f"(ih-{oh})*{cy:.3f}"
                scale_filter = f"scale={ow}:{oh}:force_original_aspect_ratio=increase,crop={ow}:{oh}:{crop_x}:{crop_y}"
            else:
                # No faces — blurred background + sharp foreground (no black bars)
                scale_filter = (
                    f"split[bg][fg];"
                    f"[bg]scale={ow}:{oh}:force_original_aspect_ratio=increase,"
                    f"crop={ow}:{oh},gblur=sigma=25[blurred];"
                    f"[fg]scale={ow}:{oh}:force_original_aspect_ratio=decrease[sharp];"
                    f"[blurred][sharp]overlay=(W-w)/2:(H-h)/2"
                )

        cg = _color_grade(color_temp)
        if "[bg]" in scale_filter:
            cmd = [
                "ffmpeg", "-y", "-loop", "1", "-i", str(source),
                "-t", str(item.display_duration),
                "-filter_complex", f"{scale_filter}[comp];[comp]{zp},{cg}",
            ]
        else:
            cmd = [
                "ffmpeg", "-y", "-loop", "1", "-i", str(source),
                "-t", str(item.display_duration),
                "-vf", f"{scale_filter},{zp},{cg}",
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
                   color_temp: str = "neutral") -> None:
    """Trim and normalize a video clip. Preserves audio if keep_audio is set."""
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

    # Probe dimensions to decide portrait vs landscape
    src_w, src_h = _probe_dimensions(Path(item.source_file))
    portrait = _is_portrait(src_w, src_h)

    cg = _color_grade(color_temp)
    if portrait:
        fc = _portrait_bg_filter(w, h)
        cmd += [
            "-filter_complex", f"{fc},{cg}{speed_vf}",
            *_get_encoder(w, h, fps), "-pix_fmt", "yuv420p",
            "-r", str(fps),
        ]
    else:
        cmd += [
            "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                   f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,{cg}{speed_vf}",
            *_get_encoder(w, h, fps), "-pix_fmt", "yuv420p",
            "-r", str(fps),
        ]
    if speed_af:
        cmd += speed_af.split()
    cmd += [*audio_args, str(out)]

    result = run_subprocess(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"    Video render failed: {result.stderr[-200:]}")


def _add_text_overlay(
    input_path: Path, output_path: Path,
    text: str, position: str, font_size: int,
    clip_duration: float = 4.0,
) -> None:
    """Burn a text overlay onto a clip."""
    y_positions = {"top": "50", "center": "(h-text_h)/2", "bottom": "h-text_h-60"}
    y_expr = y_positions.get(position, y_positions["bottom"])

    # Escape special characters for drawtext, scale font for longer text
    safe_text = text.replace("'", "\u2019").replace(":", "\\:")
    if len(text) > 20:
        font_size = int(font_size * 20 / len(text))

    end_time = min(clip_duration - 0.5, 3.0)
    font = _find_font()
    font_arg = f":fontfile='{font}'" if font else ""
    vf = (
        f"drawtext=text='{safe_text}'{font_arg}"
        f":fontsize={font_size}:fontcolor=white"
        f":borderw=2:bordercolor=black"
        f":x=(w-text_w)/2:y={y_expr}"
        f":enable='between(t,0.5,{end_time:.1f})'"
    )
    cmd = [
        "ffmpeg", "-y", "-i", str(input_path),
        "-vf", vf,
        *_get_encoder(), "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        str(output_path),
    ]
    run_subprocess(cmd, capture_output=True)


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
    for clip in clips:
        inputs += ["-i", str(clip["path"])]

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
            offset = clips[0]["duration"] - td
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

        offset += clips[i]["duration"] - td

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
            f"[speech][bg]amix=inputs=2:duration=first:weights=3 1[a]"
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
            f"afade=t=out:st={fade_out_start}:d={music.fade_out}[a]"
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
    """Get video duration in seconds."""
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
        return float(result.stdout.strip())
    except ValueError:
        return 0.0
