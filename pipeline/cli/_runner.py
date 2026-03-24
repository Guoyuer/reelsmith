"""Pipeline stage runners and orchestration."""

from __future__ import annotations

import json
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ._display import (
    _PipelineDisplay,
    _build_headline_from_args,
    _progress_cb,
    _setup_logging,
)

if TYPE_CHECKING:
    import logging

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
    global _interrupted
    if _interrupted:
        logger.info("Pipeline interrupted by user")
        display.stop()
        sys.exit(130)


@dataclass
class _PipelineContext:
    cfg: Config
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


# ---------------------------------------------------------------------------
# Stage runners
# ---------------------------------------------------------------------------


def _run_fetch(pc: _PipelineContext):
    assert pc.fetch is not None and pc.display is not None
    _check_interrupted(pc.display, pc.logger)
    pc.display.start("fetch")
    t0 = time.monotonic()
    manifest_path = pc.cfg.manifest_path

    if manifest_path.exists():
        items = json.loads(manifest_path.read_text())
        dur = time.monotonic() - t0
        pc.log(f"Fetch: {len(items)} items (cached)")
        pc.display.done("fetch", f"{len(items)} items", dur)
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
    dur = time.monotonic() - t0

    results = load_analysis(pc.cfg)
    n_photos = sum(1 for r in results if r.get("media_type") == "photo")
    n_videos = len(results) - n_photos
    pc.log(
        f"Prepare: {len(results)} items ({n_photos} photos, {n_videos} videos) in {dur:.0f}s"
    )
    pc.display.done("prepare", f"{n_photos} photos, {n_videos} videos", dur)


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

    all_items = edl.all_items()
    n_videos = sum(1 for i in all_items if i.media_type == "video")
    n_photos = len(all_items) - n_videos
    n_keep_audio = sum(1 for i in all_items if i.keep_audio)
    vid_time = sum(i.display_duration for i in all_items if i.media_type == "video")
    total_time = sum(i.display_duration for i in all_items)
    vid_pct = int(vid_time / total_time * 100) if total_time > 0 else 0

    dur = time.monotonic() - t0
    plan_detail = (
        f"v{version}: {n_photos}p+{n_videos}v, " f"~{edl.estimated_duration():.0f}s"
    )
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

    dur = time.monotonic() - t0
    if track:
        pc.log(f"Music: generated {track.name} in {dur:.0f}s")
        pc.display.done("generate_music", track.name, dur)
    else:
        pc.log("Music: skipped")
        pc.display.done("generate_music", "skipped", dur)


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

    pc.log(f"Render: {ac.w}x{ac.h} {ac.fps}fps (EDL v{ac.version})")

    out, issues = do_assemble(
        pc.cfg,
        ac,
        progress_callback=_progress_cb(pc.logger, pc.display, "assemble", t0),
    )

    dur = time.monotonic() - t0
    size_mb = round(out.stat().st_size / 1024 / 1024, 1) if out.exists() else 0
    pc.log(f"Assemble: {out.name} ({size_mb}MB) in {dur:.0f}s")
    pc.display.output_file = str(out)
    pc.display.done("assemble", f"{out.name} ({size_mb}MB)", dur)

    for issue in issues:
        level = issue.get("level", "warning")
        pc.log(
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
):
    """Execute pipeline stages directly in this process."""
    global _interrupted
    _interrupted = False

    from pipeline.config import Config

    active = stages
    ws_path = Config.run_workspace(run_name=run_name)
    cfg = Config.load(ws_path)
    cfg.ensure_dirs()

    # Create display first (starts live panel), then logging (uses its console)
    headline = _build_headline_from_args(active, plan)
    display = _PipelineDisplay(run_name, headline, active)
    logger = _setup_logging(run_name, display)

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

    try:
        for stage in active:
            if stage in _STAGE_RUNNERS:
                _STAGE_RUNNERS[stage](pc)

        total = round(time.monotonic() - t_start, 1)
        logger.info(f"Pipeline success in {total:.0f}s")

    except SystemExit:
        raise
    except Exception as e:
        total = round(time.monotonic() - t_start, 1)
        display.fail("pipeline", str(e)[:80])
        logger.error(f"Pipeline failed in {total:.0f}s: {e}", exc_info=True)
        sys.exit(1)
    finally:
        display.stop()
        display.print_summary()
