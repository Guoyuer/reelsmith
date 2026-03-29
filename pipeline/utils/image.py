"""Image utilities: thumbnails for Gemini."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("reelsmith.image_utils")

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:
    pass  # HEIC thumbnails require pillow-heif


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
