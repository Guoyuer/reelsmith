"""Encoder detection, bitrate calculation, and media probing."""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .. import constants as C
from ..utils.media import probe_duration as _probe_duration_uncached
from ..utils.media import run_subprocess

logger = logging.getLogger("vlog.assemble.encoder")


# ---------------------------------------------------------------------------
# Bitrate & encoder detection
# ---------------------------------------------------------------------------


def target_bitrate(width: int, height: int, fps: int, quality: float = 1.0) -> str:
    """Calculate target video bitrate based on resolution, fps, and quality."""
    pixels = width * height
    base = next(mbps for threshold, mbps in C.BITRATE_TIERS if pixels >= threshold)

    if fps > 30:
        base = int(base * C.HFR_MULTIPLIER)
    base = int(base * quality)
    return f"{max(base, 1)}M"


def detect_hw_encoder(
    width: int = 3840, height: int = 2160, fps: int = 60, quality: float = 1.0
) -> list[str]:
    """Detect best hardware encoder: prefers HEVC (smaller files, same speed on GPU)."""
    import sys

    h264_br = target_bitrate(width, height, fps, quality)
    hevc_br = f"{max(int(int(h264_br.rstrip('M')) * C.HEVC_RATIO), 1)}M"

    _test_cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", "nullsrc=s=640x360:d=0.1:r=15"]

    if sys.platform == "darwin":
        try:
            test = run_subprocess(
                _test_cmd + ["-c:v", "hevc_videotoolbox", "-f", "null", "-"],
                capture_output=True,
                text=True,
            )
            if test.returncode == 0:
                return ["-c:v", "hevc_videotoolbox", "-b:v", hevc_br]
        except (OSError, subprocess.SubprocessError) as e:
            logger.debug("HEVC VideoToolbox probe failed: %s", e)
        return ["-c:v", "h264_videotoolbox", "-b:v", h264_br]

    def _try_nvenc(codec: str, bitrate: str) -> list[str] | None:
        test = run_subprocess(
            _test_cmd + ["-c:v", codec, "-f", "null", "-"],
            capture_output=True,
            text=True,
        )
        if test.returncode == 0:
            return [
                "-c:v",
                codec,
                "-preset",
                "p4",
                "-rc",
                "vbr",
                "-b:v",
                bitrate,
                "-maxrate",
                bitrate,
            ]
        return None

    try:
        result = run_subprocess(
            ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True
        )
        encoders = result.stdout or ""

        for codec, br in [("hevc_nvenc", hevc_br), ("h264_nvenc", h264_br)]:
            if codec in encoders:
                enc = _try_nvenc(codec, br)
                if enc:
                    return enc
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("HW encoder probe failed: %s", e)

    return ["-c:v", "libx264", "-preset", "fast", "-b:v", h264_br]


# ---------------------------------------------------------------------------
# RenderContext — per-run render state, passed explicitly (no globals)
# ---------------------------------------------------------------------------


@dataclass
class RenderContext:
    """Per-run render state. Created by assemble(), passed to all render modules."""

    w: int
    h: int
    fps: int
    quality: float = 1.0
    _encoder_cache: dict[tuple, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.w <= 0 or self.h <= 0:
            raise ValueError(f"Invalid resolution: {self.w}x{self.h}")
        if self.w % 2 != 0 or self.h % 2 != 0:
            raise ValueError(f"Resolution must be even: {self.w}x{self.h}")
        if self.fps <= 0 or self.fps > 120:
            raise ValueError(f"Invalid fps: {self.fps}")

    _dim_cache: dict[str, tuple[int, int]] = field(default_factory=dict)
    _dur_cache: dict[str, float] = field(default_factory=dict)

    def get_encoder(
        self,
        width: int | None = None,
        height: int | None = None,
        fps: int | None = None,
    ) -> list[str]:
        """Get encoder args, defaulting to this context's resolution. Cached."""
        w = width or self.w
        h = height or self.h
        f = fps or self.fps
        key = (w, h, f, self.quality)
        if key not in self._encoder_cache:
            self._encoder_cache[key] = detect_hw_encoder(w, h, f, self.quality)
        return self._encoder_cache[key]

    def probe_dimensions(self, path: Path) -> tuple[int, int]:
        """Return (width, height) accounting for rotation metadata.

        FFmpeg auto-rotates during decode, so a 3840x2160 video with
        rotation=-90 actually produces 2160x3840 frames.  We must
        return the *display* dimensions so is_portrait() and scale
        filters work correctly.
        """
        key = str(path)
        if key in self._dim_cache:
            return self._dim_cache[key]
        result = run_subprocess(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-show_entries",
                "stream_side_data=rotation",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
        )
        try:
            data = json.loads(result.stdout)
            stream = data["streams"][0]
            w, h = int(stream["width"]), int(stream["height"])
            # Check rotation in side_data
            for sd in stream.get("side_data_list", []):
                rot = abs(int(sd.get("rotation", 0)))
                if rot in (90, 270):
                    w, h = h, w
                    break
            dims = w, h
        except (ValueError, IndexError, KeyError):
            logger.debug("Could not probe dimensions for %s", path, exc_info=True)
            dims = 0, 0
        self._dim_cache[key] = dims
        return dims

    def invalidate(self, path: Path) -> None:
        """Remove cached probe results for a path (e.g. after re-encoding)."""
        key = str(path)
        self._dim_cache.pop(key, None)
        self._dur_cache.pop(key, None)

    def probe_duration(self, path: Path) -> float:
        key = str(path)
        if key in self._dur_cache:
            return self._dur_cache[key]
        duration = _probe_duration_uncached(path)
        self._dur_cache[key] = duration
        return duration
