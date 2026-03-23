"""Stage 1: Fetch media from Synology Photos via the existing FastAPI backend."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..config import Config


@dataclass
class FetchConfig:
    source_dir: str | None = None
    from_date: str | None = None
    to_date: str | None = None
    country: str | None = None
    first_level: str | None = None
    district: str | None = None
    person_ids: list[int] | None = None
    item_types: list[int] | None = None

logger = logging.getLogger("vlog.fetch")


def fetch(cfg: Config, fc: FetchConfig) -> list[dict]:
    """Query the Synology Photos API, download all matching items, and build a manifest."""
    cfg.ensure_dirs()
    raw_dir = cfg.media_dir

    # Build collect request
    body: dict = {}
    if fc.from_date:
        body["from_date"] = fc.from_date
    if fc.to_date:
        body["to_date"] = fc.to_date
    if fc.country:
        body["country"] = fc.country
    if fc.first_level:
        body["first_level"] = fc.first_level
    if fc.district:
        body["district"] = fc.district
    if fc.person_ids:
        body["person_ids"] = fc.person_ids
    if fc.item_types:
        body["item_types"] = fc.item_types

    # Load previous manifest for metadata cache (avoids re-fetching /api/meta per item)
    manifest_path = cfg.manifest_path
    prev_meta: dict[int, dict] = {}
    if manifest_path.exists():
        try:
            for entry in json.loads(manifest_path.read_text()):
                if entry.get("metadata"):
                    prev_meta[entry["id"]] = entry["metadata"]
        except (json.JSONDecodeError, KeyError):
            pass

    with httpx.Client(base_url=cfg.api_base, timeout=30) as client:
        # Query items
        resp = client.post("/api/collect", json=body)
        resp.raise_for_status()
        data = resp.json()
        items = data["items"]
        logger.info(f"Found {data['count']} items ({data['total_mb']:.1f} MB)")

        manifest = []
        meta_cached = 0
        for i, item in enumerate(items, 1):
            item_id = item["id"]
            filename = item["filename"]
            filepath = raw_dir / f"{item_id}_{filename}"

            # Reuse cached metadata if file already downloaded
            if item_id in prev_meta and filepath.exists():
                meta = prev_meta[item_id]
                meta_cached += 1
            else:
                meta = {}
                try:
                    meta_resp = client.get(f"/api/meta/{item_id}", timeout=10)
                    if meta_resp.status_code == 200:
                        meta = meta_resp.json()
                except httpx.HTTPError as e:
                    logger.warning("metadata fetch failed for %d: %s", item_id, e)

            # Download file (skip if already exists)
            if not filepath.exists():
                logger.info(f"[{i}/{len(items)}] Downloading {filename}")
                with client.stream("GET", f"/api/media/{item_id}", timeout=600) as stream:
                    stream.raise_for_status()
                    with open(filepath, "wb") as f:
                        for chunk in stream.iter_bytes(65536):
                            f.write(chunk)
            else:
                logger.info(f"[{i}/{len(items)}] {filename} (cached)")

            # For live photos (type 3), also download the video companion
            video_path = None
            if item.get("item_type") == 3:
                video_path = raw_dir / f"{item_id}_{Path(filename).stem}.mov"
                if not video_path.exists():
                    logger.info(f"[{i}/{len(items)}] + live photo video")
                    with client.stream(
                        "GET",
                        f"/api/media/{item_id}",
                        params={"as_video": "true"},
                        timeout=600,
                    ) as stream:
                        if stream.status_code == 200:
                            with open(video_path, "wb") as f:
                                for chunk in stream.iter_bytes(65536):
                                    f.write(chunk)
                        else:
                            video_path = None

            entry = {
                **item,
                "local_path": str(filepath),
                "metadata": meta,
            }
            if video_path:
                entry["live_video_path"] = str(video_path)
            manifest.append(entry)

    manifest_path.write_text(json.dumps(manifest, indent=2))
    newly_fetched = len(items) - meta_cached
    logger.info("Manifest saved: %d items (%d metadata cached, %d fetched)", len(manifest), meta_cached, newly_fetched)
    return manifest
