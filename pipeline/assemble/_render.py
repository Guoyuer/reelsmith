"""Title card rendering for intro/outro."""

from __future__ import annotations

import logging
from pathlib import Path

from ..utils.media import run_subprocess
from ._encoder import RenderContext
from ._filters import escape_drawtext, find_font

logger = logging.getLogger("vlog.assemble.render")


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

    title_size = int(h * 0.08)
    if len(title) > 25:
        title_size = int(title_size * 25 / len(title))

    # Decide background: hero photo or gradient fallback
    use_photo_bg = background_photo is not None and Path(background_photo).exists()

    if use_photo_bg:
        photo_bg = (
            f"scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},gblur=sigma=40,"
            f"eq=brightness=-0.3:saturation=0.7,vignette=PI/5"
        )
    else:
        gradient = (
            f"color=c=0x0f0c29:s={w}x{h}:d={duration}:r={fps}[bg1];"
            f"color=c=0x302b63:s={w}x{h // 2}:d={duration}:r={fps}[bg2];"
            f"[bg1][bg2]overlay=0:h/4:format=auto[grad]"
        )

    title_y = f"(h-text_h)/2-{int(h * 0.03)}+{int(h * 0.02)}*(1-t/{duration})"
    title_text = (
        f"drawtext=text='{safe_title}'{font_arg}"
        f":fontsize={title_size}:fontcolor=white"
        f":x=(w-text_w)/2:y={title_y}"
        f":alpha='if(lt(t,0.5),t/0.5,if(gt(t,{duration - 0.8}),(({duration}-t)/0.8),1))'"
    )

    line_y = int(h * 0.55)
    line_w = int(w * 0.15)
    line_x = (w - line_w) // 2
    separator = (
        f",drawbox=x={line_x}:y={line_y}:w={line_w}:h=2"
        f":color=white@0.4:t=fill"
        f":enable='between(t,0.8,{duration - 0.5})'"
    )

    sub_text = ""
    if subtitle:
        safe_sub = escape_drawtext(subtitle)
        sub_text = (
            f",drawtext=text='{safe_sub}'{font_arg}"
            f":fontsize={int(h * 0.035)}:fontcolor=white@0.6"
            f":x=(w-text_w)/2:y={int(h * 0.59)}"
            f":alpha='if(lt(t,1.0),max(0,(t-0.7)/0.3),if(gt(t,{duration - 0.8}),(({duration}-t)/0.8),1))'"
        )

    fade = f",fade=t=in:d=0.5,fade=t=out:st={duration - 0.8}:d=0.8"

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
