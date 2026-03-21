"""Stage 1: Fetch media from Synology Photos via the existing FastAPI backend."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from .config import Config


def fetch(
    cfg: Config,
    *,
    from_date: str | None = None,
    to_date: str | None = None,
    log_fn=None,
    country: str | None = None,
    first_level: str | None = None,
    district: str | None = None,
    person_ids: list[int] | None = None,
    item_types: list[int] | None = None,
) -> list[dict]:
    """Query the Synology Photos API, download all matching items, and build a manifest."""
    _log = log_fn or print
    cfg.ensure_dirs()
    raw_dir = cfg.media_dir

    # Build collect request
    body: dict = {}
    if from_date:
        body["from_date"] = from_date
    if to_date:
        body["to_date"] = to_date
    if country:
        body["country"] = country
    if first_level:
        body["first_level"] = first_level
    if district:
        body["district"] = district
    if person_ids:
        body["person_ids"] = person_ids
    if item_types:
        body["item_types"] = item_types

    # Load previous manifest for metadata cache (avoids re-fetching /api/meta per item)
    manifest_path = cfg.workspace / "manifest.json"
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
        _log(f"Found {data['count']} items ({data['total_mb']:.1f} MB)")

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
                    _log(f"  WARNING: metadata fetch failed for {item_id}: {e}")

            # Download file (skip if already exists)
            if not filepath.exists():
                _log(f"[{i}/{len(items)}] Downloading {filename}")
                with client.stream("GET", f"/api/media/{item_id}", timeout=600) as stream:
                    stream.raise_for_status()
                    with open(filepath, "wb") as f:
                        for chunk in stream.iter_bytes(65536):
                            f.write(chunk)
            else:
                _log(f"[{i}/{len(items)}] {filename} (cached)")

            # For live photos (type 3), also download the video companion
            video_path = None
            if item.get("item_type") == 3:
                video_path = raw_dir / f"{item_id}_{Path(filename).stem}.mov"
                if not video_path.exists():
                    _log(f"[{i}/{len(items)}] + live photo video")
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
    _log(f"Manifest saved: {len(manifest)} items ({meta_cached} metadata cached, {newly_fetched} fetched)")
    return manifest
