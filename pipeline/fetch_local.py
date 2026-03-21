"""Fetch media from a local folder — no NAS required.

Scans a directory for photos and videos, extracts metadata from EXIF
(date, GPS), and builds a manifest.json compatible with the rest of
the pipeline. Alternative to fetch.py (Synology NAS).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .config import Config

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v"}


def fetch_local(
    cfg: Config,
    *,
    source_dir: str,
    log_fn=None,
) -> list[dict]:
    """Scan a local folder for photos/videos and build a manifest.

    Uses all media files found — no date filtering.
    Extracts dates from EXIF / filename / file mtime for sorting.
    Points directly to source files (no copying or linking).
    """
    _log = log_fn or print
    cfg.ensure_dirs()
    source = Path(source_dir)
    if not source.is_dir():
        raise FileNotFoundError(f"Source directory not found: {source}")

    all_extensions = PHOTO_EXTENSIONS | VIDEO_EXTENSIONS

    # Scan for media files (recursive), skip pipeline temp files
    files = []
    for f in sorted(source.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in all_extensions:
            continue
        if f.name.startswith(("_converted_", "_hist_", "_audio_", "_resized_")):
            continue
        files.append(f)
    _log(f"Found {len(files)} media files in {source}")

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

        # Stable ID from filename (deterministic across runs, unlike hash())
        import hashlib
        item_id = int(hashlib.md5(src_path.name.encode()).hexdigest()[:8], 16) % (10**8)
        filename = src_path.name

        # Point directly to source file — no copying or linking
        entry = {
            "id": item_id,
            "filename": filename,
            "item_type": item_type,
            "takentime": takentime,
            "taken_iso": taken_iso,
            "filesize": src_path.stat().st_size,
            "local_path": str(src_path),
            "metadata": {"persons": []},  # no face recognition without NAS
        }

        # Extract GPS location if available
        lat, lon = _extract_gps(src_path)
        if lat is not None and lon is not None:
            entry["latitude"] = lat
            entry["longitude"] = lon

        manifest.append(entry)
        if i % 100 == 0 or i == len(files):
            _log(f"[{i}/{len(files)}] Scanned")

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
        # ffprobe failed — try filename
        return _parse_date_from_filename(path.stem)

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

    # PIL/FFmpeg failed — try parsing date from filename (common patterns)
    return _parse_date_from_filename(path.stem)


_DATE_PATTERNS = [
    # 87462_20250617_191756 (NAS ID prefix + date + time)
    re.compile(r"(?:^\d+_)?(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})"),
    # IMG20250613085912 or DJI_20250613120415_0072_D
    re.compile(r"(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})"),
    # 2025-06-13_12-04-15
    re.compile(r"(\d{4})-(\d{2})-(\d{2})[_T](\d{2})-(\d{2})-(\d{2})"),
]


def _parse_date_from_filename(stem: str) -> datetime | None:
    """Try to extract a datetime from common filename patterns."""
    for pat in _DATE_PATTERNS:
        m = pat.search(stem)
        if m:
            try:
                y, mo, d, h, mi, s = (int(x) for x in m.groups()[-6:])
                return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)
            except ValueError:
                continue
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
