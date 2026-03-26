"""Shared TypedDicts for cross-stage data contracts.

These types define the implicit data shapes flowing between pipeline stages
(fetch → prepare → plan → assemble). Keeping them in one place makes the
contracts explicit and catches key typos / missing fields at type-check time.
"""

from __future__ import annotations

from typing import TypedDict

# ---------------------------------------------------------------------------
# Manifest entry — produced by fetch, consumed by prepare
# ---------------------------------------------------------------------------


class ManifestMetadata(TypedDict, total=False):
    """Metadata sub-dict (persons detected in media)."""

    persons: list[str]


class _ManifestRequired(TypedDict):
    """Keys always present in manifest entries."""

    id: int
    filename: str
    taken_iso: str
    local_path: str
    metadata: ManifestMetadata


class ManifestEntry(_ManifestRequired, total=False):
    """One item in manifest.json (fetch → prepare).

    Optional keys are present when EXIF GPS data is available,
    or added by the prepare stage (family detection).
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

    # Added by prepare (family detection pass)
    family_count: int
    family_names: list[str]


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

    # From manifest
    family_count: int
    persons: list[str]

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
# Preprocessed data — produced by prepare, consumed by plan
# ---------------------------------------------------------------------------


class PreprocessedData(TypedDict):
    """Output of prepare(), saved to preprocessed.json."""

    family_names: list[str]
