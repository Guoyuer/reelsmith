"""Load and validate YAML run configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click
import yaml

from ..assemble._encoder import CODEC_CHOICES

# Shared choice constants — used by Click options (_commands.py) and config validation.
TRIP_TYPE_CHOICES = ("family", "solo", "food", "adventure", "architecture", "general")
STYLE_CHOICES = ("upbeat", "cinematic", "reflective", "energetic")
LANG_CHOICES = ("en", "cn", "both")

# ---------------------------------------------------------------------------
# Config schema for validation
# ---------------------------------------------------------------------------

_VALID_GROUPS = {"pipeline", "source", "plan", "assemble"}

_GROUP_SCHEMA: dict[str, dict[str, dict[str, Any]]] = {
    "pipeline": {
        "stages": {"type": list, "required": True},
        "force": {"type": bool},
        "version": {"type": int, "nullable": True},
    },
    "source": {
        "path": {"type": str, "required": True},
    },
    "plan": {
        "duration": {"type": int, "required": True},
        "model": {"type": str, "required": True},
        "lang": {"type": str, "choices": LANG_CHOICES},
        "trip_type": {"type": str, "choices": TRIP_TYPE_CHOICES},
        "style": {"type": str, "choices": STYLE_CHOICES},
        "focus": {"type": str},
        "instruct": {"type": str},
        "music": {"type": str},
    },
    "assemble": {
        "resolution": {"type": str, "required": True},
        "bitrate": {"type": (int, float)},
        "codec": {"type": str, "choices": CODEC_CHOICES},
    },
}


def _expected_type_name(expected: type | tuple[type, ...]) -> str:
    if isinstance(expected, type):
        return expected.__name__
    return " or ".join(t.__name__ for t in expected)


def _validate_field_value(
    errors: list[str],
    *,
    group_name: str,
    field_name: str,
    value: Any,
    rules: dict[str, Any],
) -> None:
    expected = rules["type"]
    if not isinstance(value, expected):
        errors.append(
            f"'{group_name}.{field_name}' must be {_expected_type_name(expected)}, got {type(value).__name__}"
        )
        return

    if "choices" in rules and value not in rules["choices"]:
        errors.append(
            f"'{group_name}.{field_name}' must be one of {list(rules['choices'])}, got '{value}'"
        )

    if field_name == "stages":
        allowed = {"prepare", "plan", "generate_music", "assemble"}
        bad = [str(stage) for stage in value if stage not in allowed]
        if bad:
            errors.append(
                f"'{group_name}.{field_name}' contains unknown stages: {', '.join(bad)}"
            )


def _validate_group(
    errors: list[str],
    *,
    group_name: str,
    group: Any,
    schema: dict[str, dict[str, Any]],
) -> None:
    if not isinstance(group, dict):
        errors.append(f"'{group_name}' must be an object, got {type(group).__name__}")
        return

    unknown_keys = set(group.keys()) - set(schema.keys())
    if unknown_keys:
        errors.append(
            f"'{group_name}': unknown keys: {', '.join(sorted(unknown_keys))}"
        )

    for field_name, rules in schema.items():
        value = group.get(field_name)

        if value is None:
            if rules.get("nullable"):
                continue
            if rules.get("required"):
                errors.append(f"'{group_name}.{field_name}' is required")
            continue

        _validate_field_value(
            errors,
            group_name=group_name,
            field_name=field_name,
            value=value,
            rules=rules,
        )


def _validate_config(data: dict[str, Any], cfg_file: str) -> None:
    """Validate grouped config structure. Raises click.UsageError on problems."""
    if not isinstance(data, dict):
        raise click.UsageError(
            f"{cfg_file}: config must be a YAML/JSON object, got {type(data).__name__}"
        )

    errors: list[str] = []

    unknown_groups = set(data.keys()) - _VALID_GROUPS
    if unknown_groups:
        errors.append(f"unknown top-level keys: {', '.join(sorted(unknown_groups))}")

    for group_name, schema in _GROUP_SCHEMA.items():
        group = data.get(group_name)
        if group is None:
            continue

        _validate_group(errors, group_name=group_name, group=group, schema=schema)

    if errors:
        bullet_list = "\n  - ".join(errors)
        raise click.UsageError(f"{cfg_file}: invalid config:\n  - {bullet_list}")


def load_run_config(cfg_file: str) -> dict[str, Any]:
    """Read and validate a grouped config YAML (or JSON) file.

    *cfg_file* is the path passed via ``reelsmith run --config`` or the default
    ``workspace/runs/NAME/run.yaml``.

    Raises ``click.UsageError`` on invalid YAML/JSON or schema violations.
    """
    text = Path(cfg_file).read_text()
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise click.UsageError(f"{cfg_file}: invalid YAML: {e}") from None
    if data is None:
        raise click.UsageError(f"{cfg_file}: empty config file")
    _validate_config(data, cfg_file)
    return data
