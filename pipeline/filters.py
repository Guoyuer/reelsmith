"""FFmpeg filter string builders: color grade, text overlay, portrait, fonts."""

from __future__ import annotations

from pathlib import Path

from .filtergraph import FilterGraph


def build_portrait_photo_filter(
    out_w: int, out_h: int, frames: int, fps: int, zoom_rate: float,
) -> str:
    """Build FFmpeg filter_complex for portrait photos: blurred BG + sharp FG + gentle Ken Burns."""
    fg = FilterGraph()
    fg.add("split", inputs=["0:v"], output="bg] [fg")
    fg.add_raw(
        f"scale=960:-1:force_original_aspect_ratio=increase,crop=960:540,"
        f"gblur=sigma=20,scale={out_w}:{out_h}",
        inputs=["bg"], output="blurred",
    )
    fg.add_raw(f"scale=-1:{out_h}", inputs=["fg"], output="sharp")
    fg.add("overlay", {"x": "(W-w)/2", "y": "(H-h)/2"},
           inputs=["blurred", "sharp"], output="comp")
    fg.add_raw(
        f"zoompan=z='min(zoom+{zoom_rate:.6f},1.08)':d={frames}"
        f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":s={out_w}x{out_h}:fps={fps}",
        inputs=["comp"], output="",  # no output label — FFmpeg auto-connects to output
    )
    return fg.compile()


def color_grade(color_temp: str = "neutral") -> str:
    """Subtle color grade with optional temperature shift."""
    base = "eq=contrast=1.02:brightness=0.01:saturation=1.05"
    if color_temp == "warm":
        return f"{base},colorbalance=rs=0.02:gs=0.01:bs=-0.02"
    elif color_temp == "cool":
        return f"{base},colorbalance=rs=-0.02:gs=0.0:bs=0.02"
    return base


def find_font(language: str = "en") -> str:
    """Find a suitable font for title cards (cross-platform, CJK-aware)."""
    needs_cjk = language in ("cn", "both")
    if needs_cjk:
        candidates = [
            "/System/Library/Fonts/STHeiti Medium.ttc",
            "/System/Library/Fonts/PingFang.ttc",
            "C\\:/Windows/Fonts/msyh.ttc",
            "C\\:/Windows/Fonts/simhei.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/droid/DroidSansFallback.ttf",
        ]
    else:
        candidates = [
            "/System/Library/Fonts/Helvetica.ttc",
            "C\\:/Windows/Fonts/segoeui.ttf",
            "C\\:/Windows/Fonts/arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    for f in candidates:
        check_path = f.replace("\\:", ":")
        if Path(check_path).exists():
            return f
    return ""


def drawtext_filter(text: str, position: str, font_size: int,
                    clip_duration: float, language: str = "en") -> str:
    """Build a drawtext filter string for text overlay (no leading comma)."""
    y_positions = {"top": "50", "center": "(h-text_h)/2", "bottom": "h-text_h-60"}
    y_expr = y_positions.get(position, y_positions["bottom"])
    safe_text = text.replace("'", "\u2019").replace(":", "\\:")
    if len(text) > 20:
        font_size = int(font_size * 20 / len(text))
    end_time = min(clip_duration - 0.5, 3.0)
    font = find_font(language)
    font_arg = f":fontfile='{font}'" if font else ""
    return (
        f"drawtext=text='{safe_text}'{font_arg}"
        f":fontsize={font_size}:fontcolor=white"
        f":borderw=2:bordercolor=black"
        f":x=(w-text_w)/2:y={y_expr}"
        f":enable='between(t,0.5,{end_time:.1f})'"
    )
