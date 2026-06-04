"""Click CLI group, options, presets, parsers, and command definitions."""

from __future__ import annotations

from pathlib import Path

import click

from ..assemble._encoder import CODEC_CHOICES
from ._config_io import (
    _ASSEMBLE_FIELDS,
    _PLAN_FIELDS,
    _SOURCE_FIELDS,
    LANG_CHOICES,
    STYLE_CHOICES,
    TRIP_TYPE_CHOICES,
    list_configs,
    load_run_config,
)
from ._runner import _run_pipeline

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _RequiredPrefixOption(click.Option):
    """Show [required] at the start of help text instead of the end.

    ``cfg_optional=True`` marks an option that is required on the command line
    but may instead come from ``--use-cfg-file``: left non-required at the Click
    level, enforced in ``_check_required``, still shown as ``[required]``.
    """

    def __init__(self, *args, cfg_optional: bool = False, **kwargs):
        self.cfg_optional = cfg_optional
        if cfg_optional:
            kwargs["required"] = False
        super().__init__(*args, **kwargs)

    def get_help_record(self, ctx):
        record = super().get_help_record(ctx)
        if record and (self.required or self.cfg_optional):
            name, help_text = record
            help_text = help_text.removesuffix("  [required]")
            help_text = f"[required] {help_text}"
            return name, help_text
        return record


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------


class _CliGroup(click.Group):
    """Custom group that hints at missing subcommand on unknown options."""

    def parse_args(self, ctx, args):
        try:
            return super().parse_args(ctx, args)
        except click.UsageError as e:
            if "No such option" in str(e) or "no such option" in str(e):
                hint = "Did you forget a command? Try: reelsmith full, prepare, plan, assemble, workspace"
                raise click.UsageError(f"{e}\n\nHint: {hint}") from None
            raise


@click.group(cls=_CliGroup)
def cli() -> None:
    """Automated highlight reel pipeline: prepare \u2192 plan \u2192 generate_music \u2192 assemble.

    \b
    Examples:
      reelsmith full -n trip -p ./photos -r 4k60 --duration 300 --model balanced
      reelsmith plan -n trip --duration 300 --model quality --force
      reelsmith assemble -n trip -r 1080p30
    """


# ---------------------------------------------------------------------------
# Shared Click options
# ---------------------------------------------------------------------------

_name_option = click.option(
    "-n",
    "--name",
    "run_name",
    required=True,
    cls=_RequiredPrefixOption,
    help="Run name (subdirectory under workspace/runs/)",
)

_force_option = click.option(
    "--force",
    is_flag=True,
    help="Re-generate all cached data (thumbnails, video previews, EDL)",
)

# ---------------------------------------------------------------------------
# Planning presets & resolver
# ---------------------------------------------------------------------------

_PLANNING_PRESETS = {
    "fast": ("gemini-3.1-flash-lite", "LOW"),
    "balanced": ("gemini-3.5-flash", "HIGH"),
    "quality": ("gemini-3.1-pro-preview", "HIGH"),
}

_plan_options = [
    click.option(
        "--duration",
        cfg_optional=True,
        cls=_RequiredPrefixOption,
        type=int,
        help="Target duration in seconds (60=1min, 180=3min, 300=5min)",
    ),
    click.option(
        "--trip-type",
        default="general",
        type=click.Choice(TRIP_TYPE_CHOICES),
        help="Narrative style (recommended to set). family=close-ups+laughter, solo=landscapes+wonder, food=dishes+markets, adventure=action+nature, architecture=buildings+compositions, general=balanced mix.",
    ),
    click.option(
        "--style",
        default="upbeat",
        type=click.Choice(STYLE_CHOICES),
        help="Pacing and mood: upbeat=lively, cinematic=dramatic, reflective=calm, energetic=fast-cut",
    ),
    click.option(
        "--focus",
        default="",
        help="Creative focus guiding Gemini's selection (e.g. 'family reunion joy; parents exploring Singapore')",
    ),
    click.option(
        "--instruct",
        default="",
        help="Free-form instructions for Gemini (e.g. 'no text overlays; prefer slow-motion shots')",
    ),
    click.option(
        "--lang",
        default="en",
        type=click.Choice(LANG_CHOICES),
        help="Text language: en=English, cn=Chinese, both=bilingual (title cards, text overlays, chapters)",
    ),
    click.option(
        "--model",
        cfg_optional=True,
        cls=_RequiredPrefixOption,
        help="Presets: fast / balanced / quality, or model:thinking.\n\n"
        "\b\n"
        "  fast      3.1-flash-lite  LOW   lowest cost\n"
        "  balanced  3.5-flash       HIGH  stable default\n"
        "  quality   3.1-pro         HIGH  highest quality\n"
        "  custom    gemini-2.5-flash:medium",
    ),
    click.option(
        "--music",
        default="auto",
        help="Music source: auto=AI-generated per segment (default), /path/to/file=custom track, none=no music",
    ),
]


def _resolve_planning(planning: str) -> tuple[str, str]:
    """Resolve planning preset or model:thinking into (model, thinking_level).

    Accepts: 'fast', 'balanced', 'quality', 'gemini-2.5-flash', 'gemini-2.5-flash:low'
    """
    if planning in _PLANNING_PRESETS:
        return _PLANNING_PRESETS[planning]
    if ":" in planning:
        model, thinking = planning.rsplit(":", 1)
        return model, thinking.upper()
    return planning, "HIGH"


def _music_file_from_param(music: str) -> str | None:
    return None if music == "none" else music


def _stages_with_music(stages: list[str], music: str) -> list[str]:
    result = list(stages)
    if music != "none":
        result.append("generate_music")
    return result


def _build_plan_config(p: dict, *, force: bool):
    from pipeline.plan import PlanConfig

    resolved_model, resolved_thinking = _resolve_planning(p["model"])
    return PlanConfig(
        style=p["style"],
        target_duration=p["duration"],
        focus=p["focus"],
        instruct=p["instruct"],
        trip_type=p["trip_type"],
        language=p["lang"],
        model=resolved_model,
        thinking_level=resolved_thinking,
        music_file=_music_file_from_param(p["music"]),
        force=force,
    )


# ---------------------------------------------------------------------------
# Resolution presets & parser
# ---------------------------------------------------------------------------

_RESOLUTION_PRESETS = {
    "4k60": (3840, 2160, 60),
    "4k30": (3840, 2160, 30),
    "2k60": (2560, 1440, 60),
    "2k30": (2560, 1440, 30),
    "1080p60": (1920, 1080, 60),
    "1080p30": (1920, 1080, 30),
    "720p30": (1280, 720, 30),
}


def _parse_resolution(ctx, param, value: str | None) -> tuple[int, int, int] | None:
    """Parse resolution preset (e.g. '4k60', '1080p30') or WxHxFPS (e.g. '1920x1080x30')."""
    if value is None:
        return None
    key = value.lower().replace(" ", "")
    if key in _RESOLUTION_PRESETS:
        return _RESOLUTION_PRESETS[key]
    # Try WxHxFPS format
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


_assemble_options = [
    click.option(
        "--resolution",
        "-r",
        cfg_optional=True,
        cls=_RequiredPrefixOption,
        callback=_parse_resolution,
        expose_value=True,
        is_eager=False,
        help="Preset or WxHxFPS.\n\n"
        "\b\n"
        "  4k60     3840x2160  60fps\n"
        "  4k30     3840x2160  30fps\n"
        "  2k60     2560x1440  60fps\n"
        "  2k30     2560x1440  30fps\n"
        "  1080p60  1920x1080  60fps\n"
        "  1080p30  1920x1080  30fps\n"
        "  720p30   1280x720   30fps\n"
        "  custom   1920x1080x30",
    ),
    click.option(
        "--bitrate",
        default=1.0,
        type=float,
        help="Bitrate quality multiplier (default: 1.0).\n\n"
        "\b\n"
        "  0.5  low     ~21 Mbps at 4K60\n"
        "  1.0  default ~43 Mbps at 4K60\n"
        "  1.5  high    ~65 Mbps at 4K60\n"
        "  2.0  max     ~87 Mbps at 4K60",
    ),
    click.option(
        "--codec",
        default="auto",
        type=click.Choice(list(CODEC_CHOICES)),
        help="Video codec.\n\n"
        "\b\n"
        "  auto  best available (HEVC preferred)\n"
        "  av1   AV1 (~30%% smaller than HEVC, needs RTX 40+)\n"
        "  hevc  HEVC/H.265 (default on GPU)\n"
        "  h264  H.264 (widest compatibility)",
    ),
]


def _apply_options(options):
    """Decorator to apply a list of click.option decorators."""

    def wrapper(fn):
        for opt in reversed(options):
            fn = opt(fn)
        return fn

    return wrapper


# ---------------------------------------------------------------------------
# --use-cfg-file flag & helpers
# ---------------------------------------------------------------------------

_use_cfg_option = click.option(
    "--use-cfg-file",
    default=None,
    type=click.Path(dir_okay=False),
    help="Load parameters from a YAML config file (mutually exclusive with other options except -n, --force, -v). "
    "Looks in workspace/runs/{name}/ if not found at the given path. "
    "Tip: a config is auto-saved on every run.",
)

# Meta params: never part of a saved config and always allowed alongside
# --use-cfg-file (run name, the flag itself, force, EDL version).
_META_PARAMS = frozenset({"run_name", "use_cfg_file", "force", "version"})


def _check_required(ctx: click.Context) -> None:
    """Enforce cfg_optional options when --use-cfg-file is absent."""
    for param in ctx.command.params:
        if param.name is None or not getattr(param, "cfg_optional", False):
            continue
        if ctx.params.get(param.name) is None:
            raise click.MissingParameter(ctx=ctx, param=param)


def _validate_use_cfg(ctx: click.Context) -> None:
    """Raise UsageError if --use-cfg-file is combined with explicitly-set params."""
    for param in ctx.command.params:
        if param.name in _META_PARAMS:
            continue
        if param.name is None:
            continue
        source = ctx.get_parameter_source(param.name)
        if source == click.core.ParameterSource.COMMANDLINE:
            raise click.UsageError(
                f"--use-cfg-file cannot be combined with --{param.name.replace('_', '-')}. "
                "Edit the run_config.json file instead."
            )


_RESOLUTION_PRESETS_REVERSE = {v: k for k, v in _RESOLUTION_PRESETS.items()}


def _format_resolution(resolution: tuple[int, int, int]) -> str:
    """Convert (w, h, fps) back to a preset name or WxHxFPS string."""
    if resolution in _RESOLUTION_PRESETS_REVERSE:
        return _RESOLUTION_PRESETS_REVERSE[resolution]
    w, h, fps = resolution
    return f"{w}x{h}x{fps}"


def _collect_defaults(ctx: click.Context) -> set[str]:
    """Param names whose values came from CLI defaults, not user input."""
    default = click.core.ParameterSource.DEFAULT
    return {
        p.name
        for p in ctx.command.params
        if p.name and ctx.get_parameter_source(p.name) == default
    }


def _resolve_params(ctx: click.Context) -> tuple[dict, dict, set[str]]:
    """Resolve params from --use-cfg-file (cfg wins) or the CLI (explicit/default).

    Returns *(params, cli_params, defaults)*.
    """
    p = dict(ctx.params)
    use_cfg_file = p.get("use_cfg_file")

    if use_cfg_file:
        _validate_use_cfg(ctx)
        cfg_path = _find_cfg_path(use_cfg_file, p.get("run_name", ""))
        saved = load_run_config(str(cfg_path))

        cmd_params = {pr.name for pr in ctx.command.params if pr.name}
        loaded, skipped = _apply_cfg_sections(p, saved, cmd_params)

        click.echo(
            f"Config: {cfg_path.name} — loaded [{', '.join(loaded)}]"
            + (f", skipped [{', '.join(skipped)}]" if skipped else "")
        )
    else:
        _check_required(ctx)

    cli_params = {
        k: _format_resolution(v) if k == "resolution" and isinstance(v, tuple) else v
        for k, v in p.items()
        if k not in _META_PARAMS
    }
    defaults = set() if use_cfg_file else _collect_defaults(ctx)
    return p, cli_params, defaults


def _find_cfg_path(use_cfg_file: str, run_name: str) -> Path:
    """Resolve --use-cfg-file to an existing path."""
    cfg_path = Path(use_cfg_file)
    if cfg_path.exists():
        return cfg_path
    run_dir = Path("workspace/runs") / run_name / use_cfg_file
    if run_dir.exists():
        return run_dir
    raise click.BadParameter(
        f"File not found: '{use_cfg_file}' (also checked {run_dir})",
        param_hint="'--use-cfg-file'",
    )


def _apply_cfg_sections(
    p: dict, saved: dict, cmd_params: set[str]
) -> tuple[list[str], list[str]]:
    """Apply only the config sections whose fields the command accepts."""
    _SECTION_FIELDS = {
        "source": _SOURCE_FIELDS,
        "plan": _PLAN_FIELDS,
        "assemble": _ASSEMBLE_FIELDS,
    }
    loaded: list[str] = []
    skipped: list[str] = []

    for section, fields in _SECTION_FIELDS.items():
        if section not in saved:
            continue
        if not (fields & cmd_params):
            skipped.append(section)
            continue
        cfg = dict(saved[section])
        if "resolution" in cfg:
            cfg["resolution"] = _parse_resolution(None, None, cfg["resolution"])
        p.update(cfg)
        loaded.append(section)

    return loaded, skipped


# ---------------------------------------------------------------------------
# Shared source option (--path for local folder)
# ---------------------------------------------------------------------------

_source_options = [
    click.option(
        "--path",
        "-p",
        cfg_optional=True,
        cls=_RequiredPrefixOption,
        type=click.Path(exists=True),
        help="Local folder path containing photos/videos",
    ),
]


# ---------------------------------------------------------------------------
# prepare: scan + media processing
# ---------------------------------------------------------------------------


@cli.command()
@click.pass_context
@_name_option
@_use_cfg_option
@_apply_options(_source_options)
@_force_option
def prepare(ctx, run_name, use_cfg_file, path, force):
    """Scan media folder and generate thumbnails + video previews (cached, use --force to regenerate)."""
    from pipeline.prepare import PrepareConfig

    p, cli_params, defaults = _resolve_params(ctx)
    _run_pipeline(
        run_name,
        source_dir=p["path"],
        prepare=PrepareConfig(force=force),
        stages=["prepare"],
        cli_params=cli_params,
        cli_defaults=defaults,
    )


# ---------------------------------------------------------------------------
# full: end-to-end pipeline
# ---------------------------------------------------------------------------


@cli.command()
@click.pass_context
@_name_option
@_use_cfg_option
@_apply_options(_source_options)
@_force_option
@_apply_options(_plan_options)
@_apply_options(_assemble_options)
def full(
    ctx,
    run_name,
    use_cfg_file,
    path,
    force,
    duration,
    trip_type,
    style,
    focus,
    instruct,
    lang,
    model,
    music,
    resolution,
    bitrate,
    codec,
):
    """Run the full pipeline end-to-end."""
    from pipeline.assemble import AssembleConfig
    from pipeline.prepare import PrepareConfig

    p, cli_params, defaults = _resolve_params(ctx)
    w, h, fps = p["resolution"]
    stages = _stages_with_music(["prepare", "plan"], p["music"])
    stages.append("assemble")

    _run_pipeline(
        run_name,
        source_dir=p["path"],
        prepare=PrepareConfig(force=force),
        plan=_build_plan_config(p, force=force),
        assemble=AssembleConfig(
            w=w, h=h, fps=fps, bitrate=p["bitrate"], codec=p["codec"]
        ),
        stages=stages,
        cli_params=cli_params,
        cli_defaults=defaults,
    )


@cli.command()
@click.pass_context
@_name_option
@_use_cfg_option
@_apply_options(_plan_options)
@_force_option
def plan(
    ctx,
    run_name,
    use_cfg_file,
    duration,
    trip_type,
    style,
    focus,
    instruct,
    lang,
    model,
    music,
    force,
):
    """Call Gemini to generate a new EDL (increments version). Requires prepare to have run first."""
    p, cli_params, defaults = _resolve_params(ctx)

    _run_pipeline(
        run_name,
        plan=_build_plan_config(p, force=force),
        stages=_stages_with_music(["plan"], p["music"]),
        cli_params=cli_params,
        cli_defaults=defaults,
    )


@cli.command()
@click.pass_context
@_name_option
@_use_cfg_option
@click.option(
    "-v",
    "--version",
    default=None,
    type=int,
    help="EDL version to render (default: latest)",
)
@_apply_options(_assemble_options)
def assemble(ctx, run_name, use_cfg_file, version, resolution, bitrate, codec):
    """Render video from EDL. Uses latest version unless -v specified."""
    from pipeline.assemble import AssembleConfig

    p, cli_params, defaults = _resolve_params(ctx)
    w, h, fps = p["resolution"]

    _run_pipeline(
        run_name,
        assemble=AssembleConfig(
            w=w, h=h, fps=fps, bitrate=p["bitrate"], codec=p["codec"], version=version
        ),
        stages=["assemble"],
        cli_params=cli_params,
        cli_defaults=defaults,
    )


# ---------------------------------------------------------------------------
# config: inspect saved run_config.json
# ---------------------------------------------------------------------------


@cli.command("config")
@_name_option
def show_config(run_name):
    """Print saved run_config.yaml for a run."""
    from pipeline.config import Config

    ws = Config.run_workspace(run_name=run_name)
    cfg = Config.load(ws)
    configs = list_configs(cfg.workspace)
    if not configs:
        raise click.UsageError(
            f"No config files in {cfg.workspace}.\n"
            "Run the pipeline with full parameters first."
        )
    # Print latest config, list all available
    if len(configs) > 1:
        click.echo(f"# {len(configs)} configs found (showing latest):")
        for c in configs:
            marker = " ← latest" if c == configs[-1] else ""
            click.echo(f"#   {c.name}{marker}")
    click.echo(f"# {configs[-1]}")
    click.echo(configs[-1].read_text())
