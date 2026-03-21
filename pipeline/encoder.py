"""Encoder detection, bitrate calculation, and media probing."""

from __future__ import annotations

from pathlib import Path

from .media_utils import run_subprocess


# ---------------------------------------------------------------------------
# Bitrate & encoder detection
# ---------------------------------------------------------------------------

def target_bitrate(width: int, height: int, fps: int, quality: float = 1.0) -> str:
    """Calculate target video bitrate based on resolution, fps, and quality."""
    pixels = width * height
    if pixels >= 3840 * 2160:
        base = 45
    elif pixels >= 2560 * 1440:
        base = 16
    elif pixels >= 1920 * 1080:
        base = 8
    elif pixels >= 1280 * 720:
        base = 5
    else:
        base = 3

    if fps > 30:
        base = int(base * 1.5)
    base = int(base * quality)
    return f"{max(base, 1)}M"


def detect_hw_encoder(width: int = 3840, height: int = 2160, fps: int = 60,
                      quality: float = 1.0) -> list[str]:
    """Detect best hardware encoder: prefers HEVC (smaller files, same speed on GPU).

    Tries: hevc_nvenc -> h264_nvenc -> hevc_videotoolbox -> h264_videotoolbox -> libx264.
    """
    import sys
    h264_br = target_bitrate(width, height, fps, quality)
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

        if "hevc_nvenc" in encoders:
            test = run_subprocess(_test_cmd + ["-c:v", "hevc_nvenc", "-f", "null", "-"],
                                  capture_output=True, text=True)
            if test.returncode == 0:
                return ["-c:v", "hevc_nvenc", "-preset", "p4",
                        "-rc", "vbr", "-b:v", hevc_br, "-maxrate", hevc_br]

        if "h264_nvenc" in encoders:
            test = run_subprocess(_test_cmd + ["-c:v", "h264_nvenc", "-f", "null", "-"],
                                  capture_output=True, text=True)
            if test.returncode == 0:
                return ["-c:v", "h264_nvenc", "-preset", "p4",
                        "-rc", "vbr", "-b:v", h264_br, "-maxrate", h264_br]
    except Exception:
        pass

    return ["-c:v", "libx264", "-preset", "fast", "-b:v", h264_br]


# Cache per (width, height, fps, quality) so bitrate changes with settings
_HW_ENCODER_CACHE: dict[tuple, list[str]] = {}
_QUALITY: float = 1.0  # Set by assemble() before rendering
# Probe caches — cleared at start of each assemble run
_PROBE_DIM_CACHE: dict[str, tuple[int, int]] = {}
_PROBE_DUR_CACHE: dict[str, float] = {}


def get_encoder(width: int = 3840, height: int = 2160, fps: int = 60) -> list[str]:
    """Get cached hardware encoder args for the given resolution/fps/quality."""
    key = (width, height, fps, _QUALITY)
    if key not in _HW_ENCODER_CACHE:
        _HW_ENCODER_CACHE[key] = detect_hw_encoder(width, height, fps, _QUALITY)
    return _HW_ENCODER_CACHE[key]


def set_quality(q: float) -> None:
    """Set the global quality multiplier (called by assemble at start)."""
    global _QUALITY
    _QUALITY = q


def clear_caches() -> None:
    """Clear all probe caches (called at start of each assemble run)."""
    _PROBE_DIM_CACHE.clear()
    _PROBE_DUR_CACHE.clear()


# ---------------------------------------------------------------------------
# Media probing
# ---------------------------------------------------------------------------

def probe_dimensions(path: Path) -> tuple[int, int]:
    """Use ffprobe to get (width, height) of a media file. Cached."""
    key = str(path)
    if key in _PROBE_DIM_CACHE:
        return _PROBE_DIM_CACHE[key]
    result = run_subprocess(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x",
         str(path)],
        capture_output=True, text=True,
    )
    try:
        parts = result.stdout.strip().split("x")
        dims = int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        dims = 0, 0
    _PROBE_DIM_CACHE[key] = dims
    return dims


def probe_duration(path: Path) -> float:
    """Get video/audio duration in seconds. Cached."""
    key = str(path)
    if key in _PROBE_DUR_CACHE:
        return _PROBE_DUR_CACHE[key]
    result = run_subprocess(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    try:
        dur = float(result.stdout.strip().split("\n")[0])
    except (ValueError, IndexError):
        dur = 0.0
    _PROBE_DUR_CACHE[key] = dur
    return dur


def is_portrait(src_w: int, src_h: int) -> bool:
    """Return True if the source is clearly portrait (height > width * 1.2)."""
    return src_w > 0 and src_h > src_w * 1.2
