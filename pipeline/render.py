"""Clip rendering: photo, video, and title card to normalized MP4 clips."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from .edl import EditItem
from .encoder import RenderContext, is_portrait
from .filters import (
    build_portrait_photo_filter, color_grade, drawtext_filter, find_font,
    zoompan_filter, portrait_bg_filter,
)
from .image_utils import convert_heic
from .media_utils import run_subprocess

logger = logging.getLogger("vlog.render")


def render_photo(item: EditItem, output_path: Path, w: int, h: int, fps: int, *,
                 ctx: RenderContext,
                 color_temp: str = "neutral",
                 text_overlay=None, language: str = "en") -> None:
    """Render a photo with Ken Burns effect as a video clip. Text overlay baked in."""
    source = Path(item.source_file)

    if source.suffix.lower() in {".heic", ".heif"}:
        source = convert_heic(source)  # raises RuntimeError on failure

    frames = int(item.display_duration * fps)
    zoom_targets = {
        "ken_burns_in": 0.25,
        "ken_burns_out": 0.20,
        "ken_burns_left": 0.15,
        "ken_burns_right": 0.15,
        "static": 0.0,
    }
    if item.effect not in zoom_targets:
        logger.warning("Unknown photo effect '%s', defaulting to ken_burns_in", item.effect)
    target = zoom_targets.get(item.effect, 0.25)
    variation = (int(hashlib.md5(item.source_file.encode()).hexdigest()[:4], 16) % 10) / 100
    zoom_rate = 0.001 + ((target + variation) / frames) if target > 0 else 0

    dt = ""
    if text_overlay:
        dt = "," + drawtext_filter(text_overlay.text, text_overlay.position,
                                   text_overlay.font_size, item.display_duration, language, out_h=h)

    src_w, src_h = ctx.probe_dimensions(source)
    portrait = is_portrait(src_w, src_h)
    enc = ctx.get_encoder(w, h, fps)

    if portrait:
        portrait_zoom_rate = 0.001 + (0.08 / frames)
        fc = build_portrait_photo_filter(w, h, frames, fps, portrait_zoom_rate)
        cmd = [
            "ffmpeg", "-y", "-loop", "1", "-i", str(source),
            "-t", str(item.display_duration),
            "-filter_complex", f"{fc}{dt}",
            *enc, "-pix_fmt", "yuv420p",
            "-an",
            str(output_path),
        ]
    else:
        direction_map = {
            "ken_burns_in": "in", "ken_burns_out": "out",
            "ken_burns_left": "left", "ken_burns_right": "right",
            "static": "static",
        }
        direction = direction_map.get(item.effect, "in")  # already warned above
        zp = zoompan_filter(zoom_rate, frames, w, h, fps, direction=direction)

        ow, oh = w * 2, h * 2
        src_ratio = src_w / src_h if src_h > 0 else 1.0
        out_ratio = ow / oh

        if abs(src_ratio - out_ratio) / out_ratio < 0.05:
            scale_filter = f"scale={ow}:{oh}"
        else:
            scale_filter = (
                f"split[bg][fg];"
                f"[bg]scale={ow}:{oh}:force_original_aspect_ratio=increase,"
                f"crop={ow}:{oh},gblur=sigma=25[blurred];"
                f"[fg]scale={ow}:{oh}:force_original_aspect_ratio=decrease[sharp];"
                f"[blurred][sharp]overlay=(W-w)/2:(H-h)/2"
            )

        cg = color_grade(color_temp)
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
            *enc, "-pix_fmt", "yuv420p",
            "-an",
            str(output_path),
        ]

    result = run_subprocess(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Photo render failed ({item.source_file}): {result.stderr[-300:]}")


def render_video(item: EditItem, output_path: Path, w: int, h: int, fps: int, *,
                 ctx: RenderContext,
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

    speed = item.playback_speed
    speed_vf = f",setpts={1/speed:.4f}*PTS" if speed != 1.0 else ""
    speed_af = ["-af", f"atempo={speed}"] if speed != 1.0 and item.keep_audio else []

    dt = ""
    if text_overlay:
        dt = "," + drawtext_filter(text_overlay.text, text_overlay.position,
                                   text_overlay.font_size, item.display_duration, language, out_h=h)

    src_w, src_h = ctx.probe_dimensions(Path(item.source_file))
    portrait = is_portrait(src_w, src_h)
    enc = ctx.get_encoder(w, h, fps)

    cg = color_grade(color_temp)
    if portrait:
        fc = portrait_bg_filter(w, h)
        cmd += [
            "-filter_complex", f"{fc},{cg}{speed_vf}{dt}",
            *enc, "-pix_fmt", "yuv420p",
            "-r", str(fps),
        ]
    else:
        cmd += [
            "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                   f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,{cg}{speed_vf}{dt}",
            *enc, "-pix_fmt", "yuv420p",
            "-r", str(fps),
        ]
    cmd += [*speed_af, *audio_args, str(output_path)]

    result = run_subprocess(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Video render failed ({item.source_file}): {result.stderr[-300:]}")


def render_title_card(
    title: str, subtitle: str, output_path: Path, w: int, h: int, fps: int, *,
    ctx: RenderContext,
    duration: float = 3.0, language: str = "en",
    background_photo: str | None = None,
) -> None:
    """Render a professional title card with gradient background and animated text.

    If *background_photo* is provided and the file exists, the gradient is replaced
    with a heavily blurred, darkened, vignetted version of the photo.
    """
    safe_title = title.replace("'", "\u2019").replace(":", "\\:")
    font = find_font(language)
    font_arg = f":fontfile='{font}'" if font else ""

    title_size = int(h * 0.08)
    if len(title) > 25:
        title_size = int(title_size * 25 / len(title))

    # Decide background: hero photo or gradient fallback
    use_photo_bg = (
        background_photo is not None and Path(background_photo).exists()
    )

    if use_photo_bg:
        photo_bg = (
            f"scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},gblur=sigma=40,"
            f"eq=brightness=-0.3:saturation=0.7,vignette=PI/5"
        )
    else:
        gradient = (
            f"color=c=0x0f0c29:s={w}x{h}:d={duration}:r={fps}[bg1];"
            f"color=c=0x302b63:s={w}x{h//2}:d={duration}:r={fps}[bg2];"
            f"[bg1][bg2]overlay=0:h/4:format=auto[grad]"
        )

    title_y = f"(h-text_h)/2-{int(h*0.03)}+{int(h*0.02)}*(1-t/{duration})"
    title_text = (
        f"drawtext=text='{safe_title}'{font_arg}"
        f":fontsize={title_size}:fontcolor=white"
        f":x=(w-text_w)/2:y={title_y}"
        f":alpha='if(lt(t,0.5),t/0.5,if(gt(t,{duration-0.8}),(({duration}-t)/0.8),1))'"
    )

    line_y = int(h * 0.55)
    line_w = int(w * 0.15)
    line_x = (w - line_w) // 2
    separator = (
        f",drawbox=x={line_x}:y={line_y}:w={line_w}:h=2"
        f":color=white@0.4:t=fill"
        f":enable='between(t,0.8,{duration-0.5})'"
    )

    sub_text = ""
    if subtitle:
        safe_sub = subtitle.replace("'", "\u2019").replace(":", "\\:")
        sub_text = (
            f",drawtext=text='{safe_sub}'{font_arg}"
            f":fontsize={int(h * 0.035)}:fontcolor=white@0.6"
            f":x=(w-text_w)/2:y={int(h*0.59)}"
            f":alpha='if(lt(t,1.0),max(0,(t-0.7)/0.3),if(gt(t,{duration-0.8}),(({duration}-t)/0.8),1))'"
        )

    fade = f",fade=t=in:d=0.5,fade=t=out:st={duration - 0.8}:d=0.8"

    enc = ctx.get_encoder(w, h, fps)

    if use_photo_bg:
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-i", background_photo,
            "-t", str(duration),
            "-filter_complex",
            f"{photo_bg}[bg];[bg]{title_text}{separator}{sub_text}{fade}",
            *enc, "-pix_fmt", "yuv420p",
            "-an",
            str(output_path),
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-filter_complex",
            f"{gradient};[grad]{title_text}{separator}{sub_text}{fade}",
            *enc, "-pix_fmt", "yuv420p",
            "-an",
            str(output_path),
        ]
    run_subprocess(cmd, capture_output=True, text=True)
