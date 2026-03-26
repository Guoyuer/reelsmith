"""Shared TypedDicts and Pydantic models for cross-stage data contracts.

These types define the data shapes flowing between pipeline stages
(fetch → prepare → plan → assemble). TypedDicts provide static type-checking;
Pydantic models add runtime validation at stage boundaries.
"""

from __future__ import annotations

import logging
from typing import TypedDict

from pydantic import BaseModel, ConfigDict, ValidationError

logger = logging.getLogger("vlog.types")

# ---------------------------------------------------------------------------
# Canonical file extension sets — used by fetch, prepare, plan, and edl
# ---------------------------------------------------------------------------

PHOTO_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".avi", ".mkv", ".m4v"})

# ---------------------------------------------------------------------------
# Manifest entry — produced by fetch, consumed by prepare
# ---------------------------------------------------------------------------


class _ManifestRequired(TypedDict):
    """Keys always present in manifest entries."""

    id: int
    filename: str
    taken_iso: str
    local_path: str


class ManifestEntry(_ManifestRequired, total=False):
    """One item in manifest.json (fetch → prepare).

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
    iso: int


class _AnalysisRequired(TypedDict):
    """Keys always present in every analysis entry."""

    id: int
    filename: str
    local_path: str
    media_type: str  # "photo" or "video"
    taken_iso: str


class AnalysisEntry(_AnalysisRequired, total=False):
    """Per-item analysis data (prepare → plan).

    Built by load_analysis() from manifest + per-item cache files.
    The plan stage reads these via analysis_by_id: dict[str, AnalysisEntry].
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


class AnalysisEntryModel(BaseModel):
    """Runtime-validated version of AnalysisEntry.

    Used by load_analysis() to catch corrupted cache data at the
    prepare → plan boundary. Extra keys from cache files are tolerated.
    """

    model_config = ConfigDict(extra="ignore")

    # Required (from manifest)
    id: int
    filename: str
    local_path: str
    media_type: str  # "photo" or "video"
    taken_iso: str

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


def validate_analysis_entry(entry: dict) -> dict | None:
    """Validate an analysis entry dict at the prepare → plan boundary.

    Returns the validated dict (with extra keys stripped) on success,
    or None if validation fails (with a warning logged).
    """
    try:
        validated = AnalysisEntryModel.model_validate(entry)
        return validated.model_dump(exclude_none=False)
    except ValidationError as e:
        logger.warning(
            "Invalid analysis entry for item %s: %s",
            entry.get("id", "?"),
            e,
        )
        return None
