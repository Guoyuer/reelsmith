"""Image utilities: HEIC conversion, thumbnails."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from .media_utils import run_subprocess

logger = logging.getLogger("vlog.image_utils")

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:
    pass  # HEIC support unavailable; convert_heic will use sips/ImageMagick fallback

# Set by init_heic_dir() from Config; convert_heic uses this instead of source.parent
_heic_dest_dir: Path | None = None


def init_heic_dir(dest_dir: Path) -> None:
    """Set the global HEIC conversion output directory."""
    global _heic_dest_dir
    _heic_dest_dir = dest_dir
    dest_dir.mkdir(parents=True, exist_ok=True)


def convert_heic(source: Path, dest_dir: Path | None = None) -> Path:
    """Convert a HEIC/HEIF image to JPEG.

    Tries backends in order: pillow-heif (cross-platform), macOS sips,
    ImageMagick. At least one must be available.

    Output goes to *dest_dir* if given, else the module-level dir set by
    ``init_heic_dir()``, else *source.parent* as last resort.
    """
    dest_dir = dest_dir or _heic_dest_dir or source.parent
    jpeg_path = dest_dir / f"_converted_{source.stem}.jpg"
    if jpeg_path.exists():
        return jpeg_path

    # Try 1: pillow-heif + Pillow (cross-platform, pip install pillow-heif)
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

    # Try 2: macOS sips (no extra deps needed on Mac)
    if shutil.which("sips"):
        run_subprocess(
            ["sips", "-s", "format", "jpeg", str(source), "--out", str(jpeg_path)],
            capture_output=True,
        )
        if jpeg_path.exists():
            return jpeg_path

    # Try 3: ImageMagick (cross-platform, if installed)
    magick = shutil.which("magick") or shutil.which("convert")
    if magick:
        run_subprocess([magick, str(source), str(jpeg_path)], capture_output=True)
        if jpeg_path.exists():
            return jpeg_path

    raise RuntimeError(
        f"HEIC conversion failed for {source}. " "Install pillow-heif (`pip install pillow-heif`) or ImageMagick."
    )


def generate_thumbnail(
    source: Path,
    output_dir: Path,
    size: int = 400,
    quality: int = 70,
) -> Path | None:
    """Generate a thumbnail JPEG for an image file. Cached — skips if exists.

    pillow-heif must be registered before calling (for HEIC sources).
    """
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
