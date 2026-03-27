"""Shared media utilities — deduplicated helpers used across pipeline stages."""

from __future__ import annotations

import logging
import os
import subprocess
import sys

# On Windows, ensure WinGet tool locations take priority on PATH.
# Other tools (e.g. ImageMagick) may bundle outdated FFmpeg copies.
if sys.platform == "win32":
    _winget_links = os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links"
    )
    if os.path.isdir(_winget_links):
        os.environ["PATH"] = _winget_links + os.pathsep + os.environ.get("PATH", "")


_ffmpeg_logger = logging.getLogger("vlog.ffmpeg")
logger = logging.getLogger("vlog.media")

# Set by CLI signal handler; checked by run_subprocess after child exits.
_interrupted = False


def set_interrupted() -> None:
    """Signal that the user pressed Ctrl+C."""
    global _interrupted
    _interrupted = True


def run_subprocess(
    cmd: list[str], timeout: int = 300, **kwargs
) -> subprocess.CompletedProcess:
    """Run a subprocess that is killed when the parent receives SIGINT/SIGTERM.

    Unlike ``subprocess.run``, this uses ``Popen`` so that Python's signal
    handler can execute between poll intervals.  When interrupted, the child
    process is terminated immediately (SIGTERM, then SIGKILL after 3s).

    Timeout defaults to 300s (5min) to prevent hanging on corrupt files.
    Accepts the same keyword arguments as ``subprocess.run``.
    """
    if cmd and cmd[0] in ("ffmpeg", "ffprobe"):
        _ffmpeg_logger.debug("$ %s", " ".join(str(c) for c in cmd))
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
        proc.kill()
        proc.wait()
        raise
    if _interrupted:
        raise KeyboardInterrupt
    return subprocess.CompletedProcess(
        cmd,
        proc.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def probe_duration(path) -> float:
    """Get media file duration via ffprobe (uncached, for simple one-off probes)."""
    result = run_subprocess(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    try:
        return float(result.stdout.strip().split("\n")[0])
    except (ValueError, IndexError):
        logger.debug(
            "ffprobe returned unparseable output for %s: %r", path, result.stdout
        )
        return 0.0


def strip_markdown_fences(text: str) -> str:
    """Remove markdown code fences (```json ... ```) if present.

    Returns the text unchanged when no fences are found.
    """
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return text
