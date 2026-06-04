"""Encoder detection, bitrate calculation, and media probing."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .. import constants as C
from ..utils.media import probe_duration as _probe_duration_uncached
from ..utils.media import run_subprocess

logger = logging.getLogger("reelsmith.assemble.encoder")


# ---------------------------------------------------------------------------
# Bitrate & encoder detection
# ---------------------------------------------------------------------------


def target_bitrate(width: int, height: int, fps: int, bitrate: float = 1.0) -> str:
    """Calculate target video bitrate based on resolution, fps, and multiplier."""
    pixels = width * height
    base = next(mbps for threshold, mbps in C.BITRATE_TIERS if pixels >= threshold)

    if fps > 30:
        base = int(base * C.HFR_MULTIPLIER)
    base = int(base * bitrate)
    return f"{max(base, 1)}M"


def _bitrate_for_codec(
    codec_family: str, width: int, height: int, fps: int, bitrate: float
) -> str:
    """Calculate target bitrate scaled by codec efficiency."""
    h264_br = target_bitrate(width, height, fps, bitrate)
    h264_mbps = int(h264_br.rstrip("M"))
    if codec_family == "av1":
        return f"{max(int(h264_mbps * C.AV1_RATIO), 1)}M"
    if codec_family == "hevc":
        return f"{max(int(h264_mbps * C.HEVC_RATIO), 1)}M"
    return h264_br


# Valid values for the ``codec`` parameter in :func:`detect_hw_encoder`.
CODEC_CHOICES = ("auto", "av1", "hevc", "h264")


def detect_hw_encoder(
    width: int = 3840,
    height: int = 2160,
    fps: int = 60,
    bitrate: float = 1.0,
    codec: str = "auto",
) -> list[str]:
    """Detect best hardware encoder, optionally constrained by *codec*.

    *codec* values: ``"auto"`` (best available, HEVC preferred), ``"av1"``,
    ``"hevc"``, ``"h264"``.  When a specific codec is requested but no
    hardware encoder is available, falls back to software (libx264 for h264,
    libsvtav1 for av1) or raises if truly unavailable.
    """

    if codec not in CODEC_CHOICES:
        raise ValueError(f"Invalid codec {codec!r}, expected one of {CODEC_CHOICES}")

    _test_cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", "nullsrc=s=640x360:d=0.1:r=15"]

    def _try_encoder(enc_name: str) -> bool:
        try:
            test = run_subprocess(
                _test_cmd + ["-c:v", enc_name, "-f", "null", "-"],
                capture_output=True,
                text=True,
            )
            return test.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _nvenc_args(enc_name: str, bitrate: str) -> list[str]:
        return [
            "-c:v",
            enc_name,
            "-preset",
            "p4",
            "-rc",
            "vbr",
            "-b:v",
            bitrate,
            "-maxrate",
            bitrate,
        ]

    def _vt_args(enc_name: str, bitrate: str) -> list[str]:
        return ["-c:v", enc_name, "-b:v", bitrate]

    def _sw_args(enc_name: str, bitrate: str) -> list[str]:
        return ["-c:v", enc_name, "-preset", "fast", "-b:v", bitrate]

    # --- Build candidate list based on codec preference and platform ---
    if sys.platform == "darwin":
        candidates = {
            "av1": [
                ("av1_videotoolbox", _vt_args),
            ],
            "hevc": [
                ("hevc_videotoolbox", _vt_args),
            ],
            "h264": [
                ("h264_videotoolbox", _vt_args),
            ],
        }
    else:
        candidates = {
            "av1": [
                ("av1_nvenc", _nvenc_args),
            ],
            "hevc": [
                ("hevc_nvenc", _nvenc_args),
            ],
            "h264": [
                ("h264_nvenc", _nvenc_args),
            ],
        }

    # Software fallbacks (cross-platform)
    sw_fallbacks = {
        "av1": ("libsvtav1", _sw_args),
        "hevc": ("libx265", _sw_args),
        "h264": ("libx264", _sw_args),
    }

    if codec == "auto":
        # Try HEVC first (best size/speed balance), then H.264
        search_order = ["hevc", "h264"]
    else:
        search_order = [codec]

    tried: list[str] = []
    for family in search_order:
        br = _bitrate_for_codec(family, width, height, fps, bitrate)
        # Try hardware encoders
        for enc_name, args_fn in candidates.get(family, []):
            tried.append(enc_name)
            if _try_encoder(enc_name):
                logger.info("Encoder: %s @ %s", enc_name, br)
                return args_fn(enc_name, br)
        # Try software fallback
        if family in sw_fallbacks:
            enc_name, args_fn = sw_fallbacks[family]
            tried.append(enc_name)
            if _try_encoder(enc_name):
                logger.info("Encoder: %s (software) @ %s", enc_name, br)
                return args_fn(enc_name, br)

    # Ultimate fallback: libx264
    h264_br = _bitrate_for_codec("h264", width, height, fps, bitrate)
    logger.warning(
        "No encoder found (tried %s), falling back to libx264", ", ".join(tried)
    )
    return ["-c:v", "libx264", "-preset", "fast", "-b:v", h264_br]


def _detect_hwaccel() -> list[str] | None:
    """Detect hardware-accelerated decoder: CUDA (NVIDIA) or VideoToolbox (macOS)."""
    candidates = (
        [("-hwaccel", "videotoolbox")]
        if sys.platform == "darwin"
        else [("-hwaccel", "cuda")]
    )
    for args in candidates:
        try:
            result = run_subprocess(
                [
                    "ffmpeg",
                    "-y",
                    *args,
                    "-f",
                    "lavfi",
                    "-i",
                    "nullsrc=s=64x64:d=0.1",
                    "-frames:v",
                    "1",
                    "-f",
                    "null",
                    "-",
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                logger.info("Hardware decoder: %s", args[-1])
                return list(args)
        except (OSError, subprocess.SubprocessError):
            pass
    logger.info("Hardware decoder: none (CPU fallback)")
    return None


# ---------------------------------------------------------------------------
# RenderContext — per-run render state, passed explicitly (no globals)
# ---------------------------------------------------------------------------


@dataclass
class RenderContext:
    """Per-run render state. Created by assemble(), passed to all render modules."""

    w: int
    h: int
    fps: int
    bitrate: float = 1.0
    codec: str = "auto"
    _encoder_cache: dict[tuple, list[str]] = field(default_factory=dict)
    _hwaccel: list[str] | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.w <= 0 or self.h <= 0:
            raise ValueError(f"Invalid resolution: {self.w}x{self.h}")
        if self.w % 2 != 0 or self.h % 2 != 0:
            raise ValueError(f"Resolution must be even: {self.w}x{self.h}")
        if self.fps <= 0 or self.fps > 120:
            raise ValueError(f"Invalid fps: {self.fps}")
        self._hwaccel = _detect_hwaccel()

    _dim_cache: dict[str, tuple[int, int]] = field(default_factory=dict)
    _dur_cache: dict[str, float] = field(default_factory=dict)
    _trc_cache: dict[str, str] = field(default_factory=dict)

    @property
    def hwaccel_args(self) -> list[str]:
        """Return hwaccel input args (e.g. ['-hwaccel', 'cuda']), or [] if unavailable."""
        return list(self._hwaccel) if self._hwaccel else []

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
        key = (w, h, f, self.bitrate, self.codec)
        if key not in self._encoder_cache:
            self._encoder_cache[key] = detect_hw_encoder(
                w, h, f, self.bitrate, codec=self.codec
            )
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

    def probe_duration(self, path: Path) -> float:
        key = str(path)
        if key in self._dur_cache:
            return self._dur_cache[key]
        duration = _probe_duration_uncached(path)
        self._dur_cache[key] = duration
        return duration

    def probe_color_transfer(self, path: Path) -> str:
        """Return the video's color_transfer (e.g. 'smpte2084', 'arib-std-b67',
        'bt709'), or '' if unknown. Cached per source path.

        Used to decide HDR→SDR tone-mapping: HDR clips report a PQ
        (``smpte2084``) or HLG (``arib-std-b67``) transfer; SDR clips report
        ``bt709`` / ``bt470bg`` / unknown.
        """
        key = str(path)
        if key in self._trc_cache:
            return self._trc_cache[key]
        result = run_subprocess(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=color_transfer",
                "-of",
                "default=nw=1:nk=1",
                str(path),
            ],
            capture_output=True,
            text=True,
        )
        trc = (result.stdout or "").strip().split("\n")[0].strip()
        self._trc_cache[key] = trc
        return trc
