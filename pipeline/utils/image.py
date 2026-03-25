"""Image utilities: HEIC conversion for FFmpeg, thumbnails for Gemini."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from .media import run_subprocess

logger = logging.getLogger("vlog.image_utils")

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:
    pass  # HEIC thumbnails require pillow-heif; rendering uses convert_heic fallback


def convert_heic(source: Path, cache_dir: Path | None = None) -> Path:
    """Convert HEIC to JPEG for FFmpeg (which can't -loop 1 with HEIC).

    Cached — skips if output exists. Tries pillow-heif, sips, ImageMagick.
    Output goes to cache_dir (default: temp dir), never pollutes source directory.
    """
    if cache_dir is None:
        import tempfile

        cache_dir = Path(tempfile.gettempdir()) / "vlog_heic_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    jpeg_path = cache_dir / f"_converted_{source.stem}.jpg"
    if jpeg_path.exists():
        return jpeg_path

    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
        from PIL import Image

        img = Image.open(source)
        img.save(jpeg_path, "JPEG", quality=92)
        if jpeg_path.exists():
            return jpeg_path
    except ImportError:
        pass

    if shutil.which("sips"):
        run_subprocess(
            ["sips", "-s", "format", "jpeg", str(source), "--out", str(jpeg_path)],
            capture_output=True,
        )
        if jpeg_path.exists():
            return jpeg_path

    magick = shutil.which("magick") or shutil.which("convert")
    if magick:
        run_subprocess([magick, str(source), str(jpeg_path)], capture_output=True)
        if jpeg_path.exists():
            return jpeg_path

    raise RuntimeError(
        f"HEIC conversion failed for {source}. Install pillow-heif or ImageMagick."
    )


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
