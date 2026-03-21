"""Fetch media from a local folder — no NAS required.

Scans a directory for photos and videos, extracts metadata from EXIF
(date, GPS), and builds a manifest.json compatible with the rest of
the pipeline. Alternative to fetch.py (Synology NAS).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .config import Config

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v"}


def fetch_local(
    cfg: Config,
    *,
    source_dir: str,
    from_date: str | None = None,
    to_date: str | None = None,
    log_fn=None,
) -> list[dict]:
    """Scan a local folder for photos/videos and build a manifest.

    Extracts dates from EXIF (photos) or file mtime (fallback).
    Optionally filters by date range (YYYY-MM-DD).
    Files are symlinked/copied into cfg.media_dir for pipeline compatibility.
    """
    _log = log_fn or print
    cfg.ensure_dirs()
    source = Path(source_dir)
    if not source.is_dir():
        raise FileNotFoundError(f"Source directory not found: {source}")

    raw_dir = cfg.media_dir
    all_extensions = PHOTO_EXTENSIONS | VIDEO_EXTENSIONS

    # Parse date filters
    date_from = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc) if from_date else None
    date_to = datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=timezone.utc, hour=23, minute=59, second=59) if to_date else None

    # Scan for media files (recursive), skip temp/converted files and dedup by base name
    seen_base_names: set[str] = set()
    files = []
    for f in sorted(source.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in all_extensions:
            continue
        # Skip pipeline temp files
        if f.name.startswith(("_converted_", "_hist_", "_audio_", "_resized_")):
            continue
        # Dedup: strip numeric ID prefix (e.g. "87681_IMG_001.jpg" → "IMG_001.jpg")
        parts = f.name.split("_", 1)
        base_name = parts[1] if len(parts) > 1 and parts[0].isdigit() else f.name
        if base_name in seen_base_names:
            continue
        seen_base_names.add(base_name)
        files.append(f)
    _log(f"Found {len(files)} unique media files in {source}")

    manifest = []
    for i, src_path in enumerate(files, 1):
        suffix = src_path.suffix.lower()
        is_video = suffix in VIDEO_EXTENSIONS
        item_type = 1 if is_video else 0

        # Extract date from EXIF or file mtime
        taken_dt = _extract_date(src_path)
        if taken_dt is None:
            taken_dt = datetime.fromtimestamp(src_path.stat().st_mtime, tz=timezone.utc)

        takentime = int(taken_dt.timestamp())
        taken_iso = taken_dt.isoformat()

        # Date filter
        if date_from and taken_dt < date_from:
            continue
        if date_to and taken_dt > date_to:
            continue

        # Generate a stable ID from the file path
        item_id = abs(hash(str(src_path))) % (10**8)

        # Link/copy file into media dir (if not already there)
        filename = src_path.name
        dest = raw_dir / f"{item_id}_{filename}"
        if not dest.exists():
            try:
                os.link(str(src_path), str(dest))  # hard link (no copy, instant)
            except (OSError, PermissionError):
                import shutil
                shutil.copy2(str(src_path), str(dest))  # fallback: copy

        entry = {
            "id": item_id,
            "filename": filename,
            "item_type": item_type,
            "takentime": takentime,
            "taken_iso": taken_iso,
            "filesize": src_path.stat().st_size,
            "local_path": str(dest),
            "metadata": {"persons": []},  # no face recognition without NAS
        }

        # Extract GPS location if available
        lat, lon = _extract_gps(src_path)
        if lat is not None and lon is not None:
            entry["latitude"] = lat
            entry["longitude"] = lon

        manifest.append(entry)
        if i % 100 == 0 or i == len(files):
            _log(f"[{i}/{len(files)}] Scanned ({len(manifest)} matched date filter)")

    manifest_path = cfg.workspace / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    _log(f"Manifest saved: {len(manifest)} items from {source}")
    return manifest


def _extract_date(path: Path) -> datetime | None:
    """Extract capture date from EXIF (photos) or FFmpeg metadata (videos)."""
    if path.suffix.lower() in VIDEO_EXTENSIONS:
        # Use ffprobe to get creation_time from video metadata
        try:
            from .media_utils import run_subprocess
            result = run_subprocess(
                ["ffprobe", "-v", "error", "-show_entries",
                 "format_tags=creation_time", "-of", "csv=p=0", str(path)],
                capture_output=True, text=True, timeout=10,
            )
            date_str = result.stdout.strip()
            if date_str:
                # Format: 2025-06-13T12:04:15.000000Z
                dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                return dt
        except Exception:
            pass
        return None

    try:
        from PIL import Image
        img = Image.open(path)
        exif = img._getexif()
        if exif:
            date_str = exif.get(36867) or exif.get(306)  # DateTimeOriginal or DateTime
            if date_str:
                dt = datetime.strptime(date_str, "%Y:%m:%d %H:%M:%S")
                return dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    return None


def _extract_gps(path: Path) -> tuple[float | None, float | None]:
    """Extract GPS coordinates from EXIF. Returns (lat, lon) or (None, None)."""
    if path.suffix.lower() in VIDEO_EXTENSIONS:
        return None, None

    try:
        from PIL import Image
        img = Image.open(path)
        exif = img._getexif()
        if not exif:
            return None, None

        # EXIF tag 34853 = GPSInfo
        gps_info = exif.get(34853)
        if not gps_info:
            return None, None

        def _to_degrees(value):
            d, m, s = value
            return float(d) + float(m) / 60 + float(s) / 3600

        lat = _to_degrees(gps_info[2])
        lon = _to_degrees(gps_info[4])
        if gps_info[1] == "S":
            lat = -lat
        if gps_info[3] == "W":
            lon = -lon
        return lat, lon
    except Exception:
        return None, None
