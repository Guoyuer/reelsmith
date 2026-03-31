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
    """Show [required] at the start of help text instead of the end."""

    def get_help_record(self, ctx):
        record = super().get_help_record(ctx)
        if record and self.required:
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


class _CfgAwareCommand(click.Command):
    """Command that skips 'required' checks when --use-cfg-file is present."""

    def parse_args(self, ctx, args):
        # Peek: if --use-cfg-file is in args, temporarily disable required checks
        # on all params except run_name (which is always required).
        restored = []
        if "--use-cfg-file" in args:
            for param in self.params:
                if param.name != "run_name" and getattr(param, "required", False):
                    param.required = False
                    restored.append(param)
        try:
            return super().parse_args(ctx, args)
        finally:
            for param in restored:
                param.required = True


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
    "fast": ("gemini-3.1-flash-lite-preview", "LOW"),
    "balanced": ("gemini-3-flash-preview", "HIGH"),
    "quality": ("gemini-3.1-pro-preview", "HIGH"),
}

_plan_options = [
    click.option(
        "--duration",
        required=True,
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
        required=True,
        cls=_RequiredPrefixOption,
        help="Presets: fast / balanced / quality, or model:thinking.\n\n"
        "\b\n"
        "  fast      3.1-flash-lite  LOW   ~$0.24/run\n"
        "  balanced  3-flash         HIGH  ~$0.48/run\n"
        "  quality   3.1-pro         HIGH  ~$1.92/run\n"
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
        required=True,
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

# Params that are always allowed alongside --use-cfg-file
_CFG_ALLOWED_PARAMS = frozenset({"run_name", "use_cfg_file", "force", "version"})


def _validate_use_cfg(ctx: click.Context) -> None:
    """Raise UsageError if --use-cfg-file is combined with explicitly-set params."""
    for param in ctx.command.params:
        if param.name in _CFG_ALLOWED_PARAMS:
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
    """Return set of param names whose values came from CLI defaults, not user input."""
    defaults = set()
    for param in ctx.command.params:
        if param.name is None:
            continue
        source = ctx.get_parameter_source(param.name)
        if source == click.core.ParameterSource.DEFAULT:
            defaults.add(param.name)
    return defaults


def _build_cli_params(**kwargs) -> dict:
    """Build a cli_params dict, converting resolution tuple to string."""
    params = {}
    for k, v in kwargs.items():
        if k == "resolution" and isinstance(v, tuple):
            params[k] = _format_resolution(v)
        else:
            params[k] = v
    return params


_CFG_SKIP_PARAMS = frozenset({"run_name", "use_cfg_file", "force", "version"})


def _resolve_params(ctx: click.Context) -> tuple[dict, dict | None, set[str]]:
    """Handle --use-cfg-file overrides, build cli_params and defaults.

    Resolution order for each parameter:

    1. If ``--use-cfg-file`` is set: cfg-file value wins (CLI explicit
       params other than -n/--force raise an error via ``_validate_use_cfg``).
       Only config sections relevant to the current command are loaded;
       other sections are skipped with a log message. The config file is
       never overwritten.
    2. Otherwise: CLI value (explicit or default, tracked via
       ``_collect_defaults``).

    Returns *(params, cli_params, defaults)* where *params* is a dict of all
    resolved parameter values (cfg-file overrides applied).
    *cli_params* is ``None`` when loading from a config file (skip save).
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
        return p, None, set()

    cli_params = _build_cli_params(
        **{k: v for k, v in p.items() if k not in _CFG_SKIP_PARAMS}
    )
    return p, cli_params, _collect_defaults(ctx)


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
        required=True,
        cls=_RequiredPrefixOption,
        type=click.Path(exists=True),
        help="Local folder path containing photos/videos",
    ),
]


# ---------------------------------------------------------------------------
# prepare: scan + media processing
# ---------------------------------------------------------------------------


@cli.command(cls=_CfgAwareCommand)
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


@cli.command(cls=_CfgAwareCommand)
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
    from pipeline.plan import PlanConfig
    from pipeline.prepare import PrepareConfig

    p, cli_params, defaults = _resolve_params(ctx)
    resolved_model, resolved_thinking = _resolve_planning(p["model"])
    w, h, fps = p["resolution"]
    music_val = p["music"]
    stages = ["prepare", "plan"]
    music_file = None if music_val == "none" else music_val
    if music_val != "none":
        stages.append("generate_music")
    stages.append("assemble")

    _run_pipeline(
        run_name,
        source_dir=p["path"],
        prepare=PrepareConfig(force=force),
        plan=PlanConfig(
            style=p["style"],
            target_duration=p["duration"],
            focus=p["focus"],
            instruct=p["instruct"],
            trip_type=p["trip_type"],
            language=p["lang"],
            model=resolved_model,
            thinking_level=resolved_thinking,
            music_file=music_file,
            force=force,
        ),
        assemble=AssembleConfig(
            w=w, h=h, fps=fps, bitrate=p["bitrate"], codec=p["codec"]
        ),
        stages=stages,
        cli_params=cli_params,
        cli_defaults=defaults,
    )


@cli.command(cls=_CfgAwareCommand)
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
    from pipeline.plan import PlanConfig

    p, cli_params, defaults = _resolve_params(ctx)
    resolved_model, resolved_thinking = _resolve_planning(p["model"])
    music_val = p["music"]
    music_file = None if music_val == "none" else music_val
    stages = ["plan"]
    if music_val != "none":
        stages.append("generate_music")

    _run_pipeline(
        run_name,
        plan=PlanConfig(
            style=p["style"],
            target_duration=p["duration"],
            focus=p["focus"],
            instruct=p["instruct"],
            trip_type=p["trip_type"],
            language=p["lang"],
            model=resolved_model,
            thinking_level=resolved_thinking,
            music_file=music_file,
            force=force,
        ),
        stages=stages,
        cli_params=cli_params,
        cli_defaults=defaults,
    )


@cli.command(cls=_CfgAwareCommand)
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
