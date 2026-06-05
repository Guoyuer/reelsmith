"""Click CLI group and YAML-first run commands."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import click

from ._config_io import (
    list_configs,
    load_run_config,
)
from ._runner import _run_pipeline


class _CliGroup(click.Group):
    """Custom group with a concise hint for top-level option mistakes."""

    def parse_args(self, ctx, args):
        try:
            return super().parse_args(ctx, args)
        except click.UsageError as e:
            if "No such option" in str(e) or "no such option" in str(e):
                raise click.UsageError(
                    f"{e}\n\nHint: run a trip with: reelsmith run NAME"
                ) from None
            raise


@click.group(cls=_CliGroup)
def cli() -> None:
    """Automated highlight reel pipeline driven by YAML config files.

    \b
    Examples:
      reelsmith new atlanta "D:\\Atlanta Trip"
      reelsmith run atlanta
      reelsmith edit atlanta
    """


_PLANNING_PRESETS = {
    "fast": ("gemini-3.1-flash-lite", "LOW"),
    "balanced": ("gemini-3.5-flash", "HIGH"),
    "quality": ("gemini-3.1-pro-preview", "HIGH"),
}

_RESOLUTION_PRESETS = {
    "4k60": (3840, 2160, 60),
    "4k30": (3840, 2160, 30),
    "2k60": (2560, 1440, 60),
    "2k30": (2560, 1440, 30),
    "1080p60": (1920, 1080, 60),
    "1080p30": (1920, 1080, 30),
    "720p30": (1280, 720, 30),
}


def _resolve_planning(planning: str) -> tuple[str, str]:
    """Resolve planning preset or model:thinking into (model, thinking_level)."""
    if planning in _PLANNING_PRESETS:
        return _PLANNING_PRESETS[planning]
    if ":" in planning:
        model, thinking = planning.rsplit(":", 1)
        return model, thinking.upper()
    return planning, "HIGH"


def _music_file_from_param(music: str) -> str | None:
    return None if music == "none" else music


def _parse_resolution(ctx, param, value: str | None) -> tuple[int, int, int] | None:
    """Parse resolution preset or WxHxFPS."""
    if value is None:
        return None
    key = value.lower().replace(" ", "")
    if key in _RESOLUTION_PRESETS:
        return _RESOLUTION_PRESETS[key]
    parts = key.split("x")
    if len(parts) == 3:
        try:
            w, h, fps = int(parts[0]), int(parts[1]), int(parts[2])
            return (w, h, fps)
        except ValueError:
            pass
    presets = ", ".join(_RESOLUTION_PRESETS)
    raise click.BadParameter(
        f"Unknown resolution '{value}'. Use a preset ({presets}) or WxHxFPS (e.g. 1920x1080x30)"
    )


_RESOLUTION_PRESETS_REVERSE = {v: k for k, v in _RESOLUTION_PRESETS.items()}


def _format_resolution(resolution: tuple[int, int, int]) -> str:
    """Convert (w, h, fps) back to a preset name or WxHxFPS string."""
    if resolution in _RESOLUTION_PRESETS_REVERSE:
        return _RESOLUTION_PRESETS_REVERSE[resolution]
    w, h, fps = resolution
    return f"{w}x{h}x{fps}"


def _run_workspace(run_name: str) -> Path:
    from pipeline.config import Config

    return Path(Config.run_workspace(run_name=run_name))


def _default_cfg_path(run_name: str) -> Path:
    return _run_workspace(run_name) / "run.yaml"


def _find_cfg_path(cfg_file: str | None, run_name: str) -> Path:
    """Resolve a config path, also checking workspace/runs/{name}/."""
    if not cfg_file:
        cfg_path = _default_cfg_path(run_name)
        if cfg_path.exists():
            return cfg_path
        raise click.BadParameter(
            f"Default config not found: {cfg_path}. Create it with: reelsmith new {run_name} PATH",
            param_hint="'--config'",
        )
    cfg_path = Path(cfg_file)
    if cfg_path.exists():
        return cfg_path
    run_dir = Path("workspace/runs") / run_name / cfg_file
    if run_dir.exists():
        return run_dir
    raise click.BadParameter(
        f"File not found: '{cfg_file}' (also checked {run_dir})",
        param_hint="'--config'",
    )


def _stage_list(data: dict[str, Any]) -> list[str]:
    stages = data.get("pipeline", {}).get("stages")
    if not stages:
        raise click.UsageError("Config must set pipeline.stages")
    return list(stages)


_ALLOWED_STAGES = {"prepare", "plan", "generate_music", "assemble"}


def _parse_stages(value: str | None) -> list[str] | None:
    if value is None:
        return None
    stages = [stage.strip() for stage in value.split(",") if stage.strip()]
    if not stages:
        raise click.BadParameter("stages cannot be empty", param_hint="'--stages'")
    bad = [stage for stage in stages if stage not in _ALLOWED_STAGES]
    if bad:
        raise click.BadParameter(
            f"Unknown stages: {', '.join(bad)}", param_hint="'--stages'"
        )
    return stages


def _apply_pipeline_overrides(
    data: dict[str, Any],
    *,
    stages_override: str | None = None,
    version_override: int | None = None,
) -> dict[str, Any]:
    result = copy.deepcopy(data)
    pipeline = result.setdefault("pipeline", {})
    stages = _parse_stages(stages_override)
    if stages is not None:
        pipeline["stages"] = stages
    if version_override is not None:
        pipeline["version"] = version_override
    return result


def _build_prepare_config(data: dict[str, Any], stages: list[str]):
    if "prepare" not in stages:
        return None
    from pipeline.prepare import PrepareConfig

    force = bool(data.get("pipeline", {}).get("force", False))
    return PrepareConfig(force=force)


def _build_plan_config(data: dict[str, Any], stages: list[str]):
    if "plan" not in stages:
        return None
    from pipeline.plan import PlanConfig

    plan = data.get("plan") or {}
    pipeline = data.get("pipeline") or {}
    model, thinking = _resolve_planning(plan["model"])
    return PlanConfig(
        style=plan.get("style", "upbeat"),
        target_duration=plan["duration"],
        focus=plan.get("focus", ""),
        instruct=plan.get("instruct", ""),
        trip_type=plan.get("trip_type", "general"),
        language=plan.get("lang", "en"),
        model=model,
        thinking_level=thinking,
        music_file=_music_file_from_param(plan.get("music", "auto")),
        force=bool(pipeline.get("force", False)),
    )


def _build_assemble_config(data: dict[str, Any], stages: list[str]):
    if "assemble" not in stages:
        return None
    from pipeline.assemble import AssembleConfig

    assemble = data.get("assemble") or {}
    pipeline = data.get("pipeline") or {}
    resolution = _parse_resolution(None, None, assemble["resolution"])
    assert resolution is not None
    w, h, fps = resolution
    return AssembleConfig(
        w=w,
        h=h,
        fps=fps,
        bitrate=float(assemble.get("bitrate", 1.0)),
        codec=assemble.get("codec", "auto"),
        version=pipeline.get("version"),
    )


def _source_dir(data: dict[str, Any], stages: list[str]) -> str | None:
    if "prepare" not in stages:
        return None
    return str((data.get("source") or {})["path"])


def _flatten_run_config(data: dict[str, Any]) -> dict[str, Any]:
    """Flatten grouped YAML to the format persisted by save_run_config."""
    flat: dict[str, Any] = {}
    for group in ("pipeline", "source", "plan", "assemble"):
        value = data.get(group)
        if isinstance(value, dict):
            flat.update(value)
    return flat


def _validate_stage_requirements(data: dict[str, Any], stages: list[str]) -> None:
    missing: list[str] = []
    if "prepare" in stages and "source" not in data:
        missing.append("source")
    if "plan" in stages and "plan" not in data:
        missing.append("plan")
    if "assemble" in stages and "assemble" not in data:
        missing.append("assemble")
    if missing:
        raise click.UsageError(
            "Config is missing sections required by pipeline.stages: "
            + ", ".join(missing)
        )


def _run_from_config(
    *,
    run_name: str,
    cfg_path: Path,
    stages_override: str | None = None,
    version_override: int | None = None,
) -> None:
    data = _apply_pipeline_overrides(
        load_run_config(str(cfg_path)),
        stages_override=stages_override,
        version_override=version_override,
    )
    stages = _stage_list(data)
    _validate_stage_requirements(data, stages)
    prepare = _build_prepare_config(data, stages)
    plan = _build_plan_config(data, stages)
    assemble = _build_assemble_config(data, stages)
    source_dir = _source_dir(data, stages)

    click.echo(f"Config: {cfg_path} - stages [{', '.join(stages)}]")
    _run_pipeline(
        run_name,
        source_dir=source_dir,
        prepare=prepare,
        plan=plan,
        assemble=assemble,
        stages=stages,
        cli_params=_flatten_run_config(data),
        cli_defaults=set(),
    )


@cli.command("run")
@click.argument("run_name")
@click.option(
    "-c",
    "--config",
    "cfg_file",
    type=click.Path(dir_okay=False),
    help="YAML config file. Defaults to workspace/runs/NAME/run.yaml.",
)
@click.option(
    "--stages",
    help="Temporarily override pipeline.stages, e.g. plan,generate_music,assemble.",
)
@click.option(
    "--version",
    type=int,
    help="Temporarily override pipeline.version for assemble-only rerenders.",
)
def run(
    run_name: str,
    cfg_file: str | None,
    stages: str | None,
    version: int | None,
) -> None:
    """Run a trip from its YAML config."""
    cfg_path = _find_cfg_path(cfg_file, run_name)
    _run_from_config(
        run_name=run_name,
        cfg_path=cfg_path,
        stages_override=stages,
        version_override=version,
    )


def _yaml_scalar(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _default_config(
    *,
    path: str,
    duration: int,
    model: str,
    resolution: str,
) -> str:
    return f"""\
pipeline:
  stages: [prepare, plan, generate_music, assemble]
  force: false

source:
  path: {_yaml_scalar(path)}

plan:
  duration: {duration}
  model: {_yaml_scalar(model)}
  lang: cn
  trip_type: general
  style: upbeat
  focus: ''
  instruct: ''
  music: auto

assemble:
  resolution: {_yaml_scalar(resolution)}
  bitrate: 1.0
  codec: auto
"""


@cli.command("new")
@click.argument("run_name")
@click.argument("path", type=click.Path(exists=True, file_okay=False))
@click.option("--force", is_flag=True, help="Overwrite an existing config file.")
@click.option(
    "--duration",
    default=180,
    show_default=True,
    type=click.IntRange(min=1),
    help="Initial target seconds.",
)
@click.option(
    "--model", default="fast", show_default=True, help="Initial plan model preset."
)
@click.option(
    "--resolution",
    default="1080p30",
    show_default=True,
    help="Initial render resolution.",
)
def new(
    run_name: str,
    path: str,
    force: bool,
    duration: int,
    model: str,
    resolution: str,
) -> None:
    """Create a trip workspace with run.yaml."""
    _parse_resolution(None, None, resolution)
    workspace = _run_workspace(run_name)
    workspace.mkdir(parents=True, exist_ok=True)
    dest = workspace / "run.yaml"
    if dest.exists() and not force:
        raise click.UsageError(f"{dest} already exists; pass --force to overwrite")
    config = _default_config(
        path=path,
        duration=duration,
        model=model,
        resolution=resolution,
    )
    dest.write_text(config, encoding="utf-8")
    click.echo(f"Created {dest}")
    click.echo(f"Next: reelsmith run {run_name}")


@cli.command("edit")
@click.argument("run_name")
def edit(run_name: str) -> None:
    """Open a trip's run.yaml in the default editor."""
    cfg_path = _find_cfg_path(None, run_name)
    click.edit(filename=str(cfg_path.resolve()))


@cli.command("config")
@click.argument("run_name")
def show_config(run_name: str) -> None:
    """Print current run.yaml and latest saved snapshot."""
    workspace = _run_workspace(run_name)
    current = workspace / "run.yaml"
    if current.exists():
        click.echo(f"# {current}")
        click.echo(current.read_text())
    configs = list_configs(workspace)
    if not configs:
        if current.exists():
            return
        raise click.UsageError(f"No config files in {workspace}")
    if len(configs) > 1:
        click.echo(f"# {len(configs)} snapshots found")
    click.echo(f"# latest snapshot: {configs[-1]}")
    click.echo(configs[-1].read_text())
