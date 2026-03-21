"""Stage 2b: Prepare media for visual planning — thumbnails + EXIF + video metadata.

Generates thumbnails for photos, extracts EXIF, and probes video durations
so the plan stage can build contact sheets and video clip samples.
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
    """Generate thumbnails, extract EXIF, probe video durations.

    All items are included (Gemini decides what's worth using). Results are cached
    per-file in the shared analysis_cache directory.
    """
    _log = log_fn or print
    cfg.ensure_dirs()
    preprocessed_path = cfg.workspace / "preprocessed.json"
    analysis_path = cfg.workspace / "analysis.json"

    preprocessed = json.loads(preprocessed_path.read_text())
    items = preprocessed["items"]
    _log(f"Analyzing: {len(items)} items (thumbnails + EXIF + video metadata)")

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
            except (json.JSONDecodeError, KeyError) as e:
                _log(f"  WARNING: corrupt cache for item {item_id}, re-analyzing: {e}")

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


def _analyze_video(entry, item_id, local_path, _cfg, cache_file, log_fn, i, total):
    """Probe video duration and build scene count."""
    log_fn(f"[{i}/{total}] {entry['filename']} — video metadata...")

    probe = run_subprocess(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(local_path)],
        capture_output=True, text=True,
    )
    try:
        total_dur = float(probe.stdout.strip())
    except (ValueError, AttributeError):
        log_fn(f"  WARNING: could not probe duration for {local_path}, assuming 10s")
        total_dur = 10.0

    entry["video_duration"] = round(total_dur, 1)

    # Scene count (used by plan for metadata text)
    n_scenes = max(1, int(total_dur / 5))
    entry["scenes"] = [{"scene_index": i} for i in range(n_scenes)]

    cache_entry = {"video_duration": round(total_dur, 1),
                   "scenes": entry["scenes"]}
    cache_file.write_text(json.dumps(cache_entry, indent=2))

    log_fn(f"[{i}/{total}] {entry['filename']} — {total_dur:.0f}s, {n_scenes} scenes")



def _read_exif(path) -> dict:
    """Extract EXIF metadata from a photo. Returns dict with focal_length, aperture, iso."""
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS
        img = Image.open(path)
        exif_data = img._getexif()
        if not exif_data:
            return {}
        exif = {TAGS.get(k, k): v for k, v in exif_data.items()}
        result = {}
        fl = exif.get("FocalLength")
        if fl:
            result["focal_length"] = float(fl) if not hasattr(fl, 'numerator') else fl.numerator / fl.denominator
        fn = exif.get("FNumber")
        if fn:
            result["aperture"] = float(fn) if not hasattr(fn, 'numerator') else fn.numerator / fn.denominator
        iso = exif.get("ISOSpeedRatings")
        if iso:
            result["iso"] = int(iso)
        return result
    except Exception:
        return {}


def _analyze_photo(entry, item_id, local_path, cfg, cache_file):
    """Generate thumbnail and extract EXIF for a photo."""
    thumb_dir = cfg.workspace / "thumbnails"
    thumb = generate_thumbnail(local_path, thumb_dir, size=512)
    exif = _read_exif(local_path)
    cache_data = {"thumbnail_path": str(thumb)}
    if exif:
        cache_data["exif"] = exif
        entry["exif"] = exif
    entry["thumbnail_path"] = str(thumb)
    cache_file.write_text(json.dumps(cache_data, indent=2))


