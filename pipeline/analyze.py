"""Stage 2b: Prepare media for visual planning — thumbnails, keyframes, transcripts.

Generates thumbnails for photos and keyframes for videos so Gemini can see them
in contact sheets and filmstrips during the plan stage. No local AI models needed.
"""

from __future__ import annotations

import json
from pathlib import Path

from tqdm import tqdm

from .config import Config
from .media_utils import run_subprocess, generate_thumbnail

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v"}


def analyze(cfg: Config, *, progress_callback=None, log_fn=None, **_kwargs) -> list[dict]:
    """Generate thumbnails for photos and keyframes + transcripts for videos.

    All items are included (Gemini decides what's worth using). Results are cached
    per-file in the shared analysis_cache directory.
    """
    _log = log_fn or print
    cfg.ensure_dirs()
    preprocessed_path = cfg.workspace / "preprocessed.json"
    analysis_path = cfg.workspace / "analysis.json"

    preprocessed = json.loads(preprocessed_path.read_text())
    items = preprocessed["items"]
    _log(f"Analyzing: {len(items)} items (generating thumbnails + keyframes)")

    # Load existing analysis to support resuming
    existing: dict[int, dict] = {}
    if analysis_path.exists():
        for entry in json.loads(analysis_path.read_text()):
            existing[entry["id"]] = entry

    cache_dir = cfg.cache_dir
    results = []

    import sys
    use_tqdm = hasattr(sys.stderr, "fileno") and sys.stderr.isatty()
    pbar = tqdm(total=len(items), desc="Analyzing", unit="item",
                bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",
                disable=not use_tqdm)

    for i, item in enumerate(items, 1):
        item_id = item["id"]

        # Resume: skip already-analyzed items
        if item_id in existing:
            results.append(existing[item_id])
            pbar.update(1)
            continue

        local_path = Path(item["local_path"])
        suffix = local_path.suffix.lower()
        is_video = suffix in VIDEO_EXTENSIONS

        entry = {
            "id": item_id,
            "filename": item["filename"],
            "local_path": item["local_path"],
            "media_type": "video" if is_video else "photo",
            "item_type": item.get("item_type", 0),
            "takentime": item.get("takentime"),
            "taken_iso": item.get("taken_iso"),
            "duration_ms": item.get("duration"),
            "tier": item["tier"],
            "family_count": item.get("family_count", 0),
            "family_names": item.get("family_names", []),
            "country": item.get("country"),
            "first_level": item.get("first_level"),
            "district": item.get("district"),
            "persons": item.get("metadata", {}).get("persons", []),
            "cluster_size": item.get("cluster_size", 1),
        }

        # Check shared per-file cache
        cache_file = cache_dir / f"{item_id}.json"
        if cache_file.exists():
            try:
                cached = json.loads(cache_file.read_text())
                entry.update(cached)
                results.append(entry)
                pbar.update(1)
                continue
            except (json.JSONDecodeError, KeyError):
                pass

        if is_video:
            _analyze_video(entry, item_id, local_path, cfg, cache_file, _log, i, len(items))
        else:
            _analyze_photo(entry, item_id, local_path, cfg, cache_file)

        results.append(entry)
        pbar.update(1)
        if progress_callback:
            progress_callback(i, len(items), item["filename"])

        # Save incrementally every 20 items
        if i % 20 == 0:
            analysis_path.write_text(json.dumps(results, indent=2))

    pbar.close()

    # Final save
    analysis_path.write_text(json.dumps(results, indent=2))
    _log(f"Analysis complete: {len(results)} items")
    return results


def _analyze_video(entry, item_id, local_path, cfg, cache_file, log_fn, i, total):
    """Extract keyframes, build scenes, and optionally transcribe a video."""
    log_fn(f"[{i}/{total}] {entry['filename']} — video keyframes...")

    probe = run_subprocess(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(local_path)],
        capture_output=True, text=True,
    )
    try:
        total_dur = float(probe.stdout.strip())
    except (ValueError, AttributeError):
        total_dur = 10.0

    # Extract 5 keyframes in a single FFmpeg pass
    kf_dir = cfg.keyframes_dir
    kf_dir.mkdir(parents=True, exist_ok=True)
    kf_pattern = kf_dir / f"{item_id}_%02d.jpg"
    fps_val = 5.0 / max(total_dur, 1.0)
    existing_kfs = sorted(kf_dir.glob(f"{item_id}_*.jpg"))
    if len(existing_kfs) < 3:
        run_subprocess(
            ["ffmpeg", "-y", "-i", str(local_path),
             "-vf", f"fps={fps_val:.6f},scale=512:-1",
             "-frames:v", "5", "-q:v", "3",
             str(kf_pattern)],
            capture_output=True,
        )
        existing_kfs = sorted(kf_dir.glob(f"{item_id}_*.jpg"))

    entry["keyframe_paths"] = [str(p) for p in existing_kfs]
    entry["video_duration"] = round(total_dur, 1)

    # Build scene entries from keyframes for filmstrip
    interval = total_dur / max(len(existing_kfs), 1)
    entry["scenes"] = [
        {
            "scene_index": idx,
            "start": round(idx * interval, 1),
            "end": round((idx + 1) * interval, 1),
            "duration": round(interval, 1),
            "motion": "unknown",
            "keyframe": str(kf),
        }
        for idx, kf in enumerate(existing_kfs)
    ]

    # Save to shared cache (has_speech added in separate pass)
    cache_entry = {k: v for k, v in entry.items()
                   if k in ("keyframe_paths", "scenes", "video_duration", "thumbnail_path")}
    cache_file.write_text(json.dumps(cache_entry, indent=2))

    log_fn(f"[{i}/{total}] {entry['filename']} — {len(existing_kfs)} keyframes ({total_dur:.0f}s)")



def _analyze_photo(entry, item_id, local_path, cfg, cache_file):
    """Generate thumbnail for a photo."""
    thumb_dir = cfg.workspace / "thumbnails"
    thumb = generate_thumbnail(local_path, thumb_dir, size=512)
    entry["thumbnail_path"] = str(thumb)
    cache_file.write_text(json.dumps({"thumbnail_path": str(thumb)}, indent=2))


