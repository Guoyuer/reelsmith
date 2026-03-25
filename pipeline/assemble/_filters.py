"""FFmpeg filter string builders: color grade, text overlay, fonts, Ken Burns."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("vlog.assemble.filters")


_VALID_COLOR_TEMPS = {"neutral", "warm", "cool"}


def escape_drawtext(text: str) -> str:
    """Escape text for FFmpeg drawtext filter (handles : [ ] = ' \\)."""
    return (
        text.replace("\\", "\\\\")
        .replace("'", "\u2019")
        .replace(":", "\\:")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("=", "\\=")
    )


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


def drawtext_filter(
    text: str,
    position: str,
    font_size: int,
    clip_duration: float,
    language: str = "en",
    out_h: int = 0,
) -> str:
    """Build a drawtext filter string for text overlay (no leading comma)."""
    y_positions = {"top": "50", "center": "(h-text_h)/2", "bottom": "h-text_h-60"}
    y_expr = y_positions.get(position, y_positions["bottom"])
    safe_text = escape_drawtext(text)
    # Scale font to output height; base=48 at 1080p
    if out_h > 0:
        font_size = max(font_size, int(out_h * 0.055))
    if len(text) > 20:
        font_size = int(font_size * 20 / len(text))
    end_time = max(1.0, clip_duration * 0.6)
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
# Ken Burns
# ---------------------------------------------------------------------------


def ken_burns_filter(
    frames: int,
    w: int,
    h: int,
    fps: int,
    direction: str = "in",
) -> str:
    """Ken Burns via crop + lanczos scale.  No zoompan (bilinear-only).

    Input must be larger than *w* x *h* (typically 2x for zoom headroom).
    The crop selects an animated region; lanczos downscales to output.

    *direction*: ``"in"``, ``"out"``, ``"left"``, ``"right"``, or ``"static"``.
    """
    # Zoom expressions (cosine-eased), evaluated per frame via 'n'
    zoom_map = {
        "in": f"1+0.3*(1-cos(PI*n/{frames}))/2",
        "out": f"1.3-0.3*(1-cos(PI*n/{frames}))/2",
        "left": "1.15",
        "right": "1.15",
        "static": "1",
    }
    z = zoom_map.get(direction, zoom_map["in"])

    cw = f"trunc(iw/({z})/2)*2"
    ch = f"trunc(ih/({z})/2)*2"

    # Pan expressions
    if direction == "left":
        cx = f"(iw-ow)*(1-cos(PI*n/{frames}))/2"
    elif direction == "right":
        cx = f"(iw-ow)*(1-(1-cos(PI*n/{frames}))/2)"
    else:
        cx = "(iw-ow)/2"
    cy = "(ih-oh)/2"

    return (
        f"crop=w='{cw}':h='{ch}':x='{cx}':y='{cy}':exact=1,"
        f"scale={w}:{h}:flags=lanczos,fps={fps}"
    )


def is_portrait(src_w: int, src_h: int) -> bool:
    """Return True if the source is clearly portrait (height > width * 1.2)."""
    return src_w > 0 and src_h > src_w * 1.2
