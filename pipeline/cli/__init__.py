"""CLI package for the vlog pipeline.

Exports the ``cli`` Click group and key symbols used by tests.
"""

from ._commands import (
    ITEM_TYPE_NAMES,
    _apply_options,
    _build_fetch_config,
    _parse_item_types,
    _parse_resolution,
    _PLANNING_PRESETS,
    _RequiredPrefixOption,
    _RESOLUTION_PRESETS,
    _resolve_planning,
    cli,
)
from ._display import (
    STAGES,
    _build_headline_from_args,
    _ICON_DONE,
    _ICON_FAILED,
    _ICON_PENDING,
    _ICON_RUNNING,
    _PipelineDisplay,
    _progress_cb,
    _setup_logging,
)
from ._runner import (
    _check_interrupted,
    _handle_sigint,
    _interrupted,
    _PipelineContext,
    _run_assemble,
    _run_fetch,
    _run_generate_music,
    _run_pipeline,
    _run_plan,
    _run_prepare,
    _STAGE_RUNNERS,
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
