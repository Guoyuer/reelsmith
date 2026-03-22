"""Encoder detection, bitrate calculation, and media probing."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .media_utils import run_subprocess

logger = logging.getLogger("vlog.encoder")


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
    """Detect best hardware encoder: prefers HEVC (smaller files, same speed on GPU)."""
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
        except (OSError, subprocess.SubprocessError) as e:
            logger.debug("HEVC VideoToolbox probe failed: %s", e)
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
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("HW encoder probe failed: %s", e)

    return ["-c:v", "libx264", "-preset", "fast", "-b:v", h264_br]


# ---------------------------------------------------------------------------
# RenderContext — replaces scattered module-level globals
# ---------------------------------------------------------------------------

@dataclass
class RenderContext:
    """Per-run render state. Created by assemble(), used by all render modules."""
    quality: float = 1.0
    _encoder_cache: dict[tuple, list[str]] = field(default_factory=dict)
    _dim_cache: dict[str, tuple[int, int]] = field(default_factory=dict)
    _dur_cache: dict[str, float] = field(default_factory=dict)

    def get_encoder(self, width: int = 3840, height: int = 2160, fps: int = 60) -> list[str]:
        key = (width, height, fps, self.quality)
        if key not in self._encoder_cache:
            self._encoder_cache[key] = detect_hw_encoder(width, height, fps, self.quality)
        return self._encoder_cache[key]

    def probe_dimensions(self, path: Path) -> tuple[int, int]:
        key = str(path)
        if key in self._dim_cache:
            return self._dim_cache[key]
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
        self._dim_cache[key] = dims
        return dims

    def probe_duration(self, path: Path) -> float:
        key = str(path)
        if key in self._dur_cache:
            return self._dur_cache[key]
        result = run_subprocess(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True,
        )
        try:
            dur = float(result.stdout.strip().split("\n")[0])
        except (ValueError, IndexError):
            dur = 0.0
        self._dur_cache[key] = dur
        return dur


# Module-level context — set by init_context(), read by module-level functions.
_ctx = RenderContext()


def init_context(quality: float = 1.0) -> RenderContext:
    """Create a fresh RenderContext for a new assemble run."""
    global _ctx
    _ctx = RenderContext(quality=quality)
    return _ctx


def get_context() -> RenderContext:
    """Get the current RenderContext."""
    return _ctx


# ---------------------------------------------------------------------------
# Module-level convenience functions (delegate to _ctx)
# ---------------------------------------------------------------------------

def get_encoder(width: int = 3840, height: int = 2160, fps: int = 60) -> list[str]:
    return _ctx.get_encoder(width, height, fps)


def probe_duration(path: Path) -> float:
    return _ctx.probe_duration(path)


def is_portrait(src_w: int, src_h: int) -> bool:
    """Return True if the source is clearly portrait (height > width * 1.2)."""
    return src_w > 0 and src_h > src_w * 1.2
