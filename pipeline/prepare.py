"""Stage 2: Prepare media for visual planning.

Merges preprocess + analyze into one stage:
- Family member auto-detection + family_count per item
- Timeline construction (day → time_block → location)
- Photo thumbnails (600px, cached — matches contact sheet cell size)
- EXIF extraction (cached)
- Video duration probing (cached)

All results cached per-file in shared analysis_cache directory.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tqdm import tqdm

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

from .config import Config
from .media_utils import run_subprocess, generate_thumbnail

# Default timezone: system local (replaces hardcoded SGT)
_LOCAL_TZ = datetime.now(timezone.utc).astimezone().tzinfo

SKIP_PREFIXES = ("screenshot", "screen_", "pano_")
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v"}


def prepare(cfg: Config, *, family_names: list[str] | None = None,
            force: bool = False, progress_callback=None, log_fn=None,
            tz_hours: int | None = None,
            **_kwargs) -> dict:
    """Prepare all media for Gemini visual planning.

    1. Read manifest, detect family, build timeline
    2. Generate thumbnails + EXIF for photos, probe duration for videos
    3. Save preprocessed.json + analysis.json
    """
    _log = log_fn or print
    cfg.ensure_dirs()
    manifest = json.loads((cfg.workspace / "manifest.json").read_text())

    # --- Family detection ---
    if not family_names:
        family_names = _detect_family(manifest)
    _log(f"Family members: {family_names}")

    for item in manifest:
        persons = item.get("metadata", {}).get("persons", [])
        family_in_photo = [p for p in persons if p in family_names]
        item["family_count"] = len(family_in_photo)
        item["family_names"] = family_in_photo

    # --- Dedup burst photos ---
    before = len(manifest)
    manifest = _dedup_burst_photos(manifest, _log)
    if before != len(manifest):
        _log(f"Dedup: {before} → {len(manifest)} items ({before - len(manifest)} burst duplicates removed)")

    # --- Build timeline ---
    if tz_hours is not None:
        tz = timezone(timedelta(hours=tz_hours))
    else:
        tz = _LOCAL_TZ
    timeline = _build_timeline(manifest, tz=tz)

    preprocessed = {
        "family_names": family_names,
        "timeline": timeline,
    }
    pp_path = cfg.workspace / "preprocessed.json"
    pp_path.write_text(json.dumps(preprocessed, indent=2))
    _log(f"Timeline: {len(timeline)} days, {sum(len(d['chapters']) for d in timeline)} chapters")

    # --- Analyze: thumbnails, EXIF, video duration ---
    analysis_path = cfg.workspace / "analysis.json"

    existing: dict[int, dict] = {}
    if analysis_path.exists() and not force:
        for entry in json.loads(analysis_path.read_text()):
            existing[entry["id"]] = entry

    cache_dir = cfg.cache_dir
    results = []
    use_tqdm = hasattr(sys.stderr, "fileno") and sys.stderr.isatty()
    pbar = tqdm(total=len(manifest), desc="Preparing", unit="item",
                bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",
                disable=not use_tqdm)

    for i, item in enumerate(manifest, 1):
        item_id = item["id"]

        if item_id in existing:
            results.append(existing[item_id])
            pbar.update(1)
            continue

        local_path_str = item.get("local_path", "")
        local_path = Path(local_path_str) if local_path_str else None
        suffix = local_path.suffix.lower() if local_path else ""
        is_video = suffix in VIDEO_EXTENSIONS

        entry = {
            "id": item_id,
            "filename": item["filename"],
            "local_path": item.get("local_path", ""),
            "media_type": "video" if is_video else "photo",
            "taken_iso": item.get("taken_iso"),
            "duration_ms": item.get("duration"),
            "family_count": item.get("family_count", 0),
            "persons": item.get("metadata", {}).get("persons", []),
            "country": item.get("country"),
            "first_level": item.get("first_level"),
            "district": item.get("district"),
        }

        if not local_path or not local_path.exists():
            results.append(entry)
            pbar.update(1)
            continue

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
            _prepare_video(entry, item_id, local_path, cache_file, _log, i, len(manifest))
        else:
            _prepare_photo(entry, item_id, local_path, cfg, cache_file)

        results.append(entry)
        pbar.update(1)
        if progress_callback:
            progress_callback(i, len(manifest), item["filename"])

        if i % 20 == 0:
            analysis_path.write_text(json.dumps(results, indent=2))

    pbar.close()
    analysis_path.write_text(json.dumps(results, indent=2))

    n_photos = sum(1 for r in results if r.get("media_type") == "photo")
    n_videos = len(results) - n_photos
    newly = sum(1 for r in results if r["id"] not in existing)
    _log(f"Prepared: {len(results)} items ({n_photos} photos, {n_videos} videos, "
         f"{newly} newly analyzed)")

    return preprocessed


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _dedup_burst_photos(items: list[dict], log_fn=None) -> list[dict]:
    """Remove near-identical burst photos using time grouping + histogram similarity.

    Only deduplicates photos — videos pass through untouched.
    Two-pass approach:
    1. Group consecutive photos taken within 10 seconds (burst detection)
    2. Within each burst, compare PIL histograms. Similarity > 0.92 = duplicate.
       Keep the best (highest family_count, then largest filesize).

    Returns the filtered item list. Logs removed items for transparency.
    """
    _log = log_fn or print

    # Separate photos and non-photos
    photos = []
    non_photos = []
    for item in items:
        suffix = Path(item.get("local_path", "")).suffix.lower()
        if suffix in PHOTO_EXTENSIONS:
            photos.append(item)
        else:
            non_photos.append(item)

    if len(photos) < 2:
        return items

    # Sort photos by takentime
    photos.sort(key=lambda x: x.get("takentime") or 0)

    # Pass 1: group consecutive photos within 10 seconds
    bursts: list[list[dict]] = [[photos[0]]]
    for p in photos[1:]:
        prev_t = bursts[-1][-1].get("takentime") or 0
        curr_t = p.get("takentime") or 0
        if curr_t - prev_t <= 10:
            bursts[-1].append(p)
        else:
            bursts.append([p])

    # Pass 2: within each burst, compare histograms and keep best
    kept = []
    removed_count = 0
    for burst in bursts:
        if len(burst) <= 1:
            kept.extend(burst)
            continue

        # Compute histograms (pillow-heif handles HEIC)
        hists = []
        for p in burst:
            h = _photo_histogram(Path(p.get("local_path", "")))
            hists.append(h)

        # Cluster similar photos within the burst
        used = [False] * len(burst)
        for i in range(len(burst)):
            if used[i]:
                continue
            cluster = [i]
            used[i] = True
            if hists[i] is not None:
                for j in range(i + 1, len(burst)):
                    if used[j] or hists[j] is None:
                        continue
                    sim = _histogram_similarity(hists[i], hists[j])
                    if sim > 0.92:
                        cluster.append(j)
                        used[j] = True

            # Keep the best from this cluster
            best_idx = max(cluster, key=lambda k: (
                burst[k].get("family_count", 0),
                burst[k].get("filesize", 0),
            ))
            kept.append(burst[best_idx])
            if len(cluster) > 1:
                removed_names = [burst[k]["filename"] for k in cluster if k != best_idx]
                removed_count += len(removed_names)
                _log(f"  Dedup: kept {burst[best_idx]['filename']}, "
                     f"removed {len(removed_names)} similar: {', '.join(removed_names[:3])}"
                     f"{'...' if len(removed_names) > 3 else ''}")

    # Restore original order (by id) and add back non-photos
    result = kept + non_photos
    return result


def _photo_histogram(path: Path) -> list[int] | None:
    """Compute RGB histogram for a photo. Uses FFmpeg for HEIC conversion."""
    from PIL import Image as _Img

    # Try PIL directly first (handles JPEG, PNG, etc.)
    try:
        img = _Img.open(path).convert("RGB").resize((64, 64))
        return img.histogram()
    except Exception:
        pass

    # PIL failed — use FFmpeg to extract a single frame as JPEG
    tmp = path.parent / f"_hist_{path.stem}.jpg"
    try:
        run_subprocess(
            ["ffmpeg", "-y", "-i", str(path), "-vframes", "1",
             "-vf", "scale=64:64", "-q:v", "5", str(tmp)],
            capture_output=True, timeout=10,
        )
        if tmp.exists() and tmp.stat().st_size > 0:
            img = _Img.open(tmp).convert("RGB")
            hist = img.histogram()
            tmp.unlink(missing_ok=True)
            return hist
    except Exception:
        pass
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return None


def _histogram_similarity(h1: list[int], h2: list[int]) -> float:
    """Cosine similarity between two PIL histograms."""
    import math
    dot = sum(a * b for a, b in zip(h1, h2))
    mag1 = math.sqrt(sum(a * a for a in h1))
    mag2 = math.sqrt(sum(b * b for b in h2))
    if mag1 == 0 or mag2 == 0:
        return 0.0
    return dot / (mag1 * mag2)


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

        location = (item.get("district") or item.get("first_level")
                     or item.get("country") or "unknown")

        days[day_key].append({
            "item_id": item["id"],
            "time_block": block,
            "location": location,
            "family_count": item.get("family_count", 0),
        })

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
            chapters.append({
                "time_block": block,
                "location": location,
                "item_ids": [i["item_id"] for i in chapter_items],
            })

        dt_obj = datetime.strptime(day, "%Y-%m-%d")
        timeline.append({
            "date": day,
            "day_name": dt_obj.strftime("%A"),
            "chapters": chapters,
            "total_items": len(day_items),
        })

    return timeline


def _prepare_video(entry, item_id, local_path, cache_file, log_fn, i, total):
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

    cache_entry = {"video_duration": round(total_dur, 1)}
    cache_file.write_text(json.dumps(cache_entry, indent=2))
    log_fn(f"[{i}/{total}] {entry['filename']} — {total_dur:.0f}s")


def _read_exif(path) -> dict:
    """Extract EXIF metadata from a photo."""
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


def _prepare_photo(entry, item_id, local_path, cfg, cache_file):
    """Generate thumbnail and extract EXIF for a photo."""
    thumb_dir = cfg.thumbnails_dir
    thumb = generate_thumbnail(local_path, thumb_dir, size=600)
    exif = _read_exif(local_path)
    cache_data = {"thumbnail_path": str(thumb)}
    if exif:
        cache_data["exif"] = exif
        entry["exif"] = exif
    entry["thumbnail_path"] = str(thumb)
    cache_file.write_text(json.dumps(cache_data, indent=2))
