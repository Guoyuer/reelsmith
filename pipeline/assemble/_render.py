"""Title card rendering for intro/outro."""

from __future__ import annotations

import logging
from pathlib import Path

from ..utils.media import run_subprocess
from ._encoder import RenderContext
from ._filters import escape_drawtext, find_font

logger = logging.getLogger("vlog.assemble.render")

_TITLE_SCALE = 0.08  # title font size as fraction of output height
_TITLE_LONG_THRESHOLD = 25  # characters; reduce size above this
_SUBTITLE_Y_RATIO = 0.59  # subtitle vertical position as fraction of height
_SEPARATOR_WIDTH_RATIO = 0.15  # separator line width as fraction of output width
_SEPARATOR_Y_RATIO = 0.55  # separator Y position as fraction of height
_GRADIENT_START = "0x0f0c29"  # fallback gradient dark purple
_GRADIENT_END = "0x302b63"  # fallback gradient lighter purple
_BG_BLUR_SIGMA = 40  # blur for photo background
_FADE_IN_DURATION = 0.5  # seconds
_FADE_OUT_DURATION = 0.8  # seconds


def render_title_card(
    title: str,
    subtitle: str,
    output_path: Path,
    *,
    ctx: RenderContext,
    duration: float = 3.0,
    language: str = "en",
    background_photo: str | None = None,
) -> None:
    """Render a professional title card with gradient background and animated text.

    If *background_photo* is provided and the file exists, the gradient is replaced
    with a heavily blurred, darkened, vignetted version of the photo.
    """
    w, h, fps = ctx.w, ctx.h, ctx.fps
    safe_title = escape_drawtext(title)
    font = find_font(language)
    font_arg = f":fontfile='{font}'" if font else ""

    title_size = int(h * _TITLE_SCALE)
    if len(title) > _TITLE_LONG_THRESHOLD:
        title_size = int(title_size * _TITLE_LONG_THRESHOLD / len(title))

    # Decide background: hero photo or gradient fallback
    use_photo_bg = background_photo is not None and Path(background_photo).exists()

    if use_photo_bg:
        photo_bg = (
            f"scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},gblur=sigma={_BG_BLUR_SIGMA},"
            f"eq=brightness=-0.3:saturation=0.7,vignette=PI/5"
        )
    else:
        gradient = (
            f"color=c={_GRADIENT_START}:s={w}x{h}:d={duration}:r={fps}[bg1];"
            f"color=c={_GRADIENT_END}:s={w}x{h // 2}:d={duration}:r={fps}[bg2];"
            f"[bg1][bg2]overlay=0:h/4:format=auto[grad]"
        )

    title_y = f"(h-text_h)/2-{int(h * 0.03)}+{int(h * 0.02)}*(1-t/{duration})"
    title_text = (
        f"drawtext=text='{safe_title}'{font_arg}"
        f":fontsize={title_size}:fontcolor=white"
        f":x=(w-text_w)/2:y={title_y}"
        f":alpha='if(lt(t,{_FADE_IN_DURATION}),t/{_FADE_IN_DURATION},if(gt(t,{duration - _FADE_OUT_DURATION}),(({duration}-t)/{_FADE_OUT_DURATION}),1))'"
    )

    line_y = int(h * _SEPARATOR_Y_RATIO)
    line_w = int(w * _SEPARATOR_WIDTH_RATIO)
    line_x = (w - line_w) // 2
    separator = (
        f",drawbox=x={line_x}:y={line_y}:w={line_w}:h=2"
        f":color=white@0.4:t=fill"
        f":enable='between(t,{_FADE_OUT_DURATION},{duration - _FADE_IN_DURATION})'"
    )

    sub_text = ""
    if subtitle:
        safe_sub = escape_drawtext(subtitle)
        sub_text = (
            f",drawtext=text='{safe_sub}'{font_arg}"
            f":fontsize={int(h * 0.035)}:fontcolor=white@0.6"
            f":x=(w-text_w)/2:y={int(h * _SUBTITLE_Y_RATIO)}"
            f":alpha='if(lt(t,1.0),max(0,(t-0.7)/0.3),if(gt(t,{duration - _FADE_OUT_DURATION}),(({duration}-t)/{_FADE_OUT_DURATION}),1))'"
        )

    fade = f",fade=t=in:d={_FADE_IN_DURATION},fade=t=out:st={duration - _FADE_OUT_DURATION}:d={_FADE_OUT_DURATION}"

    enc = ctx.get_encoder()

    if use_photo_bg:
        cmd = [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-framerate",
            str(fps),
            "-i",
            background_photo,
            "-t",
            str(duration),
            "-filter_complex",
            f"{photo_bg}[bg];[bg]{title_text}{separator}{sub_text}{fade}",
            *enc,
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(fps),
            "-an",
            str(output_path),
        ]
    else:
        cmd = [
            "ffmpeg",
            "-y",
            "-filter_complex",
            f"{gradient};[grad]{title_text}{separator}{sub_text}{fade}",
            *enc,
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(fps),
            "-an",
            str(output_path),
        ]
    result = run_subprocess(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Title card render failed: {result.stderr}")
