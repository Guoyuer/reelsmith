"""Click CLI group, options, presets, parsers, and command definitions."""

from __future__ import annotations

import click

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

_tz_option = click.option(
    "--timezone",
    "--tz",
    "tz_hours",
    default=None,
    type=int,
    help="UTC offset in hours (default: system local, e.g. -5 NYC, 8 SGT)",
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
        type=click.Choice(
            ["family", "solo", "food", "adventure", "architecture", "general"]
        ),
        help="Narrative style: family=close-ups+laughter, solo=landscapes+wonder, food=dishes+markets, etc.",
    ),
    click.option(
        "--style",
        default="upbeat",
        type=click.Choice(["upbeat", "cinematic", "reflective", "energetic"]),
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
        type=click.Choice(["en", "cn", "both"]),
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


def _parse_resolution(ctx, param, value: str) -> tuple[int, int, int]:
    """Parse resolution preset (e.g. '4k60', '1080p30') or WxHxFPS (e.g. '1920x1080x30')."""
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
# Shared source options (--source local|nas, --path, NAS filters)
# ---------------------------------------------------------------------------

_source_options = [
    click.option(
        "--source",
        "-s",
        required=True,
        cls=_RequiredPrefixOption,
        type=click.Choice(["local", "nas"]),
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


@cli.command()
@_name_option
@_apply_options(_source_options)
@_tz_option
@_force_option
def prepare(
    run_name,
    source,
    path,
    from_date,
    to_date,
    country,
    district,
    item_types,
    tz_hours,
    force,
):
    """Fetch media and generate thumbnails + video previews (cached, use --force to regenerate)."""
    from pipeline.prepare import PrepareConfig

    _run_pipeline(
        run_name,
        fetch=_build_fetch_config(
            source, path, from_date, to_date, country, district, item_types
        ),
        prepare=PrepareConfig(force=force, tz_hours=tz_hours),
        stages=["fetch", "prepare"],
    )


# ---------------------------------------------------------------------------
# full: end-to-end pipeline
# ---------------------------------------------------------------------------


@cli.command()
@_name_option
@_apply_options(_source_options)
@_tz_option
@_force_option
@_apply_options(_plan_options)
@_apply_options(_assemble_options)
def full(
    run_name,
    source,
    path,
    from_date,
    to_date,
    country,
    district,
    item_types,
    tz_hours,
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

    resolved_model, resolved_thinking = _resolve_planning(model)
    w, h, fps = resolution
    stages = ["fetch", "prepare", "plan"]
    music_file = None if music == "none" else music
    if music != "none":
        stages.append("generate_music")
    stages.append("assemble")

    _run_pipeline(
        run_name,
        fetch=_build_fetch_config(
            source, path, from_date, to_date, country, district, item_types
        ),
        prepare=PrepareConfig(force=force, tz_hours=tz_hours),
        plan=PlanConfig(
            style=style,
            target_duration=duration,
            focus=focus,
            trip_type=trip_type,
            language=lang,
            model=resolved_model,
            thinking_level=resolved_thinking,
            music_file=music_file,
            tz_hours=tz_hours,
            force=force,
        ),
        assemble=AssembleConfig(w=w, h=h, fps=fps, quality=quality),
        stages=stages,
    )


@cli.command()
@_name_option
@_apply_options(_plan_options)
@_tz_option
@_force_option
def plan(
    run_name, duration, trip_type, style, focus, lang, model, music, tz_hours, force
):
    """Call Gemini to generate a new EDL (increments version). Requires prepare to have run first."""
    from pipeline.plan import PlanConfig

    resolved_model, resolved_thinking = _resolve_planning(model)
    music_file = None if music == "none" else music
    stages = ["plan"]
    if music != "none":
        stages.append("generate_music")

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
            tz_hours=tz_hours,
            force=force,
        ),
        stages=stages,
    )


@cli.command()
@_name_option
@click.option(
    "-v",
    "--version",
    default=None,
    type=int,
    help="EDL version to render (default: latest)",
)
@_apply_options(_assemble_options)
def assemble(run_name, version, resolution, quality):
    """Render video from EDL. Uses latest version unless -v specified."""
    from pipeline.assemble import AssembleConfig

    w, h, fps = resolution
    _run_pipeline(
        run_name,
        assemble=AssembleConfig(w=w, h=h, fps=fps, quality=quality, version=version),
        stages=["assemble"],
    )
