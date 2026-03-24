"""Stage 2: Prepare media for visual planning.

Merges preprocess + analyze into one stage:
- Family member auto-detection + family_count per item
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
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config
from .image_utils import generate_thumbnail
from .media_utils import run_subprocess

logger = logging.getLogger("vlog.prepare")


@dataclass
class PrepareConfig:
    force: bool = False
    tz_hours: int | None = None
    family_names: list[str] | None = None


# Default timezone: system local (replaces hardcoded SGT)
_LOCAL_TZ = datetime.now(timezone.utc).astimezone().tzinfo

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v"}


def load_analysis(cfg: Config) -> list[dict]:
    """Reconstruct analysis data from manifest + per-item caches.

    This replaces the old analysis.json file — the per-item cache is the
    source of truth, manifest provides the item list and base metadata.
    """
    if not cfg.manifest_path.exists():
        raise FileNotFoundError(
            f"Manifest not found: {cfg.manifest_path}\n"
            "Run the prepare stage first (e.g. vlog prepare -s local -p ./photos)"
        )
    manifest = json.loads(cfg.manifest_path.read_text())

    # Reload family names from preprocessed.json (computed during prepare)
    family_names: list[str] = []
    if cfg.preprocessed_path.exists():
        pp = json.loads(cfg.preprocessed_path.read_text())
        family_names = pp.get("family_names", [])

    cache_dir = cfg.cache_dir
    results = []
    for item in manifest:
        item_id = item["id"]
        local_path_str = item.get("local_path", "")
        suffix = Path(local_path_str).suffix.lower() if local_path_str else ""
        is_video = suffix in VIDEO_EXTENSIONS

        persons = item.get("metadata", {}).get("persons", [])
        family_in_photo = [p for p in persons if p in family_names]

        entry = {
            "id": item_id,
            "filename": item["filename"],
            "local_path": local_path_str,
            "media_type": "video" if is_video else "photo",
            "taken_iso": item["taken_iso"],
            "duration_ms": item.get("duration"),
            "family_count": len(family_in_photo),
            "persons": persons,
            "country": item.get("country"),
            "first_level": item.get("first_level"),
            "district": item.get("district") or item.get("city"),
        }

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
    cfg: Config, pc: PrepareConfig | None = None, *, progress_callback=None
) -> dict:
    """Prepare all media for Gemini visual planning.

    1. Read manifest, detect family, build timeline
    2. Generate thumbnails + EXIF for photos, probe duration for videos
    3. Save preprocessed.json + analysis.json
    """
    if pc is None:
        pc = PrepareConfig()
    cfg.ensure_dirs()
    if not cfg.manifest_path.exists():
        raise FileNotFoundError(
            f"Manifest not found: {cfg.manifest_path}\n"
            "Run the fetch stage first (e.g. vlog full -s local -p ./photos ...)"
        )
    manifest = json.loads(cfg.manifest_path.read_text())

    # --- Family detection ---
    family_names = pc.family_names
    if not family_names:
        family_names = _detect_family(manifest)
    logger.info(f"Family members: {family_names}")

    for item in manifest:
        persons = item.get("metadata", {}).get("persons", [])
        family_in_photo = [p for p in persons if p in family_names]
        item["family_count"] = len(family_in_photo)
        item["family_names"] = family_in_photo

    # --- Build timeline ---
    if pc.tz_hours is not None:
        tz = timezone(timedelta(hours=pc.tz_hours))
    else:
        tz = _LOCAL_TZ
    timeline = _build_timeline(manifest, tz=tz)

    preprocessed = {
        "family_names": family_names,
        "timeline": timeline,
    }
    pp_path = cfg.preprocessed_path
    pp_path.write_text(json.dumps(preprocessed, indent=2))
    logger.info(
        f"Timeline: {len(timeline)} days, {sum(len(d['chapters']) for d in timeline)} chapters"
    )

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
                cached = json.loads(cache_file.read_text())
                if is_video and "audio_level" not in cached:
                    logger.info(
                        f"[{i}/{len(manifest)}] {item['filename']} — upgrading cached video metadata"
                    )
                else:
                    continue  # cache hit — skip
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Corrupt cache for item {item_id}, re-analyzing: {e}")

        entry = {
            "id": item_id,
            "filename": item["filename"],
            "local_path": local_path_str,
            "media_type": "video" if is_video else "photo",
            "taken_iso": item["taken_iso"],
            "duration_ms": item.get("duration"),
            "family_count": item.get("family_count", 0),
            "persons": item.get("metadata", {}).get("persons", []),
            "country": item.get("country"),
            "first_level": item.get("first_level"),
            "district": item.get("district") or item.get("city"),
        }

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

    n_photos = sum(
        1
        for item in manifest
        if Path(item.get("local_path", "")).suffix.lower() not in VIDEO_EXTENSIONS
    )
    n_videos = len(manifest) - n_photos
    logger.info(
        f"Prepared: {len(manifest)} items ({n_photos} photos, {n_videos} videos, "
        f"{len(uncached_photos)} + {len(uncached_videos)} newly analyzed)"
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

    return preprocessed


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
    progress_callback=None,
) -> None:
    """Generate one full-length preview per video (480p 1fps + audio)."""
    from .parallel import run_parallel

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
            logger.info(f"Force: deleted {deleted} cached preview files")

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
        logger.info(f"Video previews: {cached} cached, {len(tasks)} to generate")
    if not tasks:
        return

    logger.info(f"Generating {len(tasks)} video previews (CPU x{max_workers})...")

    def _progress(done, total):
        if progress_callback:
            progress_callback(done, total, "videos")
        if done % 20 == 0 or done == total:
            logger.info(f"  Video previews: {done}/{total}")

    def _run_preview(cmd, path=None):
        result = run_subprocess(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.warning(
                "Preview failed for %s: %s", path, (result.stderr or "")[-200:]
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
    logger.info(f"  Video previews done: {n_ok}/{len(tasks)} OK")
    if n_failed:
        logger.warning(f"  Video previews: {n_failed} failed (check warnings above)")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _detect_family(manifest: list[dict], top_n: int = 5) -> list[str]:
    """Auto-detect the most frequent persons as family members."""
    counts: dict[str, int] = defaultdict(int)
    for item in manifest:
        for name in item.get("metadata", {}).get("persons", []):
            counts[name] += 1
    ranked = sorted(counts.items(), key=lambda x: -x[1])
    threshold = max(len(manifest) * 0.03, 5)
    return [name for name, c in ranked[:top_n] if c >= threshold]


def _build_timeline(items: list[dict], tz=None) -> list[dict]:
    """Group items into day -> time_block -> location chapters."""
    if tz is None:
        tz = _LOCAL_TZ
    days: dict[str, list[dict]] = defaultdict(list)

    for item in items:
        t = item.get("takentime")
        if not t:
            continue
        dt = datetime.fromtimestamp(t, tz=tz)
        day_key = dt.strftime("%Y-%m-%d")

        hour = dt.hour
        if hour < 6:
            block = "early_morning"
        elif hour < 12:
            block = "morning"
        elif hour < 17:
            block = "afternoon"
        else:
            block = "evening"

        location = (
            item.get("district")
            or item.get("first_level")
            or item.get("country")
            or "unknown"
        )

        days[day_key].append(
            {
                "item_id": item["id"],
                "time_block": block,
                "location": location,
                "family_count": item.get("family_count", 0),
            }
        )

    timeline = []
    for day in sorted(days.keys()):
        day_items = days[day]
        seen_chapters: dict[tuple[str, str], list] = {}
        for di in day_items:
            key = (di["time_block"], di["location"])
            if key not in seen_chapters:
                seen_chapters[key] = []
            seen_chapters[key].append(di)

        chapters = []
        block_order = {"early_morning": 0, "morning": 1, "afternoon": 2, "evening": 3}
        for (block, location), chapter_items in sorted(
            seen_chapters.items(), key=lambda x: block_order.get(x[0][0], 9)
        ):
            chapters.append(
                {
                    "time_block": block,
                    "location": location,
                    "item_ids": [i["item_id"] for i in chapter_items],
                }
            )

        dt_obj = datetime.strptime(day, "%Y-%m-%d")
        timeline.append(
            {
                "date": day,
                "day_name": dt_obj.strftime("%A"),
                "chapters": chapters,
                "total_items": len(day_items),
            }
        )

    return timeline


def _prepare_video(entry, item_id, local_path, cache_file, i, total):
    """Probe video duration, dimensions, fps, orientation, and audio loudness."""
    logger.info(f"[{i}/{total}] {entry['filename']} — video metadata...")

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
        logger.warning(f"Could not probe metadata for {local_path}, assuming 10s")

    orientation = "landscape"
    if video_width > 0 and video_height > 0 and video_height > video_width:
        orientation = "portrait"

    entry["video_duration"] = round(total_dur, 1)
    entry["video_width"] = video_width
    entry["video_height"] = video_height
    entry["video_fps"] = video_fps
    entry["video_orientation"] = orientation

    # Probe audio loudness (integrated loudness via loudnorm filter)
    audio_level = _probe_audio_level(local_path)
    entry["audio_level"] = audio_level

    cache_entry = {
        "video_duration": round(total_dur, 1),
        "video_width": video_width,
        "video_height": video_height,
        "video_fps": video_fps,
        "video_orientation": orientation,
        "audio_level": audio_level,
    }
    cache_file.write_text(json.dumps(cache_entry, indent=2))

    res_str = f"{video_width}x{video_height}" if video_width else "?"
    logger.info(
        f"[{i}/{total}] {entry['filename']} — {total_dur:.0f}s {res_str} "
        f"{video_fps}fps {orientation} audio={audio_level}"
    )


def _probe_audio_level(local_path) -> str:
    """Probe integrated audio loudness. Returns 'silent', 'quiet', 'normal', or 'loud'.

    Uses ffmpeg loudnorm filter in measure-only mode on the first 30s.
    """
    probe = run_subprocess(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(local_path),
            "-t",
            "30",
            "-af",
            "loudnorm=print_format=json",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    try:
        # loudnorm prints JSON to stderr after processing
        stderr = probe.stderr
        # Find the JSON block in stderr
        json_start = stderr.rfind("{")
        json_end = stderr.rfind("}") + 1
        if json_start >= 0 and json_end > json_start:
            loudness_data = json.loads(stderr[json_start:json_end])
            input_i = float(loudness_data.get("input_i", -70))
            if input_i < -40:
                return "silent"
            elif input_i < -28:
                return "quiet"
            elif input_i < -10:
                return "normal"
            else:
                return "loud"
    except (ValueError, json.JSONDecodeError, KeyError):
        logger.debug("Could not parse audio loudness for %s", local_path, exc_info=True)
    return "unknown"


def _read_exif(path) -> dict:
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


def _prepare_photo(entry, item_id, local_path, cfg, cache_file):
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
