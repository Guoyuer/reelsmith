"""Title card rendering for intro/outro."""

from __future__ import annotations

import logging
from pathlib import Path

from .. import constants as C
from .._types import VIDEO_EXTENSIONS
from ..utils.media import run_subprocess
from ._encoder import RenderContext
from ._filters import escape_drawtext, find_font
from ._graph import _loop_photo

logger = logging.getLogger("reelsmith.assemble.render")


def _base_encode_args(ctx: RenderContext) -> list[str]:
    """Common encoding arguments for title card rendering."""
    return [*ctx.get_encoder(), "-pix_fmt", "yuv420p", "-r", str(ctx.fps), "-an"]


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

    title_size = int(h * C.TITLE_SCALE)
    if len(title) > C.TITLE_LONG_THRESHOLD:
        title_size = int(title_size * C.TITLE_LONG_THRESHOLD / len(title))

    # Decide background: hero photo or gradient fallback
    use_photo_bg = background_photo is not None and Path(background_photo).exists()

    # HEIC works natively with the loop filter (no -loop 1 needed)

    if use_photo_bg:
        photo_bg = (
            f"scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},boxblur={C.BG_BLUR_SIGMA}:3,"
            f"eq=brightness=-0.3:saturation=0.7,vignette=PI/5"
        )
    else:
        gradient = (
            f"color=c={C.GRADIENT_START}:s={w}x{h}:d={duration}:r={fps}[bg1];"
            f"color=c={C.GRADIENT_END}:s={w}x{h // 2}:d={duration}:r={fps}[bg2];"
            f"[bg1][bg2]overlay=0:h/4:format=auto[grad]"
        )

    title_y = f"(h-text_h)/2-{int(h * 0.03)}+{int(h * 0.02)}*(1-t/{duration})"
    title_text = (
        f"drawtext=text='{safe_title}'{font_arg}"
        f":fontsize={title_size}:fontcolor=white"
        f":x=(w-text_w)/2:y={title_y}"
        f":alpha='if(lt(t,{C.FADE_IN_DURATION}),t/{C.FADE_IN_DURATION},if(gt(t,{duration - C.FADE_OUT_DURATION}),(({duration}-t)/{C.FADE_OUT_DURATION}),1))'"
    )

    line_y = int(h * C.SEPARATOR_Y_RATIO)
    line_w = int(w * C.SEPARATOR_WIDTH_RATIO)
    line_x = (w - line_w) // 2
    separator = (
        f",drawbox=x={line_x}:y={line_y}:w={line_w}:h=2"
        f":color=white@0.4:t=fill"
        f":enable='between(t,{C.FADE_OUT_DURATION},{duration - C.FADE_IN_DURATION})'"
    )

    sub_text = ""
    if subtitle:
        safe_sub = escape_drawtext(subtitle)
        sub_text = (
            f",drawtext=text='{safe_sub}'{font_arg}"
            f":fontsize={int(h * 0.035)}:fontcolor=white@0.6"
            f":x=(w-text_w)/2:y={int(h * C.SUBTITLE_Y_RATIO)}"
            f":alpha='if(lt(t,1.0),max(0,(t-0.7)/0.3),if(gt(t,{duration - C.FADE_OUT_DURATION}),(({duration}-t)/{C.FADE_OUT_DURATION}),1))'"
        )

    fade = f",fade=t=in:d={C.FADE_IN_DURATION},fade=t=out:st={duration - C.FADE_OUT_DURATION}:d={C.FADE_OUT_DURATION}"

    if use_photo_bg and background_photo is not None:
        bg_path = Path(background_photo)
        is_video = bg_path.suffix.lower() in VIDEO_EXTENSIONS
        frames = int(duration * fps)
        if is_video:
            # Extract first frame, loop it for duration
            input_args = ["-i", background_photo]
            bg_filter = f"select=eq(n\\,0),{photo_bg},loop=loop={frames}:size=1:start=0,setpts=N/{fps}/TB"
        else:
            input_args = ["-i", background_photo]
            bg_filter = f"{_loop_photo(frames, fps)},{photo_bg}"
        cmd = [
            "ffmpeg",
            "-y",
            *input_args,
            "-t",
            str(duration),
            "-filter_complex",
            f"{bg_filter}[bg];[bg]{title_text}{separator}{sub_text}{fade}",
            *_base_encode_args(ctx),
            str(output_path),
        ]
    else:
        cmd = [
            "ffmpeg",
            "-y",
            "-filter_complex",
            f"{gradient};[grad]{title_text}{separator}{sub_text}{fade}",
            *_base_encode_args(ctx),
            str(output_path),
        ]
    result = run_subprocess(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Title card render failed: {result.stderr}")
