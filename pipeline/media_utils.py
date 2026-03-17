"""Shared media utilities — deduplicated helpers used across pipeline stages."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

# On Windows, ensure common tool install locations are on PATH.
# WinGet installs FFmpeg/Ollama to dirs not always on PATH.
if sys.platform == "win32":
    for _p in [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama"),
    ]:
        if os.path.isdir(_p) and _p not in os.environ.get("PATH", ""):
            os.environ["PATH"] = _p + os.pathsep + os.environ.get("PATH", "")


def run_subprocess(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a subprocess that is killed when the parent receives SIGINT/SIGTERM.

    Unlike ``subprocess.run``, this uses ``Popen`` so that Python's signal
    handler can execute between poll intervals.  When interrupted, the child
    process is terminated immediately (SIGTERM, then SIGKILL after 3s).

    Accepts the same keyword arguments as ``subprocess.run``.
    """
    capture = kwargs.pop("capture_output", False)
    if capture:
        kwargs.setdefault("stdout", subprocess.PIPE)
        kwargs.setdefault("stderr", subprocess.PIPE)

    proc = subprocess.Popen(cmd, **kwargs)
    try:
        stdout, stderr = proc.communicate()
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


def extract_frames(
    video_path: Path,
    output_dir: Path,
    prefix: str,
    count: int = 5,
) -> list[Path]:
    """Extract *count* evenly-spaced frames from a video using ``-ss`` seeking.

    Parameters
    ----------
    video_path : Path
        Source video file.
    output_dir : Path
        Directory where frame images are written.
    prefix : str
        Filename prefix, e.g. ``"42"`` produces ``42_01.jpg``, ``42_02.jpg``, ...
    count : int
        Number of frames to extract (default 5).

    Returns
    -------
    list[Path]
        Sorted list of extracted frame paths.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    probe = run_subprocess(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(video_path)],
        capture_output=True, text=True,
    )
    try:
        duration = float(probe.stdout.strip())
    except (ValueError, AttributeError):
        duration = 10.0

    interval = max(duration / (count + 1), 0.5)

    for i in range(1, count + 1):
        t = interval * i
        out_path = output_dir / f"{prefix}_{i:02d}.jpg"
        run_subprocess(
            [
                "ffmpeg", "-y", "-ss", str(t), "-i", str(video_path),
                "-frames:v", "1", "-vf", "scale=1024:-1",
                "-q:v", "3",
                str(out_path),
            ],
            capture_output=True,
        )

    return sorted(output_dir.glob(f"{prefix}_*.jpg"))


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
