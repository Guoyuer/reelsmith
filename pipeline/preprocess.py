"""Preprocess: assign tiers by family presence, cluster near-duplicates, build timeline."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass  # HEIC support unavailable; handled by convert_heic fallback chain

from .config import Config

SGT = timezone(timedelta(hours=8))

SKIP_PREFIXES = ("screenshot", "screen_", "pano_")


def preprocess(cfg: Config, *, family_names: list[str] | None = None, log_fn=None) -> dict:
    """Read manifest, assign tiers, cluster duplicates, build timeline."""
    _log = log_fn or print
    cfg.ensure_dirs()
    manifest = json.loads((cfg.workspace / "manifest.json").read_text())

    # Auto-detect family members if not specified
    if not family_names:
        family_names = _detect_family(manifest)
    _log(f"Family members: {family_names}")

    # Assign tiers
    for item in manifest:
        persons = item.get("metadata", {}).get("persons", [])
        family_in_photo = [p for p in persons if p in family_names]
        item["family_count"] = len(family_in_photo)
        item["family_names"] = family_in_photo

        # Check for skip-worthy files
        fname_lower = item["filename"].lower()
        is_skip = any(fname_lower.startswith(p) for p in SKIP_PREFIXES)

        is_video = item.get("item_type") in (1, 3, 6)  # video, live, motion

        if is_skip:
            item["tier"] = "D"
        elif len(family_in_photo) >= 2:
            item["tier"] = "A"
        elif len(family_in_photo) == 1:
            item["tier"] = "B"
        elif is_video or item.get("district") or item.get("first_level") or item.get("country"):
            # Videos are always at least tier C (valuable B-roll)
            item["tier"] = "C"
        else:
            item["tier"] = "D"

        _log(f"[tier] {item['filename']}: {item['tier']} (family: {len(family_in_photo)})")

    # Cluster near-duplicates using time+location and visual similarity.
    _log("Clustering near-duplicates...")
    clusters = _cluster_items(manifest, _log)

    # Pick best representative from each cluster
    tier_rank = {"A": 0, "B": 1, "C": 2, "D": 3}
    selected = []
    for cluster in clusters:
        cluster.sort(key=lambda x: (
            tier_rank.get(x["tier"], 9),
            -x["family_count"],
            -(x.get("filesize") or 0),
        ))
        best = cluster[0]
        best["cluster_size"] = len(cluster)
        if len(cluster) > 1:
            best["cluster_alt_ids"] = [c["id"] for c in cluster[1:]]
        selected.append(best)

    # Build timeline
    timeline = _build_timeline(selected)

    # Stats
    tier_counts: dict[str, int] = defaultdict(int)
    for item in selected:
        tier_counts[item["tier"]] += 1

    result = {
        "family_names": family_names,
        "total_items": len(manifest),
        "selected_items": len(selected),
        "tier_counts": dict(tier_counts),
        "timeline": timeline,
        "items": selected,
    }

    out_path = cfg.workspace / "preprocessed.json"
    out_path.write_text(json.dumps(result, indent=2))

    _log(f"Preprocessed: {len(manifest)} → {len(selected)} unique moments")
    _log(f"Tiers: A={tier_counts.get('A',0)} B={tier_counts.get('B',0)} "
         f"C={tier_counts.get('C',0)} D={tier_counts.get('D',0)}")
    _log(f"Timeline: {len(timeline)} days, "
         f"{sum(len(d['chapters']) for d in timeline)} chapters")
    return result


def _detect_family(manifest: list[dict], top_n: int = 5) -> list[str]:
    """Auto-detect the most frequent persons as family members."""
    counts: dict[str, int] = defaultdict(int)
    for item in manifest:
        for name in item.get("metadata", {}).get("persons", []):
            counts[name] += 1
    ranked = sorted(counts.items(), key=lambda x: -x[1])
    # Keep persons appearing in at least 3% of photos
    threshold = max(len(manifest) * 0.03, 5)
    return [name for name, c in ranked[:top_n] if c >= threshold]


def _build_timeline(items: list[dict]) -> list[dict]:
    """Group items into day → time_block → location chapters."""
    days: dict[str, list[dict]] = defaultdict(list)

    for item in items:
        t = item.get("takentime")
        if not t:
            continue
        dt = datetime.fromtimestamp(t, tz=SGT)
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
            "tier": item["tier"],
            "family_count": item["family_count"],
            "time": dt.strftime("%H:%M"),
        })

    timeline = []
    for day in sorted(days.keys()):
        day_items = days[day]
        # Group by (time_block, location) preserving order
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
            a_count = sum(1 for i in chapter_items if i["tier"] == "A")
            chapters.append({
                "time_block": block,
                "location": location,
                "item_ids": [i["item_id"] for i in chapter_items],
                "count": len(chapter_items),
                "family_together": a_count,
            })

        dt = datetime.strptime(day, "%Y-%m-%d")
        timeline.append({
            "date": day,
            "day_name": dt.strftime("%A"),
            "chapters": chapters,
            "total_items": len(day_items),
        })

    return timeline


# ---------------------------------------------------------------------------
# Visual deduplication via histogram similarity
# ---------------------------------------------------------------------------

def _cluster_items(items: list[dict], log_fn) -> list[list[dict]]:
    """Cluster near-duplicate items using time+location then visual similarity.

    Two-pass approach:
    1. Group by time proximity (120s) + same location (when available)
    2. Within each time group, merge items with high visual similarity (HSV histogram)

    Falls back to time-only grouping if OpenCV is not installed.
    """
    try:
        import cv2 as _cv2
    except ImportError:
        _cv2 = None

    # Pass 1: time-based pre-grouping (fast, reduces O(n²) comparisons)
    sorted_items = sorted(items, key=lambda x: x.get("takentime") or 0)
    time_groups: list[list[dict]] = []
    current: list[dict] = []

    for item in sorted_items:
        t = item.get("takentime") or 0
        if current:
            prev_t = current[-1].get("takentime") or 0
            if t - prev_t > 120:
                time_groups.append(current)
                current = []
        current.append(item)
    if current:
        time_groups.append(current)

    # Pass 2: within each time group, merge visually similar items
    clusters: list[list[dict]] = []
    for group in time_groups:
        if len(group) <= 1:
            clusters.append(group)
            continue

        if _cv2 is None:
            # No OpenCV — treat entire time group as one cluster
            clusters.append(group)
            continue

        # Compute HSV histograms — if none can be computed, keep as one cluster
        hists = [_compute_hist(Path(item.get("local_path", ""))) for item in group]
        if all(h is None for h in hists):
            clusters.append(group)
            continue

        # Union-find by visual similarity
        n = len(group)
        parent = list(range(n))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for i in range(n):
            if hists[i] is None:
                continue
            for j in range(i + 1, n):
                if hists[j] is None:
                    continue
                sim = _cv2.compareHist(hists[i], hists[j], _cv2.HISTCMP_CORREL)
                if sim > 0.75:
                    pi, pj = find(i), find(j)
                    if pi != pj:
                        parent[pi] = pj

        sub: dict[int, list[dict]] = defaultdict(list)
        for i in range(n):
            sub[find(i)].append(group[i])
        clusters.extend(sub.values())

    merged = sum(1 for c in clusters if len(c) > 1)
    log_fn(f"Clustering: {len(items)} items → {len(clusters)} clusters ({merged} merged)")
    return clusters


def _compute_hist(path: Path):
    """Compute HSV color histogram for an image. Returns None on failure."""
    try:
        import cv2
    except ImportError:
        return None
    try:
        # Try OpenCV first (fast, handles JPG/PNG)
        img = cv2.imread(str(path))
        if img is None:
            # Fallback for HEIC: use PIL to convert
            pil_img = Image.open(path).convert("RGB").resize((64, 64))
            import numpy as np
            img = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
        else:
            img = cv2.resize(img, (64, 64))
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [16, 16], [0, 180, 0, 256])
        cv2.normalize(hist, hist)
        return hist
    except Exception:
        return None
