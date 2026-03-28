"""Stage 1: Prepare media for visual planning.

- Photo thumbnails (400px JPEG, cached in thumbnails/)
- EXIF extraction (recomputed each run — fast)
- Video duration probing (recomputed each run — fast)
- Video previews (480p 1fps, cached in previews/)

Results written to a single analysis.json per run.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .._types import VIDEO_EXTENSIONS, AnalysisEntry, _AnalysisEntryValidator, cache_id
from ..config import Config, ProgressCallback
from ..utils.image import generate_thumbnail
from ..utils.media import run_subprocess

logger = logging.getLogger("reelsmith.prepare")


@dataclass
class PrepareConfig:
    force: bool = False


def _base_analysis_entry(item: dict, *, is_video: bool) -> dict:
    """Build the common analysis entry dict from a manifest item."""
    return {
        "local_path": item["local_path"],
        "media_type": "video" if is_video else "photo",
        "taken_at": item["taken_at"],
        "filesize": item.get("filesize", 0),
        "country": item.get("country"),
        "district": item.get("city"),
    }


def load_analysis(cfg: Config) -> list[AnalysisEntry]:
    """Load analysis data written by prepare().

    Reads analysis.json and validates each entry via Pydantic.
    """
    if not cfg.analysis_path.exists():
        raise FileNotFoundError(
            f"Analysis not found: {cfg.analysis_path}\n"
            "Run the prepare stage first (e.g. reelsmith prepare -p ./photos)"
        )
    raw = json.loads(cfg.analysis_path.read_text())

    results = []
    n_skipped = 0
    for entry in raw:
        try:
            validated = _AnalysisEntryValidator.model_validate(entry)
            results.append(validated.model_dump(exclude_none=False))
        except ValidationError as e:
            logger.warning(
                "Invalid analysis entry for %s: %s",
                Path(entry.get("local_path", "?")).name,
                e,
            )
            n_skipped += 1

    if n_skipped:
        logger.warning(
            "Skipped %d items with invalid analysis data (see warnings above)",
            n_skipped,
        )
    return results


def prepare(
    cfg: Config,
    pc: PrepareConfig | None = None,
    *,
    source_dir: str | None = None,
    progress_callback: ProgressCallback = None,
) -> None:
    """Prepare all media for Gemini visual planning.

    1. Scan source folder for media (if source_dir provided)
    2. Generate thumbnails + EXIF for photos, probe duration for videos
    3. Write analysis.json
    4. Generate video previews (480p 1fps)
    """
    if pc is None:
        pc = PrepareConfig()
    cfg.ensure_dirs()

    # --- Phase 0: Scan source folder ---
    if source_dir:
        if cfg.manifest_path.exists() and not pc.force:
            items = json.loads(cfg.manifest_path.read_text())
            logger.info("Scan: %d items (cached)", len(items))
        else:
            from ._scan import fetch_local

            fetch_local(cfg, source_dir, progress_callback=progress_callback)

    if not cfg.manifest_path.exists():
        raise FileNotFoundError(
            f"Manifest not found: {cfg.manifest_path}\n"
            "Run the prepare stage first (e.g. reelsmith prepare -p ./photos)"
        )
    manifest = json.loads(cfg.manifest_path.read_text())

    # --- Phase 1: Process all items ---
    photos: list[dict] = []
    videos: list[dict] = []

    for i, item in enumerate(manifest, 1):
        local_path_str = item["local_path"]
        local_path = Path(local_path_str)
        if not local_path.exists():
            continue

        suffix = local_path.suffix.lower()
        is_video = suffix in VIDEO_EXTENSIONS
        entry = _base_analysis_entry(item, is_video=is_video)

        if is_video:
            _prepare_video(entry, local_path, i, len(manifest))
            videos.append(entry)
        else:
            _prepare_photo(entry, local_path, cfg)
            photos.append(entry)

        if progress_callback:
            progress_callback(i, len(manifest), "extract metadata")

    # Rich video probe summary table
    from ..utils import stderr_console

    console = stderr_console()
    if console and videos:
        from rich.table import Table

        t = Table(
            title=f"Video Probe ({len(videos)})",
            border_style="dim",
            show_lines=False,
        )
        t.add_column("File", max_width=35)
        t.add_column("Duration", justify="right")
        t.add_column("Resolution")
        t.add_column("FPS", justify="right")
        for entry in videos[:20]:
            t.add_row(
                Path(entry["local_path"]).name[:35],
                f"{entry.get('video_duration', 0):.0f}s",
                f"{entry.get('video_width', '?')}x{entry.get('video_height', '?')}",
                f"{entry.get('video_fps', '?')}",
            )
        if len(videos) > 20:
            t.add_row(f"... +{len(videos) - 20} more", "", "", "")
        console.print(t)

    logger.info(
        "Prepared: %d items (%d photos, %d videos)",
        len(photos) + len(videos),
        len(photos),
        len(videos),
    )

    # --- Phase 2: Write analysis.json ---
    all_entries = photos + videos
    cfg.analysis_path.write_text(json.dumps(all_entries, indent=2))

    # --- Phase 3: Generate video previews (cached per video) ---
    if videos:
        _generate_video_previews(
            videos,
            cfg.previews_dir,
            force=pc.force,
            progress_callback=progress_callback,
        )


# ---------------------------------------------------------------------------
# Video preview generation (360p 1fps, cached per video)
# ---------------------------------------------------------------------------


def _has_dense_keyframes(source: Path) -> bool:
    """Check if video has keyframe interval <= 2s (safe for -skip_frame nokey)."""
    try:
        r = run_subprocess(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-read_intervals",
                "%+10",
                "-show_entries",
                "packet=pts_time,flags",
                "-of",
                "csv=p=0",
                str(source),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        keyframe_times = []
        for line in r.stdout.strip().split("\n"):
            parts = line.strip().split(",")
            if len(parts) == 2 and "K" in parts[1]:
                try:
                    keyframe_times.append(float(parts[0]))
                except ValueError:
                    logger.debug(
                        "Could not parse keyframe time in %s", source, exc_info=True
                    )
        if len(keyframe_times) < 2:
            return False
        avg_interval = (keyframe_times[-1] - keyframe_times[0]) / (
            len(keyframe_times) - 1
        )
        return avg_interval <= 2.0
    except Exception:
        logger.debug("Could not detect keyframe interval for %s", source, exc_info=True)
        return False


def _generate_video_previews(
    video_items: list[dict],
    preview_dir: Path,
    *,
    force: bool = False,
    progress_callback: ProgressCallback = None,
) -> None:
    """Generate one full-length preview per video (480p 1fps + audio)."""
    from ..utils.parallel import run_parallel

    encoder = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "34"]
    max_workers = max(4, (os.cpu_count() or 4) // 2)

    # When force=True, delete all existing previews so they get regenerated
    if force:
        deleted = 0
        for old in preview_dir.glob("preview_*.mp4"):
            old.unlink()
            deleted += 1
        # Also delete mega-preview cache
        for meta_file in preview_dir.glob("_mega_preview.*"):
            meta_file.unlink()
            deleted += 1
        if deleted:
            logger.info("Force: deleted %d cached preview files", deleted)

    # Clean orphaned previews
    current_cids = {cache_id(vi["local_path"]) for vi in video_items}
    for old in preview_dir.glob("preview_*.mp4"):
        if old.name.startswith("_"):
            continue
        pid = old.stem.replace("preview_", "").split("_n")[0]
        if pid not in current_cids:
            old.unlink()

    tasks: list[tuple[Path, list[str]]] = []
    for vi in video_items:
        source = Path(vi["local_path"])
        duration = vi.get("video_duration", 0)
        if not source.exists() or duration <= 0:
            continue
        preview_path = preview_dir / f"preview_{cache_id(vi['local_path'])}.mp4"
        if not preview_path.exists():
            skip = ["-skip_frame", "nokey"] if _has_dense_keyframes(source) else []
            tasks.append(
                (
                    preview_path,
                    [
                        "ffmpeg",
                        "-y",
                        "-hwaccel",
                        "auto",
                        *skip,
                        "-i",
                        str(source),
                        "-vf",
                        "fps=1,scale=480:-2",
                        *encoder,
                        "-c:a",
                        "aac",
                        "-b:a",
                        "64k",
                        "-ac",
                        "1",
                        str(preview_path),
                    ],
                )
            )

    cached = len(video_items) - len(tasks)
    if cached:
        logger.info("Video previews: %d cached, %d to generate", cached, len(tasks))
    if not tasks:
        return

    logger.info("Generating %d video previews (CPU x%d)...", len(tasks), max_workers)

    def _progress(done, total):
        if progress_callback:
            progress_callback(done, total, "generating previews")

    def _run_preview(cmd, path=None):
        result = run_subprocess(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            err_lines = [
                line
                for line in (result.stderr or "").strip().splitlines()
                if line.strip()
            ]
            err_msg = err_lines[-1].strip() if err_lines else "unknown error"
            logger.warning(
                "Preview failed for %s: %s", Path(path).name if path else "?", err_msg
            )
            # Remove corrupt partial output
            if path and Path(path).exists():
                Path(path).unlink(missing_ok=True)
        return result

    parallel_tasks = [
        (p, lambda cmd=cmd, p=p: _run_preview(cmd, path=p)) for p, cmd in tasks
    ]
    run_parallel(parallel_tasks, max_workers, progress_fn=_progress)

    n_ok = sum(1 for p, _ in tasks if p.exists() and p.stat().st_size > 500)
    n_failed = len(tasks) - n_ok
    logger.info("  Video previews done: %d/%d OK", n_ok, len(tasks))
    if n_failed:
        logger.warning("  Video previews: %d failed (check warnings above)", n_failed)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _prepare_video(
    entry: dict[str, Any],
    local_path: Path,
    i: int,
    total: int,
) -> None:
    """Probe video duration, dimensions, fps, and orientation."""
    probe = run_subprocess(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,r_frame_rate",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(local_path),
        ],
        capture_output=True,
        text=True,
    )
    total_duration = 10.0
    video_width = 0
    video_height = 0
    video_fps = 0.0
    try:
        probe_data = json.loads(probe.stdout)
        total_duration = float(probe_data.get("format", {}).get("duration", 10.0))
        streams = probe_data.get("streams", [])
        if streams:
            video_width = int(streams[0].get("width", 0))
            video_height = int(streams[0].get("height", 0))
            fps_str = streams[0].get("r_frame_rate", "0/1")
            num, den = fps_str.split("/")
            video_fps = round(int(num) / max(int(den), 1), 1)
    except (ValueError, AttributeError, json.JSONDecodeError, KeyError):
        logger.warning("Could not probe metadata for %s, assuming 10s", local_path)

    orientation = "landscape"
    if video_width > 0 and video_height > 0 and video_height > video_width:
        orientation = "portrait"

    entry["video_duration"] = round(total_duration, 1)
    entry["video_width"] = video_width
    entry["video_height"] = video_height
    entry["video_fps"] = video_fps
    entry["video_orientation"] = orientation

    res_str = f"{video_width}x{video_height}" if video_width else "?"
    logger.debug(
        "[%d/%d] %s — %.0fs %s %sfps %s",
        i,
        total,
        Path(entry["local_path"]).name,
        total_duration,
        res_str,
        video_fps,
        orientation,
    )


def _read_exif(path: str | Path) -> dict[str, Any]:
    """Extract EXIF metadata from a photo (supports JPEG, HEIC, etc.)."""
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS

        img = Image.open(path)
        # Use getexif() public API — works for HEIC via pillow-heif
        exif_ifd = img.getexif()
        if not exif_ifd:
            return {}
        exif = {TAGS.get(k, k): v for k, v in exif_ifd.items()}
        # EXIF sub-IFD (tag 0x8769) contains FocalLength, FNumber, ISO etc.
        exif_sub = exif_ifd.get_ifd(0x8769)
        if exif_sub:
            exif.update({TAGS.get(k, k): v for k, v in exif_sub.items()})
        result = {}
        focal_length = exif.get("FocalLength")
        if focal_length:
            result["focal_length"] = (
                float(focal_length)
                if not hasattr(focal_length, "numerator")
                else focal_length.numerator / focal_length.denominator
            )
        f_number = exif.get("FNumber")
        if f_number:
            result["aperture"] = (
                float(f_number)
                if not hasattr(f_number, "numerator")
                else f_number.numerator / f_number.denominator
            )
        iso = exif.get("ISOSpeedRatings")
        if iso:
            result["iso_speed"] = int(iso)
        return result
    except Exception as e:
        import warnings

        warnings.warn(f"EXIF read failed for {path}: {e}")
        return {}


def _prepare_photo(entry: dict[str, Any], local_path: Path, cfg: Config) -> None:
    """Generate thumbnail and extract EXIF for a photo."""
    thumb = generate_thumbnail(local_path, cfg.thumbnails_dir, size=400, quality=70)
    exif = _read_exif(local_path)
    if thumb:
        entry["thumbnail_path"] = str(thumb)
    if exif:
        entry["exif"] = exif
