"""CLI entry point for the vlog pipeline.

Runs pipeline stages directly in a single Python process — no external
services needed. Each stage caches its output; re-running is fast.
"""

from __future__ import annotations

import json
import logging
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from pipeline.assemble import AssembleConfig
    from pipeline.config import Config
    from pipeline.fetch import FetchConfig
    from pipeline.plan import PlanConfig
    from pipeline.prepare import PrepareConfig

# ---------------------------------------------------------------------------
# SIGINT handler — first Ctrl+C sets flag, second force-quits
# ---------------------------------------------------------------------------

_interrupted = False


def _handle_sigint(sig, frame):
    global _interrupted
    if _interrupted:
        sys.exit(1)
    _interrupted = True
    print("\n\u26a0 Interrupted \u2014 finishing current operation...")


signal.signal(signal.SIGINT, _handle_sigint)


# ---------------------------------------------------------------------------
# Pipeline display
# ---------------------------------------------------------------------------

STAGES = ["fetch", "prepare", "plan", "generate_music", "assemble"]

_ICON_PENDING = "\u25cb"  # ○
_ICON_RUNNING = "\u23f3"  # ⏳
_ICON_DONE = "\u2705"  # ✅
_ICON_FAILED = "\u274c"  # ❌


class _PipelineDisplay:
    """Live terminal progress display for pipeline stages.

    Uses ANSI cursor-up codes to overwrite previous output on each update.
    Falls back to simple one-shot print lines when stderr is not a TTY.
    """

    def __init__(self, run_name: str, headline: str, stages: list[str]):
        self._run_name = run_name
        self._headline = headline
        self._stages = stages
        self._is_tty = sys.stderr.isatty()
        self._t_start = time.monotonic()
        self._prev_lines = 0  # how many lines we printed last time

        # Per-stage state
        self._status: dict[str, str] = {s: "pending" for s in stages}
        self._detail: dict[str, str] = {s: "" for s in stages}
        self._duration: dict[str, str] = {s: "" for s in stages}

    # -- Public API --

    def start(self, stage: str) -> None:
        self._status[stage] = "running"
        self._detail[stage] = ""
        self._duration[stage] = ""
        self._render()

    def update(self, stage: str, detail: str) -> None:
        self._detail[stage] = detail
        # Only re-render in TTY mode to avoid flooding non-TTY output
        if self._is_tty:
            self._render()

    def done(self, stage: str, detail: str, duration: float) -> None:
        self._status[stage] = "done"
        self._detail[stage] = detail
        cached = duration < 0.5
        self._duration[stage] = f"{duration:.0f}s" + (" (cached)" if cached else "")
        self._render()

    def fail(self, stage: str, error: str) -> None:
        self._status[stage] = "failed"
        self._detail[stage] = error[:60]
        self._render()

    # -- Rendering --

    def _icon(self, status: str) -> str:
        return {
            "pending": _ICON_PENDING,
            "running": _ICON_RUNNING,
            "done": _ICON_DONE,
            "failed": _ICON_FAILED,
        }.get(status, _ICON_PENDING)

    def _format_stage_name(self, stage: str) -> str:
        return stage.replace("_", " ")

    def _render(self) -> None:
        elapsed = time.monotonic() - self._t_start

        lines: list[str] = []
        lines.append("")
        lines.append(f"\U0001f3ac {self._run_name} \u2014 {self._headline}")
        lines.append("")

        for stage in self._stages:
            icon = self._icon(self._status[stage])
            name = self._format_stage_name(stage)
            detail = self._detail[stage]
            dur = self._duration[stage]

            # Build the line with aligned columns
            stage_col = f"  {icon} {name:<17s}"
            detail_col = f"{detail:<30s}" if detail else " " * 30
            dur_col = f"{dur}" if dur else ""
            lines.append(f"{stage_col}{detail_col}{dur_col}")

        lines.append("")
        lines.append(f"  Elapsed: {elapsed:.0f}s")
        lines.append("")

        if self._is_tty:
            # Move cursor up to overwrite previous output
            if self._prev_lines > 0:
                sys.stderr.write(f"\033[{self._prev_lines}F")
                # Clear each line we're about to overwrite
                for _ in range(self._prev_lines):
                    sys.stderr.write("\033[2K\033[1B")
                sys.stderr.write(f"\033[{self._prev_lines}F")

            output = "\n".join(lines)
            sys.stderr.write(output)
            sys.stderr.flush()
            self._prev_lines = len(lines)
        else:
            # Non-TTY: only print when a stage completes or fails
            for stage in self._stages:
                st = self._status[stage]
                if st in ("done", "failed") and not getattr(self, f"_printed_{stage}", False):
                    icon = self._icon(st)
                    name = self._format_stage_name(stage)
                    detail = self._detail[stage]
                    dur = self._duration[stage]
                    sys.stderr.write(f"{icon} {name}: {detail}  {dur}\n")
                    sys.stderr.flush()
                    setattr(self, f"_printed_{stage}", True)


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------


def _setup_logging(run_name: str) -> logging.Logger:
    """Configure dual-output logger: terminal + run.log file."""
    logger = logging.getLogger("vlog")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    # Terminal
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(console)

    # File — one log per run, timestamped
    from pipeline.config import Config

    log_dir = Path(Config.run_workspace(run_name=run_name))
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"run_{ts}.log"
    fh = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)

    return logger


def _progress_cb(logger: logging.Logger, display: _PipelineDisplay, stage: str, t0: float):
    """Create a progress callback that logs to file and updates display."""

    def cb(current: int, total: int, name: str) -> None:
        if total == 0:
            return
        # Update display on every callback
        display.update(stage, f"{current}/{total}")
        # Log at ~10% intervals to file
        if current % max(total // 10, 1) == 0 or current == total:
            elapsed = time.monotonic() - t0
            eta = (elapsed / current * (total - current) / 60) if current else 0
            pct = current / total * 100
            logger.info(f"{stage}: {current}/{total} ({pct:.0f}%) ETA {eta:.1f}min \u2014 {name}")

    return cb


def _write_status(ws: Path, status: dict) -> None:
    """Write run status JSON."""
    (ws / "run_status.json").write_text(json.dumps(status, indent=2, default=str))


def _build_headline(pc: _PipelineContext, stages: list[str]) -> str:
    """Build a short headline from plan config for display."""
    parts = []
    if pc.plan:
        if pc.plan.target_duration:
            parts.append(f"{pc.plan.target_duration}s")
        if pc.plan.style:
            parts.append(pc.plan.style)
        if pc.plan.trip_type:
            parts.append(f"{pc.plan.trip_type} vlog")
    if not parts:
        parts.append(", ".join(stages))
    return " ".join(parts)


def _check_interrupted(display: _PipelineDisplay, status: dict, ws: Path, logger: logging.Logger):
    """Check if Ctrl+C was pressed between stages. If so, save and exit."""
    global _interrupted
    if _interrupted:
        logger.info("Pipeline interrupted by user")
        status["result"] = "interrupted"
        status["completed_at"] = datetime.now(timezone.utc).isoformat()
        _write_status(ws, status)
        sys.exit(130)


@dataclass
class _PipelineContext:
    cfg: Config
    status: dict
    logger: logging.Logger
    display: _PipelineDisplay | None = None
    fetch: FetchConfig | None = None
    prepare: PrepareConfig | None = None
    plan: PlanConfig | None = None
    assemble: AssembleConfig | None = None

    @property
    def ws(self) -> Path:
        return Path(self.cfg.workspace)

    def log(self, msg: str) -> None:
        self.logger.info(msg)


def _run_fetch(pc: _PipelineContext):
    assert pc.fetch is not None and pc.display is not None
    _check_interrupted(pc.display, pc.status, pc.ws, pc.logger)
    pc.display.start("fetch")
    t0 = time.monotonic()
    manifest_path = pc.cfg.manifest_path

    if manifest_path.exists():
        items = json.loads(manifest_path.read_text())
        dur = time.monotonic() - t0
        pc.log(f"Fetch: {len(items)} items (cached)")
        pc.display.done("fetch", f"{len(items)} items", dur)
        pc.status["stages"]["fetch"] = {"status": "cached", "items": len(items)}
    else:
        fc = pc.fetch
        cb = _progress_cb(pc.logger, pc.display, "fetch", t0)
        if fc.source_dir:
            from pipeline.fetch import fetch_local

            items = fetch_local(pc.cfg, fc, progress_callback=cb)
        else:
            from pipeline.fetch import fetch

            items = fetch(pc.cfg, fc, progress_callback=cb)
        dur = time.monotonic() - t0
        pc.log(f"Fetch: {len(items)} items in {dur:.0f}s")
        pc.display.done("fetch", f"{len(items)} items", dur)
        pc.status["stages"]["fetch"] = {"status": "ok", "duration_s": round(dur, 1), "items": len(items)}


def _run_prepare(pc: _PipelineContext):
    assert pc.prepare is not None and pc.display is not None
    _check_interrupted(pc.display, pc.status, pc.ws, pc.logger)
    pc.display.start("prepare")
    t0 = time.monotonic()
    analysis_path = pc.cfg.analysis_path
    manifest_path = pc.cfg.manifest_path
    prep = pc.prepare

    stale = (
        manifest_path.exists()
        and analysis_path.exists()
        and manifest_path.stat().st_mtime > analysis_path.stat().st_mtime
    )
    if stale:
        pc.log("Manifest is newer \u2014 re-preparing")

    if not prep.force and not stale and analysis_path.exists():
        results = json.loads(analysis_path.read_text())
        n_photos = sum(1 for r in results if r.get("media_type") == "photo")
        n_videos = len(results) - n_photos
        dur = time.monotonic() - t0
        pc.log(f"Prepare: {len(results)} items ({n_photos} photos, {n_videos} videos) \u2014 cached")
        pc.display.done("prepare", f"{n_photos} photos, {n_videos} videos", dur)
        pc.status["stages"]["prepare"] = {"status": "cached"}
    else:
        from pipeline.prepare import prepare

        prepare(
            pc.cfg,
            prep,
            progress_callback=_progress_cb(pc.logger, pc.display, "prepare", t0),
        )
        dur = time.monotonic() - t0
        pc.log(f"Prepare: done in {dur:.0f}s")
        if analysis_path.exists():
            results = json.loads(analysis_path.read_text())
            n_photos = sum(1 for r in results if r.get("media_type") == "photo")
            n_videos = len(results) - n_photos
            pc.display.done("prepare", f"{n_photos} photos, {n_videos} videos", dur)
        else:
            pc.display.done("prepare", "done", dur)
        pc.status["stages"]["prepare"] = {"status": "ok", "duration_s": round(dur, 1)}


def _run_plan(pc: _PipelineContext):
    """Execute the plan stage."""
    assert pc.plan is not None and pc.display is not None
    _check_interrupted(pc.display, pc.status, pc.ws, pc.logger)
    pc.display.start("plan")
    t0 = time.monotonic()

    from pipeline.plan import plan as do_plan

    edl, version = do_plan(pc.cfg, pc.plan, progress_callback=_progress_cb(pc.logger, pc.display, "plan", t0))

    all_items = edl.all_items()
    n_videos = sum(1 for i in all_items if i.media_type == "video")
    n_photos = len(all_items) - n_videos
    n_keep_audio = sum(1 for i in all_items if i.keep_audio)
    vid_time = sum(i.display_duration for i in all_items if i.media_type == "video")
    total_time = sum(i.display_duration for i in all_items)
    vid_pct = int(vid_time / total_time * 100) if total_time > 0 else 0

    dur = time.monotonic() - t0
    plan_detail = f"v{version}: {n_photos}p+{n_videos}v, " f"~{edl.estimated_duration():.0f}s"
    pc.display.done("plan", plan_detail, dur)

    pc.log(
        f"Plan: EDL v{version} \u2014 {len(edl.segments)} segments, "
        f"{n_photos} photos + {n_videos} videos ({vid_pct}% video), "
        f"~{edl.estimated_duration():.0f}s, {dur:.0f}s"
    )
    if n_keep_audio:
        pc.log(f"  Speech preserved: {n_keep_audio} clips")
    for seg in edl.segments:
        pc.log(f"  {seg.name}: {len(seg.items)} items, transition={seg.transition}")

    pc.status["stages"]["plan"] = {
        "status": "ok",
        "version": version,
        "duration_s": round(dur, 1),
        "segments": len(edl.segments),
        "items": len(all_items),
    }


def _run_generate_music(pc: _PipelineContext):
    """Execute the generate_music stage."""
    assert pc.display is not None
    _check_interrupted(pc.display, pc.status, pc.ws, pc.logger)
    pc.display.start("generate_music")
    t0 = time.monotonic()

    from pipeline.music import generate_music_for_edl

    track = generate_music_for_edl(
        pc.cfg,
        progress_callback=_progress_cb(pc.logger, pc.display, "generate_music", t0),
    )

    dur = time.monotonic() - t0
    if track:
        pc.log(f"Music: generated {track.name} in {dur:.0f}s")
        pc.display.done("generate_music", track.name, dur)
        pc.status["stages"]["generate_music"] = {"status": "ok", "duration_s": round(dur, 1)}
    else:
        pc.log("Music: skipped")
        pc.display.done("generate_music", "skipped", dur)
        pc.status["stages"]["generate_music"] = {"status": "skipped"}


def _run_assemble(pc: _PipelineContext):
    assert pc.assemble is not None and pc.display is not None
    _check_interrupted(pc.display, pc.status, pc.ws, pc.logger)
    pc.display.start("assemble")
    t0 = time.monotonic()
    ac = pc.assemble

    from pipeline.assemble import assemble as do_assemble
    from pipeline.edl import find_latest_version

    # Handle edl_path: copy external EDL as new version
    if ac.edl_path:
        import shutil

        version = find_latest_version(pc.cfg) + 1
        dest = pc.cfg.edl_path(version)
        shutil.copy(ac.edl_path, dest)
        pc.log(f"Using EDL from {ac.edl_path} as v{version}")
        # Update config with resolved version
        from dataclasses import replace

        ac = replace(ac, version=version, edl_path=None)
    elif not ac.version:
        from dataclasses import replace

        ac = replace(ac, version=find_latest_version(pc.cfg))

    pc.log(f"Render: {ac.w}x{ac.h} {ac.fps}fps (EDL v{ac.version})")

    out, issues = do_assemble(
        pc.cfg,
        ac,
        progress_callback=_progress_cb(pc.logger, pc.display, "assemble", t0),
    )

    dur = time.monotonic() - t0
    size_mb = round(out.stat().st_size / 1024 / 1024, 1) if out.exists() else 0
    pc.log(f"Assemble: {out.name} ({size_mb}MB) in {dur:.0f}s")
    pc.display.done("assemble", f"{out.name} ({size_mb}MB)", dur)

    for issue in issues:
        level = issue.get("level", "warning")
        pc.log(f"  [{level.upper()}] {issue.get('check', '')}: {issue.get('message', '')}")

    pc.status["stages"]["assemble"] = {
        "status": "ok",
        "duration_s": round(dur, 1),
        "output": out.name,
        "size_mb": size_mb,
    }


_STAGE_RUNNERS = {
    "fetch": _run_fetch,
    "prepare": _run_prepare,
    "plan": _run_plan,
    "generate_music": _run_generate_music,
    "assemble": _run_assemble,
}


def _run_pipeline(run_name: str, *, stages: list[str], fetch=None, prepare=None, plan=None, assemble=None):
    """Execute pipeline stages directly in this process."""
    global _interrupted
    _interrupted = False

    from pipeline.config import Config

    active = stages
    ws_path = Config.run_workspace(run_name=run_name)
    cfg = Config.load(ws_path)
    cfg.ensure_dirs()

    logger = _setup_logging(run_name)

    status: dict = {
        "run_name": run_name,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "stages": {},
    }

    pc = _PipelineContext(
        cfg=cfg,
        status=status,
        logger=logger,
        fetch=fetch,
        prepare=prepare,
        plan=plan,
        assemble=assemble,
    )

    headline = _build_headline(pc, active)
    display = _PipelineDisplay(run_name, headline, active)
    pc.display = display
    t_start = time.monotonic()

    try:
        for stage in active:
            if stage in _STAGE_RUNNERS:
                _STAGE_RUNNERS[stage](pc)

        status["result"] = "success"

    except SystemExit:
        raise
    except Exception as e:
        # Mark the currently-running stage as failed in display
        for stage in active:
            if display._status.get(stage) == "running":
                display.fail(stage, str(e))
                break
        logger.error(f"Pipeline failed: {e}", exc_info=True)
        status["result"] = "failure"
        status["error"] = str(e)
        raise
    finally:
        status["completed_at"] = datetime.now(timezone.utc).isoformat()
        status["total_duration_s"] = round(time.monotonic() - t_start, 1)
        _write_status(pc.ws, status)
        total = status["total_duration_s"]
        result = status.get("result", "unknown")
        logger.info(f"Pipeline {result} in {total:.0f}s")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_name_option = click.option(
    "-n", "--name", "run_name", default="default", help="Run name (subdirectory under workspace/runs/)"
)


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
    Example:
      python run.py full -n singapore -s local -p ./photos -r 1080p30 --duration 180
    """


# Shared Click options (avoid repeating decorators)
_tz_option = click.option(
    "--timezone",
    "--tz",
    "tz_hours",
    default=None,
    type=int,
    help="UTC offset in hours (default: system local, e.g. -5 NYC, 8 SGT)",
)
_force_option = click.option("--force", is_flag=True, help="Force re-analyze (ignore cached)")

_plan_options = [
    click.option("--duration", default=60, type=int, help="Target vlog length in seconds"),
    click.option(
        "--trip-type",
        default="family",
        type=click.Choice(["family", "solo", "food", "adventure", "architecture", "general"]),
    ),
    click.option("--style", default="upbeat", type=click.Choice(["upbeat", "cinematic", "reflective", "energetic"])),
    click.option("--focus", default="", help="What to emphasize"),
    click.option("--lang", default="en", type=click.Choice(["en", "cn", "both"]), help="Text language"),
    click.option("--model", default=None, help="Gemini model override"),
    click.option("--music", default="auto", help="auto=Gemini Lyria (default), /path/to/file, none=no music"),
]

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
    raise click.BadParameter(f"Unknown resolution '{value}'. Use a preset ({presets}) or WxHxFPS (e.g. 1920x1080x30)")


_assemble_options = [
    click.option(
        "--resolution",
        "-r",
        required=True,
        callback=_parse_resolution,
        expose_value=True,
        is_eager=False,
        help="Resolution preset (4k60, 4k30, 2k60, 2k30, 1080p60, 1080p30, 720p30) or WxHxFPS",
    ),
    click.option(
        "--quality", default=1.0, type=float, help="Bitrate multiplier: 0.5=smaller, 1.0=YouTube (default), 2.0=master"
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
    click.option("-f", "--from-date", default=None, help="NAS start date YYYY-MM-DD (required when --source nas)"),
    click.option("-t", "--to-date", default=None, help="NAS end date YYYY-MM-DD (required when --source nas)"),
    click.option("--country", default=None, help="NAS filter: country"),
    click.option("--district", default=None, help="NAS filter: district/city"),
    click.option("--item-types", default=None, help="NAS filter: photo,video,live,motion"),
]


def _build_fetch_config(source, path, from_date, to_date, country, district, item_types):
    """Build FetchConfig from CLI source options with validation."""
    from pipeline.fetch import FetchConfig

    if source == "local":
        if not path:
            raise click.UsageError("--path is required when --source is local")
        return FetchConfig(source_dir=path)
    if not from_date or not to_date:
        raise click.UsageError("--from-date and --to-date are required when --source is nas")
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
def prepare(run_name, source, path, from_date, to_date, country, district, item_types, tz_hours, force):
    """Fetch and prepare media (local folder or NAS)."""
    from pipeline.prepare import PrepareConfig

    _run_pipeline(
        run_name,
        fetch=_build_fetch_config(source, path, from_date, to_date, country, district, item_types),
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

    w, h, fps = resolution
    stages = ["fetch", "prepare", "plan"]
    music_file = None if music == "none" else music
    if music != "none":
        stages.append("generate_music")
    stages.append("assemble")

    _run_pipeline(
        run_name,
        fetch=_build_fetch_config(source, path, from_date, to_date, country, district, item_types),
        prepare=PrepareConfig(force=force, tz_hours=tz_hours),
        plan=PlanConfig(
            style=style,
            target_duration=duration,
            focus=focus,
            trip_type=trip_type,
            language=lang,
            model=model,
            music_file=music_file,
            tz_hours=tz_hours,
            force=force,
        ),
        assemble=AssembleConfig(w=w, h=h, fps=fps, quality=quality, skip_broken=True),
        stages=stages,
    )


@cli.command()
@_name_option
@_apply_options(_plan_options)
@_tz_option
@_force_option
def plan(run_name, duration, trip_type, style, focus, lang, model, music, tz_hours, force):
    """Re-plan only (uses cached media + analysis). Run assemble separately to render."""
    from pipeline.plan import PlanConfig

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
            model=model,
            music_file=music_file,
            tz_hours=tz_hours,
            force=force,
        ),
        stages=stages,
    )


@cli.command()
@_name_option
@click.option("-v", "--version", default=None, type=int, help="EDL version to render")
@click.option("--edl", "edl_path", default=None, type=click.Path(exists=True), help="EDL JSON path (overrides version)")
@_apply_options(_assemble_options)
def assemble(run_name, version, edl_path, resolution, quality):
    """Re-render the vlog from current or specified EDL version."""
    from pipeline.assemble import AssembleConfig

    w, h, fps = resolution
    _run_pipeline(
        run_name,
        assemble=AssembleConfig(w=w, h=h, fps=fps, quality=quality, version=version, edl_path=edl_path),
        stages=["assemble"],
    )


# ---------------------------------------------------------------------------
# Workspace management (unchanged — no Dagster dependency)
# ---------------------------------------------------------------------------


def _fmt_size(size_bytes: int) -> str:
    if size_bytes >= 1024**3:
        return f"{size_bytes / 1024**3:.1f} GB"
    if size_bytes >= 1024**2:
        return f"{size_bytes / 1024**2:.0f} MB"
    return f"{size_bytes / 1024:.0f} KB"


def _dir_size(path) -> tuple[int, int]:
    path = Path(path)
    if not path.exists():
        return 0, 0
    total = count = 0
    for f in path.rglob("*"):
        if f.is_file():
            total += f.stat().st_size
            count += 1
    return total, count


def _age_str(mtime: float) -> str:
    age = time.time() - mtime
    if age < 3600:
        return f"{max(1, int(age / 60))}m ago"
    if age < 86400:
        return f"{int(age / 3600)}h ago"
    return f"{int(age / 86400)}d ago"


def _latest_mtime(path) -> float:
    path = Path(path)
    latest = 0.0
    if path.exists():
        for f in path.rglob("*"):
            if f.is_file():
                latest = max(latest, f.stat().st_mtime)
    return latest


def _run_detail(run_dir) -> dict:
    info: dict = {"name": run_dir.name, "path": run_dir}
    size, count = _dir_size(run_dir)
    info["size"] = size
    info["file_count"] = count
    info["last_used"] = _latest_mtime(run_dir)

    edls = sorted(run_dir.glob("edl_v*.json"), key=lambda f: f.name)
    info["edl_versions"] = len(edls)
    if edls:
        try:
            data = json.loads(edls[-1].read_text())
            segs = data.get("segments", [])
            info["edl_latest"] = int(edls[-1].stem.split("_v")[1])
            info["title"] = data.get("title", "")
            info["segments"] = len(segs)
            info["items"] = sum(len(s.get("items", [])) for s in segs)
            info["target_duration"] = data.get("target_duration", 0)
            info["language"] = data.get("language", "en")
            info["n_videos"] = sum(1 for s in segs for i in s.get("items", []) if i.get("media_type") == "video")
            info["n_keep_audio"] = sum(1 for s in segs for i in s.get("items", []) if i.get("keep_audio"))
        except Exception:
            pass

    output_dir = run_dir / "output"
    outputs = sorted(output_dir.glob("vlog_v*.mp4")) if output_dir.exists() else []
    info["outputs"] = [{"path": o, "version": int(o.stem.split("_v")[1]), "size": o.stat().st_size} for o in outputs]
    info["old_output_bytes"] = sum(o["size"] for o in info["outputs"][:-1]) if len(info["outputs"]) > 1 else 0

    intermediates = (
        (
            list(output_dir.glob("*_nomix.mp4"))
            + list(output_dir.glob("*_speech.wav"))
            + list(output_dir.glob("_group_*.mp4"))
            + list(output_dir.glob("_group_*.txt"))
        )
        if output_dir.exists()
        else []
    )
    info["intermediate_bytes"] = sum(f.stat().st_size for f in intermediates)
    info["intermediate_files"] = intermediates

    clips_dir = run_dir / "clips"
    legacy = list(clips_dir.glob("*_txt.mp4")) if clips_dir.exists() else []
    info["legacy_txt_bytes"] = sum(f.stat().st_size for f in legacy)
    info["legacy_txt_files"] = legacy

    clips_size, clips_count = _dir_size(clips_dir)
    info["clips_size"] = clips_size
    info["clips_count"] = clips_count - len(legacy)

    return info


@cli.command()
@click.option(
    "--clean",
    type=click.Choice(["safe", "cache", "media", "all"]),
    default=None,
    help="safe=old outputs+intermediates, cache=analysis/thumbnails, media=source files, all=everything",
)
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
def workspace(clean, yes):
    """Show workspace disk usage with pipeline-aware details."""
    import shutil

    ws = Path("./workspace")
    if not ws.exists():
        click.echo("No workspace directory found.")
        return

    shared = [
        ("media", "Source photos & videos", ws / "media"),
        ("music", "Generated music (Lyria cache)", ws / "music"),
        ("preview_clips", "Video preview clips (shared)", ws / "preview_clips"),
        ("analysis_cache", "Analysis cache", ws / "analysis_cache"),
        ("thumbnails", "Photo thumbnails", ws / "thumbnails"),
        ("heic_converted", "HEIC→JPEG conversions", ws / "heic_converted"),
    ]

    total = 0
    click.echo("\n=== Workspace ===\n")
    click.echo("Shared data:")

    media_size, media_count = _dir_size(ws / "media")
    total += media_size
    if media_size > 0:
        n_photos = n_videos = 0
        for mf in (ws / "runs").rglob("manifest.json") if (ws / "runs").exists() else []:
            try:
                manifest = json.loads(mf.read_text())
                for item in manifest:
                    t = item.get("type", 0)
                    if t == 0:
                        n_photos += 1
                    elif t == 1:
                        n_videos += 1
                break
            except Exception:
                pass
        media_detail = f"{media_count} files"
        if n_photos or n_videos:
            media_detail = f"{n_photos} photos, {n_videos} videos"
        click.echo(f"  {_fmt_size(media_size):>8s}  {media_detail:>22s}  Source media")

    for key, label, path in shared[1:]:
        size, count = _dir_size(path)
        total += size
        if size > 0:
            click.echo(f"  {_fmt_size(size):>8s}  {count:>17d} files  {label}")

    runs_dir = ws / "runs"
    runs = []
    if runs_dir.exists():
        for d in sorted(runs_dir.iterdir()):
            if d.is_dir():
                runs.append(_run_detail(d))
        runs.sort(key=lambda r: r["last_used"], reverse=True)

    runs_total = sum(r["size"] for r in runs)
    total += runs_total

    click.echo(f"\nRuns: {_fmt_size(runs_total)} across {len(runs)} runs")
    click.echo()

    total_reclaimable = 0
    for r in runs:
        age = _age_str(r["last_used"]) if r["last_used"] else "empty"
        click.echo(f"  {r['name']} ({_fmt_size(r['size'])}, {age})")

        if "edl_latest" in r:
            edl_parts = [f"v{r['edl_latest']}: {r['segments']} segments, {r['items']} items"]
            if r.get("n_videos"):
                edl_parts.append(f"{r['n_videos']} videos")
            if r.get("n_keep_audio"):
                edl_parts.append(f"{r['n_keep_audio']} keep_audio")
            edl_parts.append(f"~{r['target_duration']}s")
            if r.get("language", "en") != "en":
                edl_parts.append(f"lang={r['language']}")
            click.echo(f"    EDL {', '.join(edl_parts)}")
            if r.get("title"):
                click.echo(f"    \"{r['title'][:60]}\"")
        elif r["edl_versions"] > 0:
            click.echo(f"    EDL: {r['edl_versions']} version(s)")

        if r["outputs"]:
            parts = []
            for o in r["outputs"]:
                marker = ""
                if o is r["outputs"][-1] and len(r["outputs"]) > 1:
                    marker = " <-- latest"
                parts.append(f"v{o['version']} ({_fmt_size(o['size'])}){marker}")
            click.echo(f"    Output: {', '.join(parts)}")
        else:
            click.echo("    Output: (none)")

        if r["clips_count"] > 0:
            click.echo(f"    Clips: {r['clips_count']} cached ({_fmt_size(r['clips_size'])})")

        reclaim_parts = []
        if r["old_output_bytes"]:
            reclaim_parts.append(f"{_fmt_size(r['old_output_bytes'])} old outputs")
        if r["intermediate_bytes"]:
            reclaim_parts.append(f"{_fmt_size(r['intermediate_bytes'])} intermediates")
        if r["legacy_txt_bytes"]:
            reclaim_parts.append(f"{_fmt_size(r['legacy_txt_bytes'])} legacy _txt clips")
        if reclaim_parts:
            r_total = r["old_output_bytes"] + r["intermediate_bytes"] + r["legacy_txt_bytes"]
            total_reclaimable += r_total
            click.echo(f"    Prune: {', '.join(reclaim_parts)}")

    click.echo(f"\n{'─' * 50}")
    click.echo(f"Total: {_fmt_size(total)}")
    if total_reclaimable:
        click.echo(f"Reclaimable with --clean safe: {_fmt_size(total_reclaimable)}")

    if clean is None:
        return

    if clean == "safe":
        if total_reclaimable == 0:
            click.echo("\nNothing to clean.")
            return

        click.echo(f"\nWill free {_fmt_size(total_reclaimable)}:")
        to_delete: list[Path] = []
        for r in runs:
            files: list[Path] = []
            if r["old_output_bytes"]:
                files += [o["path"] for o in r["outputs"][:-1]]
            files += r["intermediate_files"]
            files += r["legacy_txt_files"]
            if files:
                click.echo(f"  {r['name']}: {len(files)} files ({_fmt_size(sum(f.stat().st_size for f in files))})")
                to_delete += files

        if not yes:
            click.confirm("Proceed?", abort=True)

        freed = 0
        for f in to_delete:
            freed += f.stat().st_size
            f.unlink()
        click.echo(f"Cleaned {len(to_delete)} files, freed {_fmt_size(freed)}.")
        return

    targets = []
    if clean in ("media", "all"):
        targets.append(("media", ws / "media"))
    if clean in ("cache", "all"):
        targets += [
            ("analysis_cache", ws / "analysis_cache"),
            ("thumbnails", ws / "thumbnails"),
            ("preview_clips", ws / "preview_clips"),
            ("heic_converted", ws / "heic_converted"),
            ("music", ws / "music"),
        ]
    if clean == "all":
        targets.append(("runs", ws / "runs"))

    sizes = [(n, p, _dir_size(p)[0]) for n, p in targets if p.exists()]
    if not sizes:
        click.echo("Nothing to clean.")
        return

    total_clean = sum(s for _, _, s in sizes)
    click.echo(f"\nWill delete {_fmt_size(total_clean)}:")
    for name, path, s in sizes:
        click.echo(f"  {_fmt_size(s):>8s}  {path}")

    if not yes:
        click.confirm("Proceed?", abort=True)

    for name, path, _ in sizes:
        shutil.rmtree(path, ignore_errors=True)
        click.echo(f"  Deleted {path}")
    click.echo("Done.")


if __name__ == "__main__":
    cli()
