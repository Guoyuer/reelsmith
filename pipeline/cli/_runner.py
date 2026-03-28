"""Pipeline stage runners and orchestration."""

from __future__ import annotations

import json
import signal
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ._display import (
    _build_headline_from_args,
    _PipelineDisplay,
    _progress_cb,
    _setup_logging,
)

if TYPE_CHECKING:
    import logging

    from pipeline.assemble import AssembleConfig
    from pipeline.config import Config
    from pipeline.plan import PlanConfig
    from pipeline.prepare import PrepareConfig

# ---------------------------------------------------------------------------
# SIGINT handler — first Ctrl+C sets flag, second force-quits
# ---------------------------------------------------------------------------

_interrupted = False


def _handle_sigint(sig, frame):
    global _interrupted
    if _interrupted:
        # Second Ctrl+C — force kill immediately
        import os

        os._exit(1)
    _interrupted = True
    from pipeline.utils.media import set_interrupted

    set_interrupted()
    print("\n\u26a0 Interrupted \u2014 press Ctrl+C again to force quit")


signal.signal(signal.SIGINT, _handle_sigint)


# ---------------------------------------------------------------------------
# Pipeline context
# ---------------------------------------------------------------------------


def _check_interrupted(display: _PipelineDisplay, logger: logging.Logger):
    """Check if Ctrl+C was pressed between stages. If so, exit."""
    if _interrupted:
        logger.info("Pipeline interrupted by user")
        display.stop()
        sys.exit(130)


@dataclass
class _PipelineContext:
    cfg: Config
    logger: logging.Logger
    display: _PipelineDisplay | None = None
    fetch: str | None = None
    prepare: PrepareConfig | None = None
    plan: PlanConfig | None = None
    assemble: AssembleConfig | None = None


# ---------------------------------------------------------------------------
# Stage runner boilerplate
# ---------------------------------------------------------------------------


@contextmanager
def _stage(pc: _PipelineContext, name: str):
    """Shared stage boilerplate: interrupt check, display start, timing, progress callback."""
    assert pc.display is not None
    display = pc.display
    _check_interrupted(display, pc.logger)
    display.start(name)
    t0 = time.monotonic()
    cb = _progress_cb(pc.logger, display, name, t0)

    def done(detail: str) -> float:
        elapsed = time.monotonic() - t0
        display.done(name, detail, elapsed)
        return elapsed

    yield cb, done


# ---------------------------------------------------------------------------
# Stage runners
# ---------------------------------------------------------------------------


def _run_fetch(pc: _PipelineContext):
    assert pc.fetch is not None
    with _stage(pc, "fetch") as (cb, done):
        manifest_path = pc.cfg.manifest_path
        force = pc.prepare is not None and pc.prepare.force
        if manifest_path.exists() and not force:
            items = json.loads(manifest_path.read_text())
            pc.logger.info(f"Fetch: {len(items)} items (cached)")
            done(f"{len(items)} items")
        else:
            from pipeline.fetch import fetch_local

            items = fetch_local(pc.cfg, pc.fetch, progress_callback=cb)
            elapsed = done(f"{len(items)} items")
            pc.logger.info(f"Fetch: {len(items)} items in {elapsed:.0f}s")


def _run_prepare(pc: _PipelineContext):
    assert pc.prepare is not None
    with _stage(pc, "prepare") as (cb, done):
        from pipeline.prepare import load_analysis, prepare

        prepare(pc.cfg, pc.prepare, progress_callback=cb)
        results = load_analysis(pc.cfg)
        n_photos = sum(1 for r in results if r.get("media_type") == "photo")
        n_videos = len(results) - n_photos
        done(f"{n_photos} photos, {n_videos} videos")


def _run_plan(pc: _PipelineContext):
    assert pc.plan is not None
    with _stage(pc, "plan") as (cb, done):
        from pipeline.plan import plan as do_plan

        edl, version = do_plan(pc.cfg, pc.plan, progress_callback=cb)
        s = edl.summary()

        plan_detail = (
            f"v{version}: {s['n_photos']}p({s['photo_time']:.0f}s)"
            f"+{s['n_videos']}v({s['vid_time']:.0f}s), "
            f"~{s['estimated_duration']:.0f}s"
        )
        elapsed = done(plan_detail)

        pc.logger.info(
            f"Plan: EDL v{version} \u2014 {len(edl.segments)} segments, "
            f"{s['n_photos']} photos + {s['n_videos']} videos ({s['vid_pct']}% video), "
            f"duration ~{s['estimated_duration']:.0f}s (target {edl.target_duration:.0f}s), planned in {elapsed:.0f}s"
        )
        if s["n_keep_audio"]:
            pc.logger.info(f"  Speech preserved: {s['n_keep_audio']} clips")
        for seg in edl.segments:
            pc.logger.info(
                f"  {seg.name}: {len(seg.items)} items, transition={seg.transition}"
            )


def _run_generate_music(pc: _PipelineContext):
    with _stage(pc, "generate_music") as (cb, done):
        from pipeline.music import generate_music_for_edl

        track = generate_music_for_edl(pc.cfg, progress_callback=cb)
        if track:
            elapsed = done(track.name)
            pc.logger.info(f"Music: generated {track.name} in {elapsed:.0f}s")
        else:
            pc.logger.info("Music: skipped")
            done("skipped")


def _run_assemble(pc: _PipelineContext):
    assert pc.assemble is not None
    with _stage(pc, "assemble") as (cb, done):
        from pipeline.assemble import assemble as do_assemble
        from pipeline.edl import find_latest_version

        ac = pc.assemble
        if not ac.version:
            from dataclasses import replace

            ac = replace(ac, version=find_latest_version(pc.cfg))

        pc.logger.info(f"Render: {ac.w}x{ac.h} {ac.fps}fps (EDL v{ac.version})")
        out, issues = do_assemble(pc.cfg, ac, progress_callback=cb)

        size_mb = round(out.stat().st_size / 1024 / 1024, 1) if out.exists() else 0
        assert pc.display is not None
        pc.display.output_file = str(out)
        elapsed = done(f"{out.name} ({size_mb}MB)")
        pc.logger.info(f"Assemble: {out.name} ({size_mb}MB) in {elapsed:.0f}s")

        for issue in issues:
            level = issue.get("level", "warning")
            pc.logger.info(
                f"  [{level.upper()}] {issue.get('check', '')}: {issue.get('message', '')}"
            )


_STAGE_RUNNERS = {
    "fetch": _run_fetch,
    "prepare": _run_prepare,
    "plan": _run_plan,
    "generate_music": _run_generate_music,
    "assemble": _run_assemble,
}


# ---------------------------------------------------------------------------
# Pipeline orchestrator
# ---------------------------------------------------------------------------


def _run_pipeline(
    run_name: str,
    *,
    stages: list[str],
    fetch=None,
    prepare=None,
    plan=None,
    assemble=None,
    cli_params: dict | None = None,
    cli_defaults: set[str] | None = None,
):
    """Execute pipeline stages directly in this process."""
    global _interrupted
    _interrupted = False
    import pipeline.utils.media

    pipeline.utils.media._interrupted = False

    from pipeline.config import Config

    active = stages
    ws_path = Config.run_workspace(run_name=run_name)
    cfg = Config.load(ws_path)
    cfg.ensure_dirs()

    # Persist CLI parameters early (before any stage runs) so that even
    # killed / failed runs have their config saved for later --use-cfg-file.
    if cli_params:
        from ._config_io import save_run_config

        save_run_config(cfg.workspace, cli_params, defaults=cli_defaults)

    # Create display first (starts live panel), then logging (uses its console)
    headline = _build_headline_from_args(active, plan)
    display = _PipelineDisplay(run_name, headline, active)
    logger = _setup_logging(run_name, display)

    # Log CLI parameters for reproducibility
    logger.info("Run: %s | stages: %s", run_name, ", ".join(active))
    if plan:
        logger.info(
            "  Plan: duration=%ds, trip_type=%s, style=%s, lang=%s, model=%s, "
            "thinking=%s, focus=%r",
            plan.target_duration,
            plan.trip_type,
            plan.style,
            plan.language,
            plan.model,
            plan.thinking_level,
            plan.focus,
        )
    if assemble:
        logger.info(
            "  Assemble: %dx%d@%d quality=%s",
            assemble.w,
            assemble.h,
            assemble.fps,
            assemble.quality,
        )
    if prepare:
        logger.info("  Prepare: force=%s", prepare.force)
    if fetch:
        src = fetch
        logger.info("  Fetch: source=%s", src)

    pc = _PipelineContext(
        cfg=cfg,
        logger=logger,
        fetch=fetch,
        prepare=prepare,
        plan=plan,
        assemble=assemble,
    )
    pc.display = display
    t_start = time.monotonic()

    current_stage = None
    try:
        for stage in active:
            if stage in _STAGE_RUNNERS:
                current_stage = stage
                _STAGE_RUNNERS[stage](pc)

        total = round(time.monotonic() - t_start, 1)
        logger.info("Pipeline success in %.0fs", total)

    except SystemExit:
        raise
    except Exception as e:
        total = round(time.monotonic() - t_start, 1)
        if current_stage:
            display.fail(current_stage, str(e)[:80])
        logger.error("Pipeline failed in %.0fs: %s", total, e, exc_info=True)
        sys.exit(1)
    finally:
        display.stop()
        display.print_summary()
