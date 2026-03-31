"""Image utilities: thumbnails for Gemini, HEIC pre-decode for FFmpeg."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

logger = logging.getLogger("reelsmith.image_utils")

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:
    pass  # HEIC thumbnails require pillow-heif

_HEIC_EXTENSIONS = frozenset({".heic", ".heif"})


def decode_heic_for_filter(source: Path) -> tuple[Path, bool]:
    """Decode HEIC to temp JPEG for FFmpeg filter_complex compatibility.

    FFmpeg 8.x represents HEIF photos as tile grids (48+ separate 512x512
    streams). In simple decode mode, FFmpeg auto-composes tiles via xstack.
    However, ``filter_complex`` with ``[0:v]`` selects the first tile stream
    instead of the composed image, producing a black/corrupted frame.

    This pre-decodes HEIC files to JPEG using FFmpeg's simple decode path
    (which correctly triggers xstack), so ``filter_complex`` can read them.

    Returns ``(path, was_decoded)``. Caller must delete the temp file when
    ``was_decoded`` is True.
    """
    if source.suffix.lower() not in _HEIC_EXTENSIONS:
        return source, False

    from .media import run_subprocess

    tmp_dir = Path(tempfile.gettempdir()) / "reelsmith_heic"
    tmp_dir.mkdir(exist_ok=True)
    decoded = tmp_dir / f"{source.stem}_{hash(str(source)) & 0xFFFFFF:06x}.jpg"
    if decoded.exists():
        return decoded, True

    result = run_subprocess(
        ["ffmpeg", "-y", "-i", str(source), "-q:v", "2", str(decoded)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0 or not decoded.exists():
        logger.warning(
            "HEIC decode failed for %s: %s", source.name, result.stderr[-200:]
        )
        return source, False
    return decoded, True


def generate_thumbnail(
    source: Path,
    output_dir: Path,
    size: int = 400,
    quality: int = 70,
) -> Path | None:
    """Generate a thumbnail JPEG for an image file. Cached — skips if exists."""
    from PIL import Image

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{source.stem}_thumb.jpg"
    if out_path.exists():
        return out_path

    try:
        img = Image.open(source)
        img.thumbnail((size, size))
        img.save(out_path, "JPEG", quality=quality)
    except (OSError, RuntimeError) as e:
        logger.warning("Thumbnail failed for %s: %s — skipping", source.name, e)
        return None

    return out_path
