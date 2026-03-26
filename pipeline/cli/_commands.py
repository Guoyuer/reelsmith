"""Click CLI group, options, presets, parsers, and command definitions."""

from __future__ import annotations

import click

from ._config_io import (
    LANG_CHOICES,
    SOURCE_CHOICES,
    STYLE_CHOICES,
    TRIP_TYPE_CHOICES,
    config_path_for,
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


ITEM_TYPE_NAMES = {"photo": 0, "video": 1, "live": 3, "motion": 6}


def _parse_item_types(value: str) -> list[int]:
    result = []
    for part in value.split(","):
        part = part.strip().lower()
        if part in ITEM_TYPE_NAMES:
            result.append(ITEM_TYPE_NAMES[part])
        else:
            result.append(int(part))
    return result


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
                hint = "Did you forget a command? Use: full, prepare, plan, assemble, workspace"
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
    """Automated vlog pipeline: fetch \u2192 prepare \u2192 plan \u2192 generate_music \u2192 assemble.

    \b
    Examples:
      vlog full -n trip -s local -p ./photos -r 4k60 --duration 300 --model balanced
      vlog plan -n trip --duration 300 --model quality --force
      vlog assemble -n trip -r 1080p30
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
        default="family",
        type=click.Choice(TRIP_TYPE_CHOICES),
        help="Narrative style: family=close-ups+laughter, solo=landscapes+wonder, food=dishes+markets, etc.",
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
        "quality",
        default=1.0,
        type=float,
        help="Bitrate quality multiplier (default: 1.0).\n\n"
        "\b\n"
        "  0.5  low     ~21 Mbps at 4K60\n"
        "  1.0  default ~43 Mbps at 4K60\n"
        "  1.5  high    ~65 Mbps at 4K60\n"
        "  2.0  max     ~87 Mbps at 4K60",
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
    type=click.Path(exists=True, dir_okay=False),
    help="Load parameters from a JSON config file (mutually exclusive with other options except -n, --force, -v). "
    "Tip: a config is auto-saved to workspace/runs/{name}/run_config.json on every run.",
)

# Params that are always allowed alongside --use-cfg-file
_CFG_ALLOWED_PARAMS = frozenset({"run_name", "use_cfg_file", "force", "version"})


def _validate_use_cfg(ctx: click.Context) -> None:
    """Raise UsageError if --use-cfg-file is combined with explicitly-set params."""
    for param in ctx.command.params:
        if param.name in _CFG_ALLOWED_PARAMS:
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


def _load_source_fields(saved: dict) -> tuple:
    """Extract source-related fields from grouped config. Returns 7-tuple."""
    src = saved["source"]
    return (
        src["type"],
        src.get("path"),
        src.get("from_date"),
        src.get("to_date"),
        src.get("country"),
        src.get("district"),
        src.get("item_types"),
    )


def _load_plan_fields(saved: dict) -> tuple:
    """Extract plan-related fields from grouped config. Returns 7-tuple."""
    p = saved["plan"]
    return (
        p["duration"],
        p["model"],
        p.get("lang", "en"),
        p.get("trip_type", "family"),
        p.get("style", "upbeat"),
        p.get("focus", ""),
        p.get("music", "auto"),
    )


def _load_assemble_fields(saved: dict) -> tuple:
    """Extract assemble-related fields from grouped config. Returns (resolution, quality)."""
    a = saved["assemble"]
    return (
        _parse_resolution(None, None, a["resolution"]),
        a.get("bitrate", 1.0),
    )


# ---------------------------------------------------------------------------
# Shared source options (--source local|nas, --path, NAS filters)
# ---------------------------------------------------------------------------

_source_options = [
    click.option(
        "--source",
        "-s",
        required=True,
        cls=_RequiredPrefixOption,
        type=click.Choice(SOURCE_CHOICES),
        help="Media source: local folder or Synology NAS",
    ),
    click.option(
        "--path",
        "-p",
        default=None,
        type=click.Path(exists=True),
        help="Local folder path (required when --source local)",
    ),
    click.option(
        "-f",
        "--from-date",
        default=None,
        help="NAS start date YYYY-MM-DD (required when --source nas)",
    ),
    click.option(
        "-t",
        "--to-date",
        default=None,
        help="NAS end date YYYY-MM-DD (required when --source nas)",
    ),
    click.option("--country", default=None, help="NAS filter: country"),
    click.option("--district", default=None, help="NAS filter: district/city"),
    click.option(
        "--item-types", default=None, help="NAS filter: photo,video,live,motion"
    ),
]


def _build_fetch_config(
    source, path, from_date, to_date, country, district, item_types
):
    """Build FetchConfig from CLI source options with validation."""
    from pipeline.fetch import FetchConfig

    if source == "local":
        if not path:
            raise click.UsageError("--path is required when --source is local")
        return FetchConfig(source_dir=path)
    if not from_date or not to_date:
        raise click.UsageError(
            "--from-date and --to-date are required when --source is nas"
        )
    return FetchConfig(
        from_date=from_date,
        to_date=to_date,
        country=country,
        district=district,
        item_types=_parse_item_types(item_types) if item_types else None,
    )


# ---------------------------------------------------------------------------
# prepare: fetch + media processing
# ---------------------------------------------------------------------------


@cli.command(cls=_CfgAwareCommand)
@click.pass_context
@_name_option
@_use_cfg_option
@_apply_options(_source_options)
@_force_option
def prepare(
    ctx,
    run_name,
    use_cfg_file,
    source,
    path,
    from_date,
    to_date,
    country,
    district,
    item_types,
    force,
):
    """Fetch media and generate thumbnails + video previews (cached, use --force to regenerate)."""
    from pipeline.prepare import PrepareConfig

    if use_cfg_file:
        _validate_use_cfg(ctx)
        saved = load_run_config(use_cfg_file)
        source, path, from_date, to_date, country, district, item_types = (
            _load_source_fields(saved)
        )

    defaults = _collect_defaults(ctx) if not use_cfg_file else set()
    cli_params = _build_cli_params(
        source=source,
        path=path,
        from_date=from_date,
        to_date=to_date,
        country=country,
        district=district,
        item_types=item_types,
    )

    _run_pipeline(
        run_name,
        fetch=_build_fetch_config(
            source, path, from_date, to_date, country, district, item_types
        ),
        prepare=PrepareConfig(force=force),
        stages=["fetch", "prepare"],
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
    source,
    path,
    from_date,
    to_date,
    country,
    district,
    item_types,
    force,
    duration,
    trip_type,
    style,
    focus,
    lang,
    model,
    music,
    resolution,
    quality,
):
    """Run the full pipeline end-to-end."""
    from pipeline.assemble import AssembleConfig
    from pipeline.plan import PlanConfig
    from pipeline.prepare import PrepareConfig

    if use_cfg_file:
        _validate_use_cfg(ctx)
        saved = load_run_config(use_cfg_file)
        source, path, from_date, to_date, country, district, item_types = (
            _load_source_fields(saved)
        )
        duration, model, lang, trip_type, style, focus, music = _load_plan_fields(saved)
        resolution, quality = _load_assemble_fields(saved)

    resolved_model, resolved_thinking = _resolve_planning(model)
    w, h, fps = resolution
    stages = ["fetch", "prepare", "plan"]
    music_file = None if music == "none" else music
    if music != "none":
        stages.append("generate_music")
    stages.append("assemble")

    defaults = _collect_defaults(ctx) if not use_cfg_file else set()
    cli_params = _build_cli_params(
        source=source,
        path=path,
        from_date=from_date,
        to_date=to_date,
        country=country,
        district=district,
        item_types=item_types,
        duration=duration,
        model=model,
        resolution=resolution,
        lang=lang,
        trip_type=trip_type,
        style=style,
        focus=focus,
        music=music,
        bitrate=quality,
    )

    _run_pipeline(
        run_name,
        fetch=_build_fetch_config(
            source, path, from_date, to_date, country, district, item_types
        ),
        prepare=PrepareConfig(force=force),
        plan=PlanConfig(
            style=style,
            target_duration=duration,
            focus=focus,
            trip_type=trip_type,
            language=lang,
            model=resolved_model,
            thinking_level=resolved_thinking,
            music_file=music_file,
            force=force,
        ),
        assemble=AssembleConfig(w=w, h=h, fps=fps, quality=quality),
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
    lang,
    model,
    music,
    force,
):
    """Call Gemini to generate a new EDL (increments version). Requires prepare to have run first."""
    from pipeline.plan import PlanConfig

    if use_cfg_file:
        _validate_use_cfg(ctx)
        saved = load_run_config(use_cfg_file)
        duration, model, lang, trip_type, style, focus, music = _load_plan_fields(saved)

    resolved_model, resolved_thinking = _resolve_planning(model)
    music_file = None if music == "none" else music
    stages = ["plan"]
    if music != "none":
        stages.append("generate_music")

    defaults = _collect_defaults(ctx) if not use_cfg_file else set()
    cli_params = _build_cli_params(
        duration=duration,
        model=model,
        lang=lang,
        trip_type=trip_type,
        style=style,
        focus=focus,
        music=music,
    )

    _run_pipeline(
        run_name,
        plan=PlanConfig(
            style=style,
            target_duration=duration,
            focus=focus,
            trip_type=trip_type,
            language=lang,
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
def assemble(ctx, run_name, use_cfg_file, version, resolution, quality):
    """Render video from EDL. Uses latest version unless -v specified."""
    from pipeline.assemble import AssembleConfig

    if use_cfg_file:
        _validate_use_cfg(ctx)
        saved = load_run_config(use_cfg_file)
        resolution, quality = _load_assemble_fields(saved)

    w, h, fps = resolution

    defaults = _collect_defaults(ctx) if not use_cfg_file else set()
    cli_params = _build_cli_params(resolution=resolution, bitrate=quality)

    _run_pipeline(
        run_name,
        assemble=AssembleConfig(w=w, h=h, fps=fps, quality=quality, version=version),
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
    cfg_path = config_path_for(cfg.workspace)
    try:
        text = cfg_path.read_text()
    except FileNotFoundError:
        raise click.UsageError(
            f"No saved config at {cfg_path}.\n"
            "Run the pipeline with full parameters first."
        )
    click.echo(f"# {cfg_path}")
    click.echo(text)
