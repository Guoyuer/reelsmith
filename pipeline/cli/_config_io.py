"""Save and load run configuration (CLI parameters) as JSON."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

# Fields persisted to run_config.json.
# Excludes per-invocation flags: force, version.
SAVED_FIELDS = frozenset(
    {
        "source",
        "path",
        "from_date",
        "to_date",
        "duration",
        "model",
        "resolution",
        "lang",
        "trip_type",
        "style",
        "focus",
        "music",
        "bitrate",
        "country",
        "district",
        "item_types",
    }
)

_CONFIG_FILENAME = "run_config.json"


def config_path_for(workspace: Path) -> Path:
    """Return the canonical run_config.json path for a workspace."""
    return workspace / _CONFIG_FILENAME


def save_run_config(workspace: Path, cli_params: dict[str, Any]) -> Path:
    """Write CLI parameters to run_config.json (overwrites existing).

    Only fields in SAVED_FIELDS are persisted.  None values are omitted
    to keep the file clean and human-editable.
    """
    filtered = {k: v for k, v in cli_params.items() if k in SAVED_FIELDS and v is not None}
    dest = config_path_for(workspace)
    dest.write_text(json.dumps(filtered, indent=2, ensure_ascii=False) + "\n")
    return dest


def load_run_config(cfg_file: str) -> dict[str, Any]:
    """Read a config JSON file and return the stored parameters.

    *cfg_file* is the path passed via ``--use-cfg-file``.
    When called from CLI commands, Click's ``type=click.Path(exists=True)``
    validates existence before this function runs.
    """
    return json.loads(Path(cfg_file).read_text())
