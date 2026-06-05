"""Save and load run configuration as YAML with default annotations.

Config is stored in grouped format with ``# default`` comments::

    pipeline:
      stages: [prepare, plan, generate_music, assemble]
      force: false

    source:
      path: /photos

    plan:
      duration: 300
      model: balanced
      lang: cn
      trip_type: family  # default
      style: upbeat  # default
      music: auto  # default

    assemble:
      resolution: 4k60
      bitrate: 1.0  # default
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import click
import yaml

from ..assemble._encoder import CODEC_CHOICES

logger = logging.getLogger("reelsmith.plan")

# Shared choice constants — used by Click options (_commands.py) and config validation.
TRIP_TYPE_CHOICES = ("family", "solo", "food", "adventure", "architecture", "general")
STYLE_CHOICES = ("upbeat", "cinematic", "reflective", "energetic")
LANG_CHOICES = ("en", "cn", "both")

# Which flat CLI params belong to which config group.
_PIPELINE_FIELDS = {"stages", "force", "version"}
_SOURCE_FIELDS = {"path"}
_PLAN_FIELDS = {
    "duration",
    "model",
    "lang",
    "trip_type",
    "style",
    "focus",
    "instruct",
    "music",
}
_ASSEMBLE_FIELDS = {"resolution", "bitrate", "codec"}

_CONFIG_GLOB = "run_config_*.yaml"
_CONFIG_PREFIX = "run_config_"

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


def list_configs(workspace: Path) -> list[Path]:
    """Return all run_config_{timestamp}.yaml files, newest last."""
    return sorted(workspace.glob(_CONFIG_GLOB))


def _dump_yaml_with_comments(grouped: dict[str, Any], defaults: set[str]) -> str:
    """Render grouped config as YAML with ``# default`` comments on default values.

    *defaults* is a set of flat param names (e.g. ``{"trip_type", "style"}``).
    """
    lines: list[str] = []

    for group_name in ("pipeline", "source", "plan", "assemble"):
        group = grouped.get(group_name)
        if not group:
            continue
        if lines:
            lines.append("")  # blank line between groups
        lines.append(f"{group_name}:")
        for key, value in group.items():
            # Format value for YAML
            if isinstance(value, bool):
                yaml_val = "true" if value else "false"
            elif isinstance(value, str):
                # Quote strings that contain special chars or look like numbers
                if (
                    not value
                    or any(c in value for c in ":{}[]#,&*?|>!%@`'\"\\")
                    or value != value.strip()
                ):
                    yaml_val = (
                        yaml.dump(value, default_flow_style=True)
                        .removesuffix("\n...\n")
                        .strip()
                    )
                else:
                    yaml_val = value
            else:
                yaml_val = str(value)
            comment = "  # default" if key in defaults else ""
            lines.append(f"  {key}: {yaml_val}{comment}")
    lines.append("")  # trailing newline
    return "\n".join(lines)


def save_run_config(
    workspace: Path,
    cli_params: dict[str, Any],
    defaults: set[str] | None = None,
) -> Path:
    """Write CLI parameters to run_config.yaml in grouped format.

    None values are omitted to keep the file clean and human-editable.
    Per-invocation flags (force, version, run_name) are excluded.
    *defaults* is the set of param names that came from CLI defaults (not user input).
    """

    def _pick(fields: frozenset[str] | set[str]) -> dict[str, Any]:
        return {k: v for k, v in cli_params.items() if k in fields and v is not None}

    grouped: dict[str, Any] = {}
    pipeline_cfg = _pick(_PIPELINE_FIELDS)
    if pipeline_cfg:
        grouped["pipeline"] = pipeline_cfg
    source_cfg = _pick(_SOURCE_FIELDS)
    if source_cfg:
        grouped["source"] = source_cfg
    plan_cfg = _pick(_PLAN_FIELDS)
    if plan_cfg:
        grouped["plan"] = plan_cfg
    assemble_cfg = _pick(_ASSEMBLE_FIELDS)
    if assemble_cfg:
        grouped["assemble"] = assemble_cfg

    from datetime import datetime

    yaml_text = _dump_yaml_with_comments(grouped, defaults or set())

    # Write timestamped file (never overwritten)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = workspace / f"run_config_{timestamp}.yaml"
    dest.write_text(yaml_text)

    # Log to file
    logger.info("Run config saved: %s", dest.name)
    for line in dest.read_text().splitlines():
        if line.strip():
            logger.info("  %s", line)

    # Rich display to terminal
    from pipeline.utils import stderr_console

    console = stderr_console()
    if console:
        from rich.panel import Panel
        from rich.syntax import Syntax

        console.print(
            Panel(
                Syntax(dest.read_text(), "yaml", theme="ansi_dark"),
                title=f"[bold]Run Config[/bold] — {dest.name}",
                border_style="dim",
                expand=False,
            )
        )

    return dest


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
