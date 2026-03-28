"""Shared media utilities — deduplicated helpers used across pipeline stages."""

from __future__ import annotations

import logging
import os
import re
import shutil
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


_ffmpeg_logger = logging.getLogger("reelsmith.ffmpeg")
logger = logging.getLogger("reelsmith.media")

# ---------------------------------------------------------------------------
# FFmpeg version requirement
# ---------------------------------------------------------------------------

#: Minimum supported FFmpeg major version.
#: Key features that require this version:
#:   - ``loudnorm`` filter (two-pass loudness normalization) — FFmpeg 3.1+
#:   - ``-filter_complex_script`` flag — FFmpeg 4.0+
#:   - Stable HEVC hardware encoding (nvenc / videotoolbox) — FFmpeg 5.0+
#:   - Multi-threaded CLI (parallel demux/decode/filter/encode) — FFmpeg 7.0+
#: Recommended: FFmpeg 8.0+ for Whisper speech filter and Vulkan AV1 encoding.
MIN_FFMPEG_VERSION = (7, 0)


def check_ffmpeg(*, required: bool = True) -> tuple[int, ...] | None:
    """Verify that FFmpeg is installed and meets the minimum version requirement.

    Parameters
    ----------
    required:
        If *True* (default), raise :class:`SystemExit` when FFmpeg is missing
        or too old.  If *False*, log a warning instead and return *None*.

    Returns
    -------
    tuple[int, ...] | None
        Parsed version tuple (e.g. ``(7, 1)``), or *None* when the check
        fails and *required* is *False*.
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        msg = (
            "FFmpeg not found on PATH. "
            "Install FFmpeg %d.%d+ from https://ffmpeg.org/download.html"
            % MIN_FFMPEG_VERSION
        )
        if required:
            raise SystemExit(msg)
        logger.warning(msg)
        return None

    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        first_line = (result.stdout or "").split("\n", 1)[0]
    except (OSError, subprocess.SubprocessError) as exc:
        msg = "Could not determine FFmpeg version: %s" % exc
        if required:
            raise SystemExit(msg)
        logger.warning(msg)
        return None

    # Typical first line: "ffmpeg version 7.1 Copyright ..."
    # or "ffmpeg version N-12345-gabcdef ..." (nightly builds).
    match = re.search(r"ffmpeg version (\d+)(?:\.(\d+))?", first_line)
    if not match:
        logger.warning("Could not parse FFmpeg version from: %s", first_line)
        # Don't block on unparseable output — might be a custom/nightly build.
        return None

    version = (int(match.group(1)), int(match.group(2) or 0))

    if version < MIN_FFMPEG_VERSION:
        msg = (
            "FFmpeg %d.%d detected, but %d.%d+ is required. "
            "Please upgrade: https://ffmpeg.org/download.html"
            % (*version, *MIN_FFMPEG_VERSION)
        )
        if required:
            raise SystemExit(msg)
        logger.warning(msg)
        return None

    logger.debug("FFmpeg %d.%d detected (>= %d.%d required)", *version, *MIN_FFMPEG_VERSION)
    return version

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
