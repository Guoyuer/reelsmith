"""CLI package for the vlog pipeline.

Exports the ``cli`` Click group and key symbols used by tests.
"""

from ._commands import (
    ITEM_TYPE_NAMES,
    _parse_resolution,
    _PLANNING_PRESETS,
    _RESOLUTION_PRESETS,
    _resolve_planning,
    cli,
)
from ._display import (
    STAGES,
    _PipelineDisplay,
)
from ._runner import (
    _PipelineContext,
    _run_pipeline,
    _run_prepare,
)

# Register the workspace command (side-effect import)
from . import _workspace  # noqa: F401

__all__ = [
    "cli",
    "STAGES",
    "ITEM_TYPE_NAMES",
    "_PLANNING_PRESETS",
    "_RESOLUTION_PRESETS",
    "_resolve_planning",
    "_parse_resolution",
    "_run_pipeline",
    "_PipelineDisplay",
    "_PipelineContext",
    "_run_prepare",
]
