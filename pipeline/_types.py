"""Shared TypedDicts and Pydantic models for cross-stage data contracts.

These types define the data shapes flowing between pipeline stages
(prepare → plan → assemble). TypedDicts provide static type-checking;
Pydantic models add runtime validation at stage boundaries.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TypedDict

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("reelsmith.types")

# ---------------------------------------------------------------------------
# Canonical file extension sets — used by prepare, plan, and edl
# ---------------------------------------------------------------------------

PHOTO_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".avi", ".mkv", ".m4v"})


def cache_id(local_path: str) -> str:
    """Short deterministic hash from full path, for cache filenames."""
    return hashlib.md5(local_path.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Manifest entry — produced by scan, consumed by prepare
# ---------------------------------------------------------------------------


class _ManifestRequired(TypedDict):
    """Keys always present in manifest entries."""

    taken_at: str
    local_path: str


class ManifestEntry(_ManifestRequired, total=False):
    """One item in manifest.json (scan → prepare).

    Optional keys are present when EXIF GPS data is available.
    """

    # From fetch_local
    item_type: int  # 0=photo, 1=video
    takentime: int
    filesize: int

    # Location (from EXIF GPS)
    latitude: float
    longitude: float
    city: str
    country: str
    first_level: str  # region/state
    district: str


# ---------------------------------------------------------------------------
# Analysis entry — produced by prepare (load_analysis), consumed by plan
# ---------------------------------------------------------------------------


class ExifData(TypedDict, total=False):
    """EXIF metadata extracted from a photo."""

    focal_length: float
    aperture: float
    iso_speed: int


class _AnalysisRequired(TypedDict):
    """Keys always present in every analysis entry."""

    local_path: str
    media_type: str  # "photo" or "video"
    taken_at: str


class AnalysisEntry(_AnalysisRequired, total=False):
    """Per-item analysis data (prepare → plan).

    Built by load_analysis() from manifest + per-item cache files.
    The plan stage reads these via analysis_by_path: dict[str, AnalysisEntry].
    """

    # Location
    country: str | None
    first_level: str | None
    district: str | None

    # Photo-specific (from cache)
    thumbnail_path: str
    exif: ExifData

    # Video-specific (from cache)
    video_duration: float
    video_width: int
    video_height: int
    video_fps: float
    video_orientation: str  # "landscape" or "portrait"


# ---------------------------------------------------------------------------
# Pydantic runtime validation models — used at stage boundaries
# ---------------------------------------------------------------------------


class _AnalysisEntryValidator(BaseModel):
    """Internal Pydantic validator for analysis entries. Use AnalysisEntry TypedDict for type annotations."""

    model_config = ConfigDict(extra="ignore")

    # Required (from manifest)
    local_path: str
    media_type: str  # "photo" or "video"
    taken_at: str

    # Location (optional — not all items have GPS)
    country: str | None = None
    first_level: str | None = None
    district: str | None = None

    # Photo-specific (from cache, optional)
    thumbnail_path: str | None = None
    exif: dict | None = None

    # Video-specific (from cache, optional)
    video_duration: float | None = None
    video_width: int | None = None
    video_height: int | None = None
    video_fps: float | None = None
    video_orientation: str | None = None
