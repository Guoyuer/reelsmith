"""Video concatenation via demuxer — simple, reliable, no re-encoding."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from ..media_utils import run_subprocess

logger = logging.getLogger("vlog.assemble.concat")


def concatenate(clips: list[dict], output_path: Path, **_kwargs) -> None:
    """Concatenate pre-faded clips via concat demuxer (no re-encoding).

    Clips already have fade-in/fade-out baked in during render.
    This just stitches them together with -c:v copy.
    """
    if len(clips) == 1:
        shutil.copy(str(clips[0]["path"]), str(output_path))
        return

    list_path = output_path.with_suffix(".txt")
    with open(list_path, "w") as f:
        for clip in clips:
            safe = str(clip["path"].resolve()).replace("\\", "/")
            f.write(f"file '{safe}'\n")

    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(list_path),
        "-c:v", "copy", "-an",
        "-fflags", "+genpts",
        "-avoid_negative_ts", "make_zero",
        str(output_path),
    ]
    result = run_subprocess(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Concat failed: {result.stderr[-300:]}")
    logger.info(f"  Concat: {len(clips)} clips → {output_path.name}")
