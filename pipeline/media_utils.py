"""Shared media utilities — deduplicated helpers used across pipeline stages."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

# On Windows, ensure WinGet tool locations take priority on PATH.
# Other tools (e.g. ImageMagick) may bundle outdated FFmpeg copies.
if sys.platform == "win32":
    _winget_links = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links")
    if os.path.isdir(_winget_links):
        os.environ["PATH"] = _winget_links + os.pathsep + os.environ.get("PATH", "")


_ffmpeg_logger = logging.getLogger("pipeline.ffmpeg")


def run_subprocess(cmd: list[str], timeout: int = 300, **kwargs) -> subprocess.CompletedProcess:
    """Run a subprocess that is killed when the parent receives SIGINT/SIGTERM.

    Unlike ``subprocess.run``, this uses ``Popen`` so that Python's signal
    handler can execute between poll intervals.  When interrupted, the child
    process is terminated immediately (SIGTERM, then SIGKILL after 3s).

    Timeout defaults to 300s (5min) to prevent hanging on corrupt files.
    Accepts the same keyword arguments as ``subprocess.run``.
    """
    if cmd and cmd[0] in ("ffmpeg", "ffprobe"):
        _ffmpeg_logger.info("$ %s", " ".join(str(c) for c in cmd))
    capture = kwargs.pop("capture_output", False)
    if capture:
        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)

    proc = subprocess.Popen(cmd, **kwargs)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        return subprocess.CompletedProcess(cmd, 1, stdout=stdout, stderr=stderr)
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        raise
    return subprocess.CompletedProcess(
        cmd, proc.returncode,
        stdout=stdout, stderr=stderr,
    )


def strip_markdown_fences(text: str) -> str:
    """Remove markdown code fences (```json ... ```) if present.

    Returns the text unchanged when no fences are found.
    """
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return text


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
    import shutil

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
) -> Path:
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
    except Exception as e:
        import warnings
        warnings.warn(f"Could not generate thumbnail for {source}: {e}")
        img = Image.new("RGB", (size, size), (60, 60, 60))
        img.save(out_path, "JPEG", quality=85)

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

    for idx, img_path in enumerate(image_paths):
        row, col = divmod(idx, columns)
        x, y = col * cell_size, row * cell_size

        try:
            if img_path.suffix.lower() in {".heic", ".heif"}:
                img_path = convert_heic(img_path)
            img = Image.open(img_path)
            img.thumbnail((cell_size - 4, cell_size - 4))
            # Center in cell
            ox = x + (cell_size - img.width) // 2
            oy = y + (cell_size - img.height) // 2
            sheet.paste(img, (ox, oy))
        except Exception as e:
            import warnings
            warnings.warn(f"Contact sheet: could not load {img_path}: {e}")

        # Draw label — large, high-contrast, top-left corner for visibility
        label = labels[idx] if idx < len(labels) else ""
        if label:
            # Use a larger font if available
            try:
                from PIL import ImageFont
                font = ImageFont.truetype("arial.ttf", 20)
            except (OSError, ImportError):
                font = ImageDraw.getfont()
            bbox = draw.textbbox((0, 0), label, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            pad = 4
            draw.rectangle([x, y, x + tw + pad * 2, y + th + pad * 2],
                           fill=(0, 0, 0, 200))
            draw.text((x + pad, y + pad), label, fill="yellow", font=font)

    sheet.save(output_path, "JPEG", quality=88)
    return output_path



# ---------------------------------------------------------------------------
# FFmpeg filter-string helpers (used by assemble.py)
# ---------------------------------------------------------------------------

def _zoompan_filter(
    zoom_rate: float, frames: int, w: int, h: int, fps: int,
    direction: str = "in",
) -> str:
    """Build a Ken Burns zoompan filter expression.

    *direction*: ``"in"``, ``"out"``, ``"left"``, ``"right"``, or ``"static"``.
    """
    center = f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
    tail = f":s={w}x{h}:fps={fps}"

    zoom_exprs = {
        "in":     f"z='min(zoom+{zoom_rate:.6f},1.3)':d={frames}:{center}",
        "out":    f"z='if(eq(on,1),1.3,max(zoom-{zoom_rate:.6f},1.0))':d={frames}:{center}",
        "left":   f"z='1.15':d={frames}:x='(iw-iw/zoom)*on/{frames}':y='ih/2-(ih/zoom/2)'",
        "right":  f"z='1.15':d={frames}:x='(iw-iw/zoom)*(1-on/{frames})':y='ih/2-(ih/zoom/2)'",
        "static": f"z='1':d={frames}",
    }
    return f"zoompan={zoom_exprs.get(direction, zoom_exprs['in'])}{tail}"


def _portrait_bg_filter(w: int, h: int) -> str:
    """Build the blurred-background + sharp-foreground overlay filter for portrait videos.

    The result is a ``filter_complex`` string suitable for a single-input
    FFmpeg command (expects ``[0:v]`` as input).
    """
    return (
        f"[0:v]split[bg][fg];"
        f"[bg]scale={w}:-1:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},gblur=sigma=30[blurred];"
        f"[fg]scale=-1:{h}[sharp];"
        f"[blurred][sharp]overlay=(W-w)/2:(H-h)/2"
    )
