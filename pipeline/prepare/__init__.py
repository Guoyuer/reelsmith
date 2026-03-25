"""Stage 2: Prepare media for visual planning."""

from ._prepare import PrepareConfig, load_analysis, prepare

# Re-export private helpers used by tests
from ._prepare import (  # noqa: F401
    _detect_family,
    _generate_video_previews,
    _has_dense_keyframes,
    _prepare_photo,
    _prepare_video,
    _read_exif,
)

__all__ = ["prepare", "load_analysis", "PrepareConfig"]
