"""Preprocess: assign tiers by family presence, cluster near-duplicates, build timeline."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config

SGT = timezone(timedelta(hours=8))

SKIP_PREFIXES = ("screenshot", "screen_", "pano_")


def preprocess(cfg: Config, *, family_names: list[str] | None = None) -> dict:
    """Read manifest, assign tiers, cluster duplicates, build timeline."""
    cfg.ensure_dirs()
    manifest = json.loads((cfg.workspace / "manifest.json").read_text())

    # Auto-detect family members if not specified
    if not family_names:
        family_names = _detect_family(manifest)
    print(f"Family members: {family_names}")

    # Assign tiers
    for item in manifest:
        persons = item.get("metadata", {}).get("persons", [])
        family_in_photo = [p for p in persons if p in family_names]
        item["family_count"] = len(family_in_photo)
        item["family_names"] = family_in_photo

        # Check for skip-worthy files
        fname_lower = item["filename"].lower()
        is_skip = any(fname_lower.startswith(p) for p in SKIP_PREFIXES)

        if is_skip:
            item["tier"] = "D"
        elif len(family_in_photo) >= 2:
            item["tier"] = "A"
        elif len(family_in_photo) == 1:
            item["tier"] = "B"
        elif item.get("district") or item.get("first_level") or item.get("country"):
            item["tier"] = "C"
        else:
            item["tier"] = "D"

    # Cluster near-duplicates (within 10s window)
    items_sorted = sorted(manifest, key=lambda x: x.get("takentime") or 0)
    clusters: list[list[dict]] = []
    current: list[dict] = []

    for item in items_sorted:
        t = item.get("takentime") or 0
        if current and t - (current[-1].get("takentime") or 0) > 10:
            clusters.append(current)
            current = []
        current.append(item)
    if current:
        clusters.append(current)

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

    print(f"Preprocessed: {len(manifest)} → {len(selected)} unique moments")
    print(f"  Tiers: A={tier_counts.get('A',0)} (family together) "
          f"B={tier_counts.get('B',0)} (one family) "
          f"C={tier_counts.get('C',0)} (scene) "
          f"D={tier_counts.get('D',0)} (skip)")
    print(f"  Timeline: {len(timeline)} days, "
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
