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


def detect_scenes(
    video_path: Path,
    threshold: float = 0.3,
    min_scene_duration: float = 2.0,
) -> list[dict]:
    """Detect scene boundaries in a video using FFmpeg's scene filter.

    Returns a list of scene dicts with start/end times.
    Falls back to evenly-spaced segments if scene detection fails.
    """
    probe = run_subprocess(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(video_path)],
        capture_output=True, text=True,
    )
    try:
        total_duration = float(probe.stdout.strip())
    except (ValueError, AttributeError):
        total_duration = 10.0

    # Use FFmpeg scene filter to find scene changes
    result = run_subprocess(
        ["ffmpeg", "-i", str(video_path),
         "-vf", f"select='gt(scene,{threshold})',showinfo",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )

    # Parse scene change timestamps from stderr
    import re
    timestamps = [0.0]  # always start at 0
    for line in (result.stderr or "").split("\n"):
        match = re.search(r"pts_time:(\d+\.?\d*)", line)
        if match:
            t = float(match.group(1))
            # Skip if too close to previous timestamp
            if t - timestamps[-1] >= min_scene_duration:
                timestamps.append(t)
    timestamps.append(total_duration)

    # If scene detection found nothing useful, split evenly
    if len(timestamps) <= 2 and total_duration > 6:
        n_segments = min(max(int(total_duration / 5), 2), 8)
        interval = total_duration / n_segments
        timestamps = [i * interval for i in range(n_segments)] + [total_duration]

    # Build scene list
    scenes = []
    for i in range(len(timestamps) - 1):
        start = round(timestamps[i], 2)
        end = round(timestamps[i + 1], 2)
        if end - start < 0.5:
            continue
        scenes.append({
            "scene_index": len(scenes),
            "start": start,
            "end": end,
            "duration": round(end - start, 2),
        })

    return scenes


def extract_scene_keyframe(
    video_path: Path,
    scene: dict,
    output_dir: Path,
    prefix: str,
) -> Path | None:
    """Extract a representative keyframe from the middle of a scene."""
    output_dir.mkdir(parents=True, exist_ok=True)
    mid_time = (scene["start"] + scene["end"]) / 2
    out_path = output_dir / f"{prefix}_scene_{scene['scene_index']:02d}.jpg"
    if out_path.exists():
        return out_path
    run_subprocess(
        ["ffmpeg", "-y", "-ss", str(mid_time), "-i", str(video_path),
         "-frames:v", "1", "-vf", "scale=1024:-1", "-q:v", "3",
         str(out_path)],
        capture_output=True,
    )
    return out_path if out_path.exists() else None


def classify_motion(video_path: Path, start: float = 0, duration: float = 5) -> str:
    """Classify camera motion in a video segment using frame difference analysis.

    Returns one of: static, pan, handheld, smooth_motion, unknown
    """
    try:
        import cv2
        import numpy as np
    except ImportError:
        return "unknown"

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        return "unknown"

    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000)

    frames_to_sample = min(int(duration * fps), 90)  # cap at 90 frames
    sample_interval = max(1, frames_to_sample // 30)  # sample ~30 frames

    prev_gray = None
    diffs = []
    frame_count = 0

    while frame_count < frames_to_sample:
        ret, frame = cap.read()
        if not ret:
            break
        frame_count += 1
        if frame_count % sample_interval != 0:
            continue

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.resize(gray, (160, 90))  # small for speed

        if prev_gray is not None:
            diff = cv2.absdiff(prev_gray, gray)
            diffs.append(float(np.mean(diff)))
        prev_gray = gray

    cap.release()

    if not diffs:
        return "unknown"

    mean_diff = np.mean(diffs)
    std_diff = np.std(diffs)

    if mean_diff < 3:
        return "static"
    elif mean_diff < 8 and std_diff < 3:
        return "smooth_motion"  # steady pan or drone
    elif std_diff > 6:
        return "handheld"  # shaky/action
    else:
        return "pan"


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
