"""CLI entry point for the vlog pipeline.

Runs pipeline stages directly in a single Python process — no external
services needed. Each stage caches its output; re-running is fast.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import click


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

_ICON_PENDING = "\u25cb"   # ○
_ICON_RUNNING = "\u23f3"   # ⏳
_ICON_DONE    = "\u2705"   # ✅
_ICON_FAILED  = "\u274c"   # ❌


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


def _build_headline(stage_configs: dict, stages: list[str]) -> str:
    """Build a short headline from plan config for display."""
    plan_cfg = stage_configs.get("plan", {})
    parts = []
    dur = plan_cfg.get("target_duration")
    if dur:
        parts.append(f"{dur}s")
    style = plan_cfg.get("style")
    if style:
        parts.append(style)
    trip = plan_cfg.get("trip_type")
    if trip:
        parts.append(f"{trip} vlog")
    if not parts:
        # Fallback: describe which stages are running
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


def _log_config(log_fn, stage: str, actual: dict, defaults: dict) -> None:
    """Log stage config with explicit/default markers.

    Prints each parameter showing whether it was explicitly set or fell back
    to its default value.
    """
    parts = []
    for key, default in defaults.items():
        if key in actual:
            parts.append(f"{key}={actual[key]}")
        else:
            parts.append(f"{key}={default} (default)")
    # Also log any keys in actual that aren't in defaults
    for key in actual:
        if key not in defaults:
            parts.append(f"{key}={actual[key]}")
    log_fn(f"[{stage}] {', '.join(parts)}")


def _run_pipeline(run_name: str, stage_configs: dict, *, stages: list[str] | None = None):
    """Execute pipeline stages directly in this process."""
    global _interrupted
    _interrupted = False

    from pipeline.config import Config

    active = stages or STAGES
    ws_path = Config.run_workspace(run_name=run_name)
    cfg = Config.load(ws_path)
    cfg.ensure_dirs()

    logger = _setup_logging(run_name)
    ws = Path(ws_path)
    log = logger.info

    headline = _build_headline(stage_configs, active)
    display = _PipelineDisplay(run_name, headline, active)

    status: dict = {
        "run_name": run_name,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "stages": {},
    }
    t_start = time.monotonic()

    try:
        # --- FETCH ---
        if "fetch" in active:
            _check_interrupted(display, status, ws, logger)
            display.start("fetch")
            fc = stage_configs.get("fetch", {})
            _log_config(log, "fetch", fc, {
                "source_dir": "NAS", "from_date": "", "to_date": "",
                "country": None, "district": None, "item_types": None,
            })
            t0 = time.monotonic()
            manifest_path = ws / "manifest.json"

            if not fc.get("force") and manifest_path.exists():
                items = json.loads(manifest_path.read_text())
                dur = time.monotonic() - t0
                log(f"Fetch: {len(items)} items (cached)")
                display.done("fetch", f"{len(items)} items", dur)
                status["stages"]["fetch"] = {"status": "cached", "items": len(items)}
            else:
                if fc.get("source_dir"):
                    from pipeline.fetch_local import fetch_local
                    items = fetch_local(cfg, source_dir=fc["source_dir"])
                else:
                    from pipeline.fetch import fetch
                    items = fetch(
                        cfg, from_date=fc.get("from_date", ""),
                        to_date=fc.get("to_date", ""),
                        country=fc.get("country"), first_level=fc.get("first_level"),
                        district=fc.get("district"), person_ids=fc.get("person_ids"),
                        item_types=fc.get("item_types"),
                    )
                dur = time.monotonic() - t0
                log(f"Fetch: {len(items)} items in {dur:.0f}s")
                display.done("fetch", f"{len(items)} items", dur)
                status["stages"]["fetch"] = {"status": "ok", "duration_s": round(dur, 1), "items": len(items)}

        # --- PREPARE ---
        if "prepare" in active:
            _check_interrupted(display, status, ws, logger)
            display.start("prepare")
            pc = stage_configs.get("prepare", {})
            _log_config(log, "prepare", pc, {
                "force": False, "family_names": None, "tz_offset": None,
            })
            t0 = time.monotonic()
            analysis_path = ws / "analysis.json"
            manifest_path = ws / "manifest.json"

            stale = (manifest_path.exists() and analysis_path.exists()
                     and manifest_path.stat().st_mtime > analysis_path.stat().st_mtime)
            if stale:
                log("Manifest is newer \u2014 re-preparing")

            if not pc.get("force") and not stale and analysis_path.exists():
                results = json.loads(analysis_path.read_text())
                n_photos = sum(1 for r in results if r.get("media_type") == "photo")
                n_videos = len(results) - n_photos
                dur = time.monotonic() - t0
                log(f"Prepare: {len(results)} items ({n_photos} photos, {n_videos} videos) \u2014 cached")
                display.done("prepare", f"{n_photos} photos, {n_videos} videos", dur)
                status["stages"]["prepare"] = {"status": "cached"}
            else:
                from pipeline.prepare import prepare
                result = prepare(
                    cfg, family_names=pc.get("family_names"),
                    force=pc.get("force", False),
                    progress_callback=_progress_cb(logger, display, "prepare", t0),
                    tz_hours=pc.get("tz_offset"),
                )
                dur = time.monotonic() - t0
                log(f"Prepare: done in {dur:.0f}s")
                # Read back results to get counts for display
                if analysis_path.exists():
                    results = json.loads(analysis_path.read_text())
                    n_photos = sum(1 for r in results if r.get("media_type") == "photo")
                    n_videos = len(results) - n_photos
                    display.done("prepare", f"{n_photos} photos, {n_videos} videos", dur)
                else:
                    display.done("prepare", "done", dur)
                status["stages"]["prepare"] = {"status": "ok", "duration_s": round(dur, 1)}

        # --- PLAN ---
        if "plan" in active:
            _check_interrupted(display, status, ws, logger)
            display.start("plan")
            pc = stage_configs.get("plan", {})
            _log_config(log, "plan", pc, {
                "style": "upbeat", "target_duration": 180, "trip_type": "family",
                "language": "en", "focus": "", "music_file": None,
                "width": 3840, "height": 2160, "fps": 60,
                "quality": 1.0, "tz_offset": None,
            })
            t0 = time.monotonic()

            from pipeline.plan import plan as do_plan
            edl, version = do_plan(
                cfg,
                style=pc.get("style", "upbeat"),
                target_duration=pc.get("target_duration", 180),
                focus=pc.get("focus", ""),
                trip_type=pc.get("trip_type", "family"),
                music_file=pc.get("music_file") or None,
                language=pc.get("language", "en"),
                resolution=(pc.get("width", 3840), pc.get("height", 2160)),
                fps=pc.get("fps", 60),
                quality=pc.get("quality", 1.0),
                tz_hours=pc.get("tz_offset"),
                model=pc.get("model"),
            )

            all_items = edl.all_items()
            n_videos = sum(1 for i in all_items if i.media_type == "video")
            n_photos = len(all_items) - n_videos
            n_keep = sum(1 for i in all_items if i.keep_audio)
            vid_time = sum(i.display_duration for i in all_items if i.media_type == "video")
            total_time = sum(i.display_duration for i in all_items)
            vid_pct = int(vid_time / total_time * 100) if total_time > 0 else 0

            dur = time.monotonic() - t0
            plan_detail = (f"v{version}: {n_photos}p+{n_videos}v, "
                           f"~{edl.estimated_duration():.0f}s")
            display.done("plan", plan_detail, dur)

            log(f"Plan: EDL v{version} \u2014 {len(edl.segments)} segments, "
                f"{n_photos} photos + {n_videos} videos ({vid_pct}% video), "
                f"~{edl.estimated_duration():.0f}s, {dur:.0f}s")
            if n_keep:
                log(f"  Speech preserved: {n_keep} clips")
            for seg in edl.segments:
                log(f"  {seg.name}: {len(seg.items)} items, transition={seg.transition}")

            status["stages"]["plan"] = {
                "status": "ok", "version": version,
                "duration_s": round(dur, 1),
                "segments": len(edl.segments),
                "items": len(all_items),
            }

        # --- GENERATE MUSIC ---
        if "generate_music" in active:
            _check_interrupted(display, status, ws, logger)
            display.start("generate_music")
            mc = stage_configs.get("generate_music", {})
            _log_config(log, "generate_music", mc, {
                "music_backend": "gemini",
            })
            t0 = time.monotonic()

            from pipeline.music import generate_music_for_edl
            track = generate_music_for_edl(
                cfg, backend=mc.get("music_backend", "gemini"),
            )

            dur = time.monotonic() - t0
            if track:
                log(f"Music: generated {track.name} in {dur:.0f}s")
                display.done("generate_music", track.name, dur)
                status["stages"]["generate_music"] = {"status": "ok", "duration_s": round(dur, 1)}
            else:
                log("Music: skipped")
                display.done("generate_music", "skipped", dur)
                status["stages"]["generate_music"] = {"status": "skipped"}

        # --- ASSEMBLE ---
        if "assemble" in active:
            _check_interrupted(display, status, ws, logger)
            display.start("assemble")
            ac = stage_configs.get("assemble", {})
            _log_config(log, "assemble", ac, {
                "version": 0, "edl_path": None, "quality": 1.0,
                "skip_broken": False,
            })
            t0 = time.monotonic()

            from pipeline.assemble import assemble as do_assemble
            from pipeline.edl import find_latest_version

            import json as _json

            edl_path = ac.get("edl_path")
            if edl_path:
                # Copy external EDL into run dir as next version
                import shutil
                version = find_latest_version(cfg) + 1
                dest = cfg.workspace / f"edl_v{version}.json"
                shutil.copy(edl_path, dest)
                log(f"Using EDL from {edl_path} as v{version}")
            else:
                version = ac.get("version", 0)
                if version <= 0:
                    version = find_latest_version(cfg)

            # Always read resolution/fps from EDL
            edl_file = cfg.workspace / f"edl_v{version}.json"
            edl_data = _json.loads(edl_file.read_text())
            edl_res = edl_data.get("resolution", [3840, 2160])
            edl_fps = edl_data.get("fps", 60)

            log(f"Render: {edl_res[0]}x{edl_res[1]} {edl_fps}fps (EDL v{version})")

            out, issues = do_assemble(
                cfg, version=version,
                resolution=(edl_res[0], edl_res[1]),
                fps=edl_fps,
                progress_callback=_progress_cb(logger, display, "assemble", t0),
                skip_broken=ac.get("skip_broken", False),
                quality=ac.get("quality", 1.0),
            )

            dur = time.monotonic() - t0
            size_mb = round(out.stat().st_size / 1024 / 1024, 1) if out.exists() else 0
            log(f"Assemble: {out.name} ({size_mb}MB) in {dur:.0f}s")
            display.done("assemble", f"{out.name} ({size_mb}MB)", dur)

            for issue in issues:
                level = issue.get("level", "warning")
                log(f"  [{level.upper()}] {issue.get('check', '')}: {issue.get('message', '')}")

            status["stages"]["assemble"] = {
                "status": "ok", "duration_s": round(dur, 1),
                "output": out.name, "size_mb": size_mb,
            }

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
        _write_status(ws, status)
        total = status["total_duration_s"]
        result = status.get("result", "unknown")
        logger.info(f"Pipeline {result} in {total:.0f}s")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _run_name(ctx: click.Context) -> str:
    return ctx.obj["run_name"] or "default"


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


@click.group()
@click.option("--run-name", "-n", default=None,
              help="Run name (subdirectory under workspace/runs/)")
@click.pass_context
def cli(ctx: click.Context, run_name: str | None) -> None:
    """Automated vlog pipeline: fetch \u2192 prepare \u2192 plan \u2192 generate_music \u2192 assemble."""
    ctx.ensure_object(dict)
    ctx.obj["run_name"] = run_name


@cli.command()
@click.option("-f", "--from-date", default="", help="Start date (YYYY-MM-DD)")
@click.option("-t", "--to-date", default="", help="End date (YYYY-MM-DD)")
@click.option("--source", default="", help="Local folder of photos/videos (alternative to NAS)")
@click.option("--duration", default=60, type=int, help="Target vlog length in seconds")
@click.option("--trip-type", default="family",
              type=click.Choice(["family", "solo", "food", "adventure", "architecture", "general"]))
@click.option("--style", default="upbeat",
              type=click.Choice(["upbeat", "cinematic", "reflective", "energetic"]))
@click.option("--focus", default="", help="What to emphasize (default: derived from trip-type)")
@click.option("--item-types", default=None,
              help="Media types: photo,video,live,motion (default: all)")
@click.option("--music", default="auto",
              help="auto=Gemini Lyria (default), local=MusicGen, /path/to/file, none=no music")
@click.option("--width", default=3840, type=int, help="Output width")
@click.option("--height", default=2160, type=int, help="Output height")
@click.option("--fps", default=60, type=int, help="Output FPS")
@click.option("--quality", default=1.0, type=float,
              help="Bitrate multiplier: 0.5=smaller files, 1.0=YouTube quality (default), 2.0=master quality")
@click.option("--country", default=None, help="Filter by country")
@click.option("--district", default=None, help="Filter by district/city")
@click.option("--force-prepare", is_flag=True, help="Force re-prepare (ignore cached)")
@click.option("--lang", default="en", type=click.Choice(["en", "cn", "both"]),
              help="Text language: en=English (default), cn=Chinese, both=bilingual")
@click.option("--family", default=None,
              help="Comma-separated family member names")
@click.option("--timezone", "--tz", "tz_offset", default=None, type=int,
              help="UTC offset in hours (default: system local, e.g. -5 NYC, 8 SGT)")
@click.pass_context
def full(ctx, from_date, to_date, source, duration, trip_type, style, focus,
         item_types, music, width, height, fps, quality,
         country, district, force_prepare, family, lang, tz_offset):
    """Run the full pipeline end-to-end."""
    if not source and (not from_date or not to_date):
        raise click.UsageError("Either --source (local folder) or -f/-t (date range) is required.")

    type_list = _parse_item_types(item_types) if item_types else None

    fetch_cfg: dict = {"force": True}
    if source:
        fetch_cfg["source_dir"] = source
    else:
        fetch_cfg["from_date"] = from_date
        fetch_cfg["to_date"] = to_date
    if country:
        fetch_cfg["country"] = country
    if district:
        fetch_cfg["district"] = district
    if type_list:
        fetch_cfg["item_types"] = type_list

    prepare_cfg: dict = {"force": force_prepare}
    if family:
        prepare_cfg["family_names"] = [n.strip() for n in family.split(",")]
    if tz_offset is not None:
        prepare_cfg["tz_offset"] = tz_offset

    plan_cfg: dict = {
        "style": style, "target_duration": duration,
        "focus": focus, "trip_type": trip_type,
        "language": lang,
        "width": width, "height": height, "fps": fps,
        "quality": quality,
    }
    if tz_offset is not None:
        plan_cfg["tz_offset"] = tz_offset
    music_backend = "gemini"
    if music == "local":
        plan_cfg["music_file"] = "auto"
        music_backend = "local"
    elif music == "none":
        pass
    elif music == "auto":
        plan_cfg["music_file"] = "auto"
    else:
        plan_cfg["music_file"] = music

    _run_pipeline(_run_name(ctx), {
        "fetch": fetch_cfg,
        "prepare": prepare_cfg,
        "plan": plan_cfg,
        "generate_music": {"music_backend": music_backend},
        "assemble": {"skip_broken": True, "quality": quality},
    })


@cli.command()
@click.option("--duration", default=60, type=int, help="Target vlog length in seconds")
@click.option("--trip-type", default="family",
              type=click.Choice(["family", "solo", "food", "adventure", "architecture", "general"]))
@click.option("--style", default="upbeat",
              type=click.Choice(["upbeat", "cinematic", "reflective", "energetic"]))
@click.option("--focus", default="", help="What to emphasize")
@click.option("--lang", default="en", type=click.Choice(["en", "cn", "both"]),
              help="Text language")
@click.option("--model", default=None, help="Gemini model (default: VLOG_MODEL env or gemini-3-flash-preview)")
@click.pass_context
def plan(ctx, duration, trip_type, style, focus, lang, model):
    """Re-plan and re-assemble (uses cached media + analysis)."""
    plan_cfg: dict = {"style": style, "target_duration": duration,
                      "focus": focus, "trip_type": trip_type, "language": lang,
                      "music_file": "auto"}
    if model:
        plan_cfg["model"] = model
    _run_pipeline(_run_name(ctx), {
        "plan": plan_cfg,
        "generate_music": {"music_backend": "gemini"},
    }, stages=["plan", "generate_music", "assemble"])


@cli.command()
@click.option("-v", "--version", default=None, type=int, help="EDL version to render")
@click.option("--edl", "edl_path", default=None, type=click.Path(exists=True), help="EDL JSON path (overrides version)")
@click.option("--quality", default=1.0, type=float, help="Bitrate multiplier")
@click.pass_context
def assemble(ctx, version, edl_path, quality):
    """Re-render the vlog from current or specified EDL version."""
    ac: dict = {"quality": quality}
    if version is not None:
        ac["version"] = version
    if edl_path is not None:
        ac["edl_path"] = edl_path
    _run_pipeline(_run_name(ctx), {"assemble": ac}, stages=["assemble"])


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
            info["n_videos"] = sum(
                1 for s in segs for i in s.get("items", []) if i.get("media_type") == "video"
            )
            info["n_keep_audio"] = sum(
                1 for s in segs for i in s.get("items", []) if i.get("keep_audio")
            )
        except Exception:
            pass

    output_dir = run_dir / "output"
    outputs = sorted(output_dir.glob("vlog_v*.mp4")) if output_dir.exists() else []
    info["outputs"] = [
        {"path": o, "version": int(o.stem.split("_v")[1]), "size": o.stat().st_size}
        for o in outputs
    ]
    info["old_output_bytes"] = sum(o["size"] for o in info["outputs"][:-1]) if len(info["outputs"]) > 1 else 0

    intermediates = (
        list(output_dir.glob("*_nomix.mp4")) +
        list(output_dir.glob("*_speech.wav")) +
        list(output_dir.glob("concat_list.txt"))
    ) if output_dir.exists() else []
    info["intermediate_bytes"] = sum(f.stat().st_size for f in intermediates)
    info["intermediate_files"] = intermediates

    clips_dir = run_dir / "clips"
    legacy = list(clips_dir.glob("*_txt.mp4")) if clips_dir.exists() else []
    info["legacy_txt_bytes"] = sum(f.stat().st_size for f in legacy)
    info["legacy_txt_files"] = legacy

    clips_size, clips_count = _dir_size(clips_dir)
    info["clips_size"] = clips_size
    info["clips_count"] = clips_count - len(legacy)

    cs_size, _ = _dir_size(run_dir / "contact_sheets")
    info["contact_sheets_size"] = cs_size

    return info


@cli.command()
@click.option("--clean", type=click.Choice(["media", "cache", "runs", "all"]),
              default=None, help="Delete shared data")
@click.option("--prune", is_flag=True, help="Remove old output versions, intermediates, and legacy clips")
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
def workspace(clean, prune, yes):
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
        ("contact_sheets", "Contact sheets (shared)", ws / "contact_sheets"),
        ("keyframes", "Video keyframe cache", ws / "keyframes"),
        ("analysis_cache", "Analysis cache", ws / "analysis_cache"),
        ("thumbnails", "Photo thumbnails", ws / "thumbnails"),
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
            click.echo(f"    Output: (none)")

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
        click.echo(f"Reclaimable with --prune: {_fmt_size(total_reclaimable)}")

    if prune:
        if total_reclaimable == 0:
            click.echo("\nNothing to prune.")
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
        click.echo(f"Pruned {len(to_delete)} files, freed {_fmt_size(freed)}.")
        return

    if clean is None:
        return

    targets = []
    if clean in ("media", "all"):
        targets.append(("media", ws / "media"))
    if clean in ("cache", "all"):
        targets += [("analysis_cache", ws / "analysis_cache"),
                    ("thumbnails", ws / "thumbnails"),
                    ("keyframes", ws / "keyframes"),
                    ("music", ws / "music")]
    if clean in ("runs", "all"):
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
