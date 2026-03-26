"""Stage 2: Prepare media for visual planning.

Merges previous preprocess + analyze into one stage:
- Timeline construction (day → time_block → location)
- Photo thumbnails (400px JPEG, cached — used directly by plan stage)
- EXIF extraction (cached)
- Video duration probing (cached)

All results cached per-file in shared analysis_cache directory.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .._types import VIDEO_EXTENSIONS, AnalysisEntry
from ..config import Config, ProgressCallback
from ..utils.image import generate_thumbnail
from ..utils.media import run_subprocess

logger = logging.getLogger("vlog.prepare")


@dataclass
class PrepareConfig:
    force: bool = False


def _base_analysis_entry(item: dict, *, is_video: bool) -> dict:
    """Build the common analysis entry dict from a manifest item."""
    return {
        "id": item["id"],
        "filename": item["filename"],
        "local_path": item.get("local_path", ""),
        "media_type": "video" if is_video else "photo",
        "taken_iso": item["taken_iso"],
        "country": item.get("country"),
        "first_level": item.get("first_level"),
        "district": item.get("district") or item.get("city"),
    }


def load_analysis(cfg: Config) -> list[AnalysisEntry]:
    """Reconstruct analysis data from manifest + per-item caches."""
    if not cfg.manifest_path.exists():
        raise FileNotFoundError(
            f"Manifest not found: {cfg.manifest_path}\n"
            "Run the prepare stage first (e.g. vlog prepare -p ./photos)"
        )
    manifest = json.loads(cfg.manifest_path.read_text())

    cache_dir = cfg.cache_dir
    results = []
    for item in manifest:
        item_id = item["id"]
        local_path_str = item.get("local_path", "")
        suffix = Path(local_path_str).suffix.lower() if local_path_str else ""
        is_video = suffix in VIDEO_EXTENSIONS

        entry = _base_analysis_entry(item, is_video=is_video)

        cache_file = cache_dir / f"{item_id}.json"
        if cache_file.exists():
            try:
                cached = json.loads(cache_file.read_text())
                entry.update(cached)
            except (json.JSONDecodeError, KeyError):
                pass

        results.append(entry)
    return results


def prepare(
    cfg: Config,
    pc: PrepareConfig | None = None,
    *,
    progress_callback: ProgressCallback = None,
) -> None:
    """Prepare all media for Gemini visual planning.

    1. Read manifest
    2. Generate thumbnails + EXIF for photos, probe duration for videos
    3. Generate video previews (360p 1fps)
    """
    if pc is None:
        pc = PrepareConfig()
    cfg.ensure_dirs()
    if not cfg.manifest_path.exists():
        raise FileNotFoundError(
            f"Manifest not found: {cfg.manifest_path}\n"
            "Run the fetch stage first (e.g. vlog full -p ./photos ...)"
        )
    manifest = json.loads(cfg.manifest_path.read_text())

    # --- Analyze: thumbnails, EXIF, video duration ---
    cache_dir = cfg.cache_dir

    # --- Phase 1: Scan — check caches, build entries, collect uncached items ---
    uncached_photos: list[
        tuple[dict, int, Path, Path]
    ] = []  # (entry, item_id, path, cache_file)
    uncached_videos: list[tuple[dict, int, Path, Path]] = []

    for i, item in enumerate(manifest, 1):
        item_id = item["id"]
        if progress_callback:
            progress_callback(i, len(manifest), "scan")

        local_path_str = item.get("local_path", "")
        local_path = Path(local_path_str)
        if not local_path.exists():
            continue

        suffix = local_path.suffix.lower()
        is_video = suffix in VIDEO_EXTENSIONS

        cache_file = cache_dir / f"{item_id}.json"
        if cache_file.exists() and not pc.force:
            try:
                json.loads(cache_file.read_text())  # validate not corrupt
                continue  # cache hit — skip
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(
                    "Corrupt cache for item %s, re-analyzing: %s", item_id, e
                )

        entry = _base_analysis_entry(item, is_video=is_video)

        if is_video:
            uncached_videos.append((entry, item_id, local_path, cache_file))
        else:
            uncached_photos.append((entry, item_id, local_path, cache_file))

    # --- Phase 2: Prepare photos — EXIF + thumbnails ---
    for i, (entry, item_id, local_path, cache_file) in enumerate(uncached_photos, 1):
        if progress_callback:
            progress_callback(i, len(uncached_photos), "photos")
        _prepare_photo(entry, item_id, local_path, cfg, cache_file)

    # --- Phase 3: Probe uncached videos ---
    for i, (entry, item_id, local_path, cache_file) in enumerate(uncached_videos, 1):
        if progress_callback:
            progress_callback(i, len(uncached_videos), "video probe")
        _prepare_video(entry, item_id, local_path, cache_file, i, len(uncached_videos))

    # Rich video probe summary table
    from ..utils import stderr_console

    console = stderr_console()
    if console and uncached_videos:
        from rich.table import Table

        t = Table(
            title=f"Video Probe ({len(uncached_videos)} new)",
            border_style="dim",
            show_lines=False,
        )
        t.add_column("File", max_width=35)
        t.add_column("Duration", justify="right")
        t.add_column("Resolution")
        t.add_column("FPS", justify="right")
        for entry, _, _, _ in uncached_videos[:20]:
            t.add_row(
                entry["filename"][:35],
                f"{entry.get('video_duration', 0):.0f}s",
                f"{entry.get('video_width', '?')}x{entry.get('video_height', '?')}",
                f"{entry.get('video_fps', '?')}",
            )
        if len(uncached_videos) > 20:
            t.add_row(f"... +{len(uncached_videos) - 20} more", "", "", "")
        console.print(t)

    n_photos = sum(
        1
        for item in manifest
        if Path(item.get("local_path", "")).suffix.lower() not in VIDEO_EXTENSIONS
    )
    n_videos = len(manifest) - n_photos
    n_new = len(uncached_photos) + len(uncached_videos)
    cached_note = f", {n_new} new" if n_new else ", all cached"
    logger.info(
        "Prepared: %d items (%d photos, %d videos%s)",
        len(manifest),
        n_photos,
        n_videos,
        cached_note,
    )

    # Generate video previews (cached per video, used by plan stage)
    # Use load_analysis to get full entries with cache data merged
    results = load_analysis(cfg)
    video_items = [r for r in results if r.get("media_type") == "video"]
    if video_items:
        _generate_video_previews(
            video_items,
            cfg.preview_clips_dir,
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
    video_items: list[AnalysisEntry],
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
    current_ids = {str(vi["id"]) for vi in video_items}
    for old in preview_dir.glob("preview_*.mp4"):
        if old.name.startswith("_"):
            continue
        pid = old.stem.replace("preview_", "").split("_n")[0]
        if pid not in current_ids:
            old.unlink()

    tasks: list[tuple[Path, list[str]]] = []
    for vi in video_items:
        vid_id = vi["id"]
        source = Path(vi.get("local_path", ""))
        dur = vi.get("video_duration", 0)
        if not source.exists() or dur <= 0:
            continue
        preview_path = preview_dir / f"preview_{vid_id}.mp4"
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
            progress_callback(done, total, "videos")

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
    item_id: int,
    local_path: Path,
    cache_file: Path,
    i: int,
    total: int,
) -> None:
    """Probe video duration, dimensions, fps, orientation, and audio loudness."""
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
    total_dur = 10.0
    video_width = 0
    video_height = 0
    video_fps = 0.0
    try:
        probe_data = json.loads(probe.stdout)
        total_dur = float(probe_data.get("format", {}).get("duration", 10.0))
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

    entry["video_duration"] = round(total_dur, 1)
    entry["video_width"] = video_width
    entry["video_height"] = video_height
    entry["video_fps"] = video_fps
    entry["video_orientation"] = orientation

    cache_entry = {
        "video_duration": round(total_dur, 1),
        "video_width": video_width,
        "video_height": video_height,
        "video_fps": video_fps,
        "video_orientation": orientation,
    }
    cache_file.write_text(json.dumps(cache_entry, indent=2))

    res_str = f"{video_width}x{video_height}" if video_width else "?"
    logger.debug(
        "[%d/%d] %s — %.0fs %s %sfps %s",
        i,
        total,
        entry["filename"],
        total_dur,
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
        fl = exif.get("FocalLength")
        if fl:
            result["focal_length"] = (
                float(fl)
                if not hasattr(fl, "numerator")
                else fl.numerator / fl.denominator
            )
        fn = exif.get("FNumber")
        if fn:
            result["aperture"] = (
                float(fn)
                if not hasattr(fn, "numerator")
                else fn.numerator / fn.denominator
            )
        iso = exif.get("ISOSpeedRatings")
        if iso:
            result["iso"] = int(iso)
        return result
    except Exception as e:
        import warnings

        warnings.warn(f"EXIF read failed for {path}: {e}")
        return {}


def _prepare_photo(
    entry: dict[str, Any], item_id: int, local_path: Path, cfg: Config, cache_file: Path
) -> None:
    """Generate thumbnail and extract EXIF for a photo."""
    thumb_dir = cfg.thumbnails_dir
    thumb = generate_thumbnail(local_path, thumb_dir, size=400, quality=70)
    exif = _read_exif(local_path)
    cache_data = {}
    if thumb:
        cache_data["thumbnail_path"] = str(thumb)
        entry["thumbnail_path"] = str(thumb)
    if exif:
        cache_data["exif"] = exif
        entry["exif"] = exif
    cache_file.write_text(json.dumps(cache_data, indent=2))
