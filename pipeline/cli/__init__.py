"""CLI package for the vlog pipeline.

Exports the ``cli`` Click group and key symbols used by tests.
"""

# Register the workspace command (side-effect import)
from . import _workspace  # noqa: F401
from ._commands import (
    _PLANNING_PRESETS,
    _RESOLUTION_PRESETS,
    _format_resolution,
    _parse_resolution,
    _resolve_planning,
    cli,
)
from ._config_io import (
    LANG_CHOICES,
    STYLE_CHOICES,
    TRIP_TYPE_CHOICES,
    list_configs,
    load_run_config,
    save_run_config,
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

__all__ = [
    "cli",
    "STAGES",
    "LANG_CHOICES",
    "STYLE_CHOICES",
    "TRIP_TYPE_CHOICES",
    "_PLANNING_PRESETS",
    "_RESOLUTION_PRESETS",
    "_format_resolution",
    "_resolve_planning",
    "_parse_resolution",
    "_run_pipeline",
    "_PipelineDisplay",
    "_PipelineContext",
    "_run_prepare",
    "list_configs",
    "load_run_config",
    "save_run_config",
]
