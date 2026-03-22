"""Image utilities: HEIC conversion, thumbnails, contact sheets."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from .media_utils import run_subprocess

logger = logging.getLogger("vlog.image_utils")


def convert_heic(source: Path, dest_dir: Path | None = None) -> Path:
    """Convert a HEIC/HEIF image to JPEG.

    Tries backends in order: pillow-heif (cross-platform), macOS sips,
    ImageMagick. At least one must be available.

    Parameters
    ----------
    source : Path
        Path to the .heic/.heif file.
    dest_dir : Path | None
        Directory for the output JPEG. Defaults to the same directory as *source*.

    Returns
    -------
    Path
        Path to the converted JPEG. If the JPEG already exists, conversion is
        skipped and the existing path is returned.

    Raises
    ------
    RuntimeError
        If no backend succeeds.
    """

    dest_dir = dest_dir or source.parent
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
            ["sips", "-s", "format", "jpeg",
             str(source), "--out", str(jpeg_path)],
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
        f"HEIC conversion failed for {source}. "
        "Install pillow-heif (`pip install pillow-heif`) or ImageMagick."
    )


def generate_thumbnail(
    source: Path,
    output_dir: Path,
    size: int = 512,
) -> Path | None:
    """Generate a thumbnail JPEG for an image file. Cached — skips if exists."""
    from PIL import Image

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{source.stem}_thumb.jpg"
    if out_path.exists():
        return out_path

    try:
        if source.suffix.lower() in {".heic", ".heif"}:
            source = convert_heic(source)
        img = Image.open(source)
        img.thumbnail((size, size))
        img.save(out_path, "JPEG", quality=85)
    except (OSError, RuntimeError) as e:
        logger.warning(
            "Thumbnail failed for %s: %s — skipping", source.name, e)
        return None

    return out_path


def make_contact_sheet(
    image_paths: list[Path],
    output_path: Path,
    cell_size: int = 256,
    columns: int = 4,
    labels: list[str] | None = None,
) -> Path:
    """Arrange images into a numbered grid contact sheet."""
    from math import ceil

    from PIL import Image, ImageDraw

    if not image_paths:
        return output_path

    output_path.parent.mkdir(parents=True, exist_ok=True)
    labels = labels or [f"#{i + 1:02d}" for i in range(len(image_paths))]
    rows = ceil(len(image_paths) / columns)
    sheet = Image.new("RGB", (columns * cell_size, rows * cell_size), (30, 30, 30))
    draw = ImageDraw.Draw(sheet)

    # Load font once for all labels
    try:
        from PIL import ImageFont
        label_font = ImageFont.truetype("arial.ttf", 20)
    except (OSError, ImportError):
        label_font = ImageDraw.getfont()

    for idx, img_path in enumerate(image_paths):
        row, col = divmod(idx, columns)
        x, y = col * cell_size, row * cell_size

        try:
            if img_path.suffix.lower() in {".heic", ".heif"}:
                img_path = convert_heic(img_path)
            img = Image.open(img_path)
            img.thumbnail((cell_size - 4, cell_size - 4))
            ox = x + (cell_size - img.width) // 2
            oy = y + (cell_size - img.height) // 2
            sheet.paste(img, (ox, oy))
        except Exception as e:
            import warnings
            warnings.warn(f"Contact sheet: could not load {img_path}: {e}")

        label = labels[idx] if idx < len(labels) else ""
        if label:
            bbox = draw.textbbox((0, 0), label, font=label_font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            pad = 4
            draw.rectangle([x, y, x + tw + pad * 2, y + th + pad * 2],
                           fill=(0, 0, 0, 200))
            draw.text((x + pad, y + pad), label, fill="yellow", font=label_font)

    sheet.save(output_path, "JPEG", quality=75)
    return output_path
