"""Pipeline stage runners and orchestration."""

from __future__ import annotations

import json
import signal
import sys
import time
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
# Stage runners
# ---------------------------------------------------------------------------


def _run_fetch(pc: _PipelineContext):
    assert pc.fetch is not None and pc.display is not None
    _check_interrupted(pc.display, pc.logger)
    pc.display.start("fetch")
    t0 = time.monotonic()
    manifest_path = pc.cfg.manifest_path

    force = pc.prepare is not None and pc.prepare.force
    if manifest_path.exists() and not force:
        items = json.loads(manifest_path.read_text())
        elapsed = time.monotonic() - t0
        pc.logger.info(f"Fetch: {len(items)} items (cached)")
        pc.display.done("fetch", f"{len(items)} items", elapsed)
    else:
        cb = _progress_cb(pc.logger, pc.display, "fetch", t0)
        from pipeline.fetch import fetch_local

        items = fetch_local(pc.cfg, pc.fetch, progress_callback=cb)
        elapsed = time.monotonic() - t0
        pc.logger.info(f"Fetch: {len(items)} items in {elapsed:.0f}s")
        pc.display.done("fetch", f"{len(items)} items", elapsed)


def _run_prepare(pc: _PipelineContext):
    assert pc.prepare is not None and pc.display is not None
    _check_interrupted(pc.display, pc.logger)
    pc.display.start("prepare")
    t0 = time.monotonic()

    from pipeline.prepare import load_analysis, prepare

    prepare(
        pc.cfg,
        pc.prepare,
        progress_callback=_progress_cb(pc.logger, pc.display, "prepare", t0),
    )
    elapsed = time.monotonic() - t0

    results = load_analysis(pc.cfg)
    n_photos = sum(1 for r in results if r.get("media_type") == "photo")
    n_videos = len(results) - n_photos
    pc.display.done("prepare", f"{n_photos} photos, {n_videos} videos", elapsed)


def _run_plan(pc: _PipelineContext):
    """Execute the plan stage."""
    assert pc.plan is not None and pc.display is not None
    _check_interrupted(pc.display, pc.logger)
    pc.display.start("plan")
    t0 = time.monotonic()

    from pipeline.plan import plan as do_plan

    edl, version = do_plan(
        pc.cfg,
        pc.plan,
        progress_callback=_progress_cb(pc.logger, pc.display, "plan", t0),
    )

    s = edl.summary()
    elapsed = time.monotonic() - t0

    plan_detail = (
        f"v{version}: {s['n_photos']}p({s['photo_time']:.0f}s)"
        f"+{s['n_videos']}v({s['vid_time']:.0f}s), "
        f"~{s['estimated_duration']:.0f}s"
    )
    pc.display.done("plan", plan_detail, elapsed)

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
    """Execute the generate_music stage."""
    assert pc.display is not None
    _check_interrupted(pc.display, pc.logger)
    pc.display.start("generate_music")
    t0 = time.monotonic()

    from pipeline.music import generate_music_for_edl

    track = generate_music_for_edl(
        pc.cfg,
        progress_callback=_progress_cb(pc.logger, pc.display, "generate_music", t0),
    )

    elapsed = time.monotonic() - t0
    if track:
        pc.logger.info(f"Music: generated {track.name} in {elapsed:.0f}s")
        pc.display.done("generate_music", track.name, elapsed)
    else:
        pc.logger.info("Music: skipped")
        pc.display.done("generate_music", "skipped", elapsed)


def _run_assemble(pc: _PipelineContext):
    assert pc.assemble is not None and pc.display is not None
    _check_interrupted(pc.display, pc.logger)
    pc.display.start("assemble")
    t0 = time.monotonic()
    ac = pc.assemble

    from pipeline.assemble import assemble as do_assemble
    from pipeline.edl import find_latest_version

    if not ac.version:
        from dataclasses import replace

        ac = replace(ac, version=find_latest_version(pc.cfg))

    pc.logger.info(f"Render: {ac.w}x{ac.h} {ac.fps}fps (EDL v{ac.version})")

    out, issues = do_assemble(
        pc.cfg,
        ac,
        progress_callback=_progress_cb(pc.logger, pc.display, "assemble", t0),
    )

    elapsed = time.monotonic() - t0
    size_mb = round(out.stat().st_size / 1024 / 1024, 1) if out.exists() else 0
    pc.logger.info(f"Assemble: {out.name} ({size_mb}MB) in {elapsed:.0f}s")
    pc.display.output_file = str(out)
    pc.display.done("assemble", f"{out.name} ({size_mb}MB)", elapsed)

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
