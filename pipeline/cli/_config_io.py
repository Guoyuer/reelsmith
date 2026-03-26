"""Save and load run configuration (CLI parameters) as JSON.

Config is stored in grouped format::

    {
      "source": {"type": "local", "path": "/photos"},
      "plan":   {"duration": 300, "model": "balanced", ...},
      "assemble": {"resolution": "4k60", "bitrate": 1.0}
    }
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

# Shared choice constants — used by Click options (_commands.py) and config validation.
SOURCE_CHOICES = ("local", "nas")
TRIP_TYPE_CHOICES = ("family", "solo", "food", "adventure", "architecture", "general")
STYLE_CHOICES = ("upbeat", "cinematic", "reflective", "energetic")
LANG_CHOICES = ("en", "cn", "both")

# Which flat CLI params belong to which config group.
_SOURCE_FIELDS = {
    "source",
    "path",
    "from_date",
    "to_date",
    "country",
    "district",
    "item_types",
}
_PLAN_FIELDS = {"duration", "model", "lang", "trip_type", "style", "focus", "music"}
_ASSEMBLE_FIELDS = {"resolution", "bitrate"}

_CONFIG_FILENAME = "run_config.json"

# ---------------------------------------------------------------------------
# Config schema for validation
# ---------------------------------------------------------------------------

_VALID_GROUPS = {"source", "plan", "assemble"}

_GROUP_SCHEMA: dict[str, dict[str, dict[str, Any]]] = {
    "source": {
        "type": {"type": str, "required": True, "choices": SOURCE_CHOICES},
        "path": {"type": str},
        "from_date": {"type": str},
        "to_date": {"type": str},
        "country": {"type": str},
        "district": {"type": str},
        "item_types": {"type": str},
    },
    "plan": {
        "duration": {"type": int, "required": True},
        "model": {"type": str, "required": True},
        "lang": {"type": str, "choices": LANG_CHOICES},
        "trip_type": {"type": str, "choices": TRIP_TYPE_CHOICES},
        "style": {"type": str, "choices": STYLE_CHOICES},
        "focus": {"type": str},
        "music": {"type": str},
    },
    "assemble": {
        "resolution": {"type": str, "required": True},
        "bitrate": {"type": (int, float)},
    },
}


def _validate_config(data: dict[str, Any], cfg_file: str) -> None:
    """Validate grouped config structure. Raises click.UsageError on problems."""
    if not isinstance(data, dict):
        raise click.UsageError(
            f"{cfg_file}: config must be a JSON object, got {type(data).__name__}"
        )

    errors: list[str] = []

    # Check for unknown top-level keys
    unknown_groups = set(data.keys()) - _VALID_GROUPS
    if unknown_groups:
        errors.append(f"unknown top-level keys: {', '.join(sorted(unknown_groups))}")

    for group_name, schema in _GROUP_SCHEMA.items():
        group = data.get(group_name)
        if group is None:
            continue

        if not isinstance(group, dict):
            errors.append(
                f"'{group_name}' must be an object, got {type(group).__name__}"
            )
            continue

        # Unknown keys within group
        unknown_keys = set(group.keys()) - set(schema.keys())
        if unknown_keys:
            errors.append(
                f"'{group_name}': unknown keys: {', '.join(sorted(unknown_keys))}"
            )

        # Per-field validation
        for field_name, rules in schema.items():
            value = group.get(field_name)

            if value is None:
                if rules.get("required"):
                    errors.append(f"'{group_name}.{field_name}' is required")
                continue

            # Type check
            expected = rules["type"]
            if not isinstance(value, expected):
                expected_name = (
                    expected.__name__
                    if isinstance(expected, type)
                    else " or ".join(t.__name__ for t in expected)
                )
                errors.append(
                    f"'{group_name}.{field_name}' must be {expected_name}, got {type(value).__name__}"
                )
                continue

            # Choices check
            if "choices" in rules and value not in rules["choices"]:
                errors.append(
                    f"'{group_name}.{field_name}' must be one of {list(rules['choices'])}, got '{value}'"
                )

    if errors:
        bullet_list = "\n  - ".join(errors)
        raise click.UsageError(f"{cfg_file}: invalid config:\n  - {bullet_list}")


def config_path_for(workspace: Path) -> Path:
    """Return the canonical run_config.json path for a workspace."""
    return workspace / _CONFIG_FILENAME


def save_run_config(workspace: Path, cli_params: dict[str, Any]) -> Path:
    """Write CLI parameters to run_config.json in grouped format.

    None values are omitted to keep the file clean and human-editable.
    Per-invocation flags (force, version, run_name) are excluded.
    """

    def _pick(fields: frozenset[str] | set[str]) -> dict[str, Any]:
        return {k: v for k, v in cli_params.items() if k in fields and v is not None}

    grouped: dict[str, Any] = {}
    source_cfg = _pick(_SOURCE_FIELDS)
    if source_cfg:
        if "source" in source_cfg:
            source_cfg["type"] = source_cfg.pop("source")
        grouped["source"] = source_cfg
    plan_cfg = _pick(_PLAN_FIELDS)
    if plan_cfg:
        grouped["plan"] = plan_cfg
    assemble_cfg = _pick(_ASSEMBLE_FIELDS)
    if assemble_cfg:
        grouped["assemble"] = assemble_cfg

    dest = config_path_for(workspace)
    dest.write_text(json.dumps(grouped, indent=2, ensure_ascii=False) + "\n")
    return dest


def load_run_config(cfg_file: str) -> dict[str, Any]:
    """Read and validate a grouped config JSON file.

    *cfg_file* is the path passed via ``--use-cfg-file``.
    When called from CLI commands, Click's ``type=click.Path(exists=True)``
    validates existence before this function runs.

    Raises ``click.UsageError`` on invalid JSON or schema violations.
    """
    text = Path(cfg_file).read_text()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise click.UsageError(f"{cfg_file}: invalid JSON: {e}") from None
    _validate_config(data, cfg_file)
    return data
