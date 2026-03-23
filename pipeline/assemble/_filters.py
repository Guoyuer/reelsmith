"""FFmpeg filter string builders: color grade, text overlay, portrait, fonts, Ken Burns."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("vlog.assemble.filters")


def build_portrait_photo_filter(
    out_w: int, out_h: int, frames: int, fps: int, zoom_rate: float,
) -> str:
    """Build FFmpeg filter_complex for portrait photos: blurred BG + sharp FG + gentle Ken Burns."""
    return (
        f"[0:v]split[bg][fg];"
        f"[bg]scale=960:-1:force_original_aspect_ratio=increase,crop=960:540,"
        f"gblur=sigma=60,scale={out_w}:{out_h},eq=brightness=-0.15:saturation=0.6[blurred];"
        f"[fg]scale=-1:{out_h}[sharp];"
        f"[blurred][sharp]overlay=(W-w)/2:(H-h)/2[comp];"
        f"[comp]zoompan=z='1+(1.08-1)*(1-cos(PI*on/{frames}))/2':d={frames}"
        f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":s={out_w}x{out_h}:fps={fps}"
    )


_VALID_COLOR_TEMPS = {"neutral", "warm", "cool"}


def color_grade(color_temp: str = "neutral") -> str:
    """Subtle color grade with optional temperature shift."""
    if color_temp not in _VALID_COLOR_TEMPS:
        logger.warning("Unknown color_temp '%s', defaulting to neutral", color_temp)
        color_temp = "neutral"
    base = "eq=contrast=1.02:brightness=0.01:saturation=1.05"
    if color_temp == "warm":
        return f"{base},colorbalance=rs=0.02:gs=0.01:bs=-0.02"
    elif color_temp == "cool":
        return f"{base},colorbalance=rs=-0.02:gs=0.0:bs=0.02"
    return base


def find_font(language: str = "en") -> str:
    """Find a suitable font for title cards (cross-platform, CJK-aware).

    Returns the path with FFmpeg drawtext escaping (colon → \\:).
    """
    needs_cjk = language in ("cn", "both")
    if needs_cjk:
        candidates = [
            "/System/Library/Fonts/STHeiti Medium.ttc",
            "/System/Library/Fonts/PingFang.ttc",
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simhei.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/droid/DroidSansFallback.ttf",
        ]
    else:
        candidates = [
            "/System/Library/Fonts/Helvetica.ttc",
            "C:/Windows/Fonts/segoeui.ttf",
            "C:/Windows/Fonts/arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    for f in candidates:
        if Path(f).exists():
            return f.replace(":", "\\:")  # escape for FFmpeg drawtext
    return ""


def drawtext_filter(text: str, position: str, font_size: int,
                    clip_duration: float, language: str = "en",
                    out_h: int = 0) -> str:
    """Build a drawtext filter string for text overlay (no leading comma)."""
    y_positions = {"top": "50", "center": "(h-text_h)/2", "bottom": "h-text_h-60"}
    y_expr = y_positions.get(position, y_positions["bottom"])
    safe_text = text.replace("'", "\u2019").replace(":", "\\:")
    # Scale font to output height; base=48 at 1080p
    if out_h > 0:
        font_size = max(font_size, int(out_h * 0.055))
    if len(text) > 20:
        font_size = int(font_size * 20 / len(text))
    end_time = min(clip_duration - 0.5, 3.0)
    font = find_font(language)
    font_arg = f":fontfile='{font}'" if font else ""
    return (
        f"drawtext=text='{safe_text}'{font_arg}"
        f":fontsize={font_size}:fontcolor=white"
        f":shadowcolor=black@0.6:shadowx=3:shadowy=3"
        f":x=(w-text_w)/2:y={y_expr}"
        f":enable='between(t,0.5,{end_time:.1f})'"
    )


# ---------------------------------------------------------------------------
# Ken Burns & portrait video filters
# ---------------------------------------------------------------------------

def zoompan_filter(
    zoom_rate: float, frames: int, w: int, h: int, fps: int,
    direction: str = "in",
) -> str:
    """Build a Ken Burns zoompan filter expression.

    *direction*: ``"in"``, ``"out"``, ``"left"``, ``"right"``, or ``"static"``.
    """
    center = f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
    tail = f":s={w}x{h}:fps={fps}"

    zoom_exprs = {
        "in":     f"z='1+(1.3-1)*(1-cos(PI*on/{frames}))/2':d={frames}:{center}",
        "out":    f"z='1.3-(1.3-1)*(1-cos(PI*on/{frames}))/2':d={frames}:{center}",
        "left":   f"z='1.15':d={frames}:x='(iw-iw/zoom)*(1-cos(PI*on/{frames}))/2':y='ih/2-(ih/zoom/2)'",
        "right":  f"z='1.15':d={frames}:x='(iw-iw/zoom)*(1-(1-cos(PI*on/{frames}))/2)':y='ih/2-(ih/zoom/2)'",
        "static": f"z='1':d={frames}",
    }
    return f"zoompan={zoom_exprs.get(direction, zoom_exprs['in'])}{tail}"


def portrait_bg_filter(w: int, h: int) -> str:
    """Build the blurred-background + sharp-foreground overlay filter for portrait videos.

    The result is a ``filter_complex`` string suitable for a single-input
    FFmpeg command (expects ``[0:v]`` as input).
    """
    return (
        f"[0:v]split[bg][fg];"
        f"[bg]scale={w}:-1:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},gblur=sigma=60,eq=brightness=-0.15:saturation=0.6[blurred];"
        f"[fg]scale=-1:{h}[sharp];"
        f"[blurred][sharp]overlay=(W-w)/2:(H-h)/2"
    )


def is_portrait(src_w: int, src_h: int) -> bool:
    """Return True if the source is clearly portrait (height > width * 1.2)."""
    return src_w > 0 and src_h > src_w * 1.2
