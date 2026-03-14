"""Prefect 3.x workflow definitions wrapping the vlog pipeline stages."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from prefect import flow, task, get_run_logger
from prefect.artifacts import (
    create_progress_artifact,
    update_progress_artifact,
    create_markdown_artifact,
)
from prefect.exceptions import MissingContextError

from .config import Config

_fallback_logger = logging.getLogger("pipeline.flows")


def _logger():
    """Get Prefect run logger, or fall back to stdlib logger (for tests / direct calls)."""
    try:
        return get_run_logger()
    except MissingContextError:
        return _fallback_logger


# ---------------------------------------------------------------------------
# Task wrappers — one per pipeline stage
# ---------------------------------------------------------------------------

@task(
    name="fetch-media",
    retries=2,
    retry_delay_seconds=30,
    log_prints=True,
)
def fetch_task(
    workspace: str,
    *,
    from_date: str | None = None,
    to_date: str | None = None,
    country: str | None = None,
    first_level: str | None = None,
    district: str | None = None,
    person_ids: list[int] | None = None,
    item_types: list[int] | None = None,
) -> str:
    """Download media from Synology Photos. Returns path to manifest.json."""
    from .fetch import fetch as do_fetch

    cfg = Config.load(workspace)
    manifest = do_fetch(
        cfg,
        from_date=from_date, to_date=to_date,
        country=country, first_level=first_level, district=district,
        person_ids=person_ids, item_types=item_types,
    )

    manifest_path = str(cfg.workspace / "manifest.json")
    logger = _logger()
    logger.info(f"Fetched {len(manifest)} items -> {manifest_path}")

    create_markdown_artifact(
        key="fetch-summary",
        markdown=(
            f"## Fetch Complete\n\n"
            f"- **Items downloaded:** {len(manifest)}\n"
            f"- **Manifest:** `{manifest_path}`"
        ),
    )

    return manifest_path


@task(
    name="preprocess",
    retries=1,
    retry_delay_seconds=5,
    log_prints=True,
)
def preprocess_task(
    workspace: str,
    manifest_path: str,
    *,
    family_names: list[str] | None = None,
) -> str:
    """Tier items, cluster duplicates, build timeline. Returns preprocessed.json path."""
    from .preprocess import preprocess as do_preprocess

    cfg = Config.load(workspace)
    result = do_preprocess(cfg, family_names=family_names)

    preprocessed_path = str(cfg.workspace / "preprocessed.json")
    logger = _logger()
    logger.info(
        f"Preprocessed: {result['total_items']} -> {result['selected_items']} unique moments"
    )

    create_markdown_artifact(
        key="preprocess-summary",
        markdown=(
            f"## Preprocess Complete\n\n"
            f"- **Total items:** {result['total_items']}\n"
            f"- **Selected:** {result['selected_items']}\n"
            f"- **Tiers:** {result['tier_counts']}\n"
            f"- **Timeline days:** {len(result['timeline'])}"
        ),
    )

    return preprocessed_path


@task(
    name="analyze-media",
    retries=1,
    retry_delay_seconds=10,
    log_prints=True,
    timeout_seconds=7200,
)
def analyze_task(workspace: str, preprocessed_path: str) -> str:
    """Analyze media with vision model. Returns analysis.json path."""
    from .analyze import analyze as do_analyze

    cfg = Config.load(workspace)

    artifact_id = create_progress_artifact(
        progress=0.0,
        description="Starting vision analysis...",
    )
    last_pct = 0.0

    def on_progress(current: int, total: int, filename: str) -> None:
        nonlocal last_pct
        pct = (current / total) * 100 if total else 0
        if pct - last_pct >= 2.0 or current == total:
            update_progress_artifact(
                artifact_id=artifact_id,
                progress=pct,
                description=f"Analyzing {current}/{total}: {filename}",
            )
            last_pct = pct

    results = do_analyze(cfg, progress_callback=on_progress)

    update_progress_artifact(
        artifact_id=artifact_id,
        progress=100.0,
        description=f"Analysis complete: {len(results)} items",
    )

    analysis_path = str(cfg.workspace / "analysis.json")
    logger = _logger()
    ok = sum(1 for r in results if r.get("vision"))
    logger.info(f"Analysis complete: {ok}/{len(results)} with vision results")

    create_markdown_artifact(
        key="analyze-summary",
        markdown=(
            f"## Analysis Complete\n\n"
            f"- **Items analyzed:** {len(results)}\n"
            f"- **With vision results:** {ok}"
        ),
    )

    return analysis_path


@task(
    name="plan-edl",
    retries=2,
    retry_delay_seconds=15,
    log_prints=True,
)
def plan_task(
    workspace: str,
    preprocessed_path: str,
    analysis_path: str,
    *,
    style: str = "upbeat",
    target_duration: int = 180,
    focus: str = "happiness with family",
) -> str:
    """Generate EDL using local LLM. Returns edl.json path."""
    from .plan import plan as do_plan

    cfg = Config.load(workspace)
    edl = do_plan(cfg, style=style, target_duration=target_duration, focus=focus)

    edl_path = str(cfg.workspace / "edl.json")
    logger = _logger()
    logger.info(f"EDL: {len(edl.segments)} segments, ~{edl.estimated_duration():.0f}s")

    create_markdown_artifact(
        key="plan-summary",
        markdown=(
            f"## EDL Planned\n\n"
            f"- **Title:** {edl.title}\n"
            f"- **Segments:** {len(edl.segments)}\n"
            f"- **Items:** {len(edl.all_items())}\n"
            f"- **Estimated duration:** {edl.estimated_duration():.0f}s"
        ),
    )

    return edl_path


@task(
    name="assemble-video",
    retries=1,
    retry_delay_seconds=30,
    log_prints=True,
    timeout_seconds=3600,
)
def assemble_task(workspace: str, edl_path: str, *, version: int = 1) -> str:
    """Render vlog from EDL. Returns path to output video."""
    from .assemble import assemble as do_assemble

    cfg = Config.load(workspace)

    artifact_id = create_progress_artifact(
        progress=0.0,
        description="Rendering clips...",
    )
    last_pct = 0.0

    def on_progress(current: int, total: int, clip_name: str) -> None:
        nonlocal last_pct
        pct = (current / total) * 100 if total else 0
        if pct - last_pct >= 3.0 or current == total:
            update_progress_artifact(
                artifact_id=artifact_id,
                progress=pct,
                description=f"Rendering {current}/{total}: {clip_name}",
            )
            last_pct = pct

    output_path = do_assemble(cfg, version=version, progress_callback=on_progress)

    update_progress_artifact(
        artifact_id=artifact_id,
        progress=100.0,
        description=f"Assembly complete: {output_path.name}",
    )

    logger = _logger()
    logger.info(f"Assembled: {output_path}")

    create_markdown_artifact(
        key="assemble-summary",
        markdown=(
            f"## Video Assembled\n\n"
            f"- **Output:** `{output_path}`\n"
            f"- **Version:** {version}"
        ),
    )

    return str(output_path)


@task(
    name="self-critique-round",
    retries=1,
    retry_delay_seconds=15,
    log_prints=True,
    task_run_name="critique-round-{round_num}",
)
def critique_round_task(
    workspace: str,
    *,
    round_num: int,
    style: str = "upbeat",
) -> str:
    """One self-critique round: extract frames -> vision critique -> revise EDL -> re-assemble."""
    from .assemble import assemble as do_assemble
    from .edl import EDL
    from .iterate import (
        CRITIQUE_PROMPT,
        _extract_review_frames,
        _find_latest_version,
        _save_edl_version,
    )
    from .llm import ollama_chat

    cfg = Config.load(workspace)
    edl_path = cfg.workspace / "edl.json"
    edl = EDL.model_validate_json(edl_path.read_text())

    current_version = _find_latest_version(cfg)
    current_video = cfg.workspace / "output" / f"vlog_v{current_version}.mp4"

    if not current_video.exists():
        print(f"  No video at {current_video}, assembling first...")
        do_assemble(cfg, version=current_version)

    frames = _extract_review_frames(
        current_video, cfg.workspace / "review_frames", count=8,
    )

    prompt = CRITIQUE_PROMPT.format(
        style=style,
        edl_json=edl.model_dump_json(indent=2),
    )

    content = ollama_chat(cfg, model=cfg.vision_model, prompt=prompt, images=frames)
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1].rsplit("```", 1)[0]

    logger = _logger()
    try:
        edl = EDL.model_validate_json(content)
    except Exception as e:
        logger.warning(f"Failed to parse critique EDL: {e}")
        return str(current_video)

    new_version = current_version + 1
    edl_path.write_text(edl.model_dump_json(indent=2))
    _save_edl_version(cfg, edl, new_version)

    # Clear old clips to force re-render with revised EDL
    clips_dir = cfg.workspace / "clips"
    if clips_dir.exists():
        for f in clips_dir.iterdir():
            f.unlink(missing_ok=True)

    output = do_assemble(cfg, version=new_version)
    logger.info(f"Critique round {round_num}: v{current_version} -> v{new_version}")

    create_markdown_artifact(
        key=f"critique-round-{round_num}",
        markdown=(
            f"## Critique Round {round_num}\n\n"
            f"- **Input:** v{current_version}\n"
            f"- **Output:** v{new_version}\n"
            f"- **Segments:** {len(edl.segments)}\n"
            f"- **Items:** {len(edl.all_items())}"
        ),
    )

    return str(output)


@task(
    name="apply-feedback",
    retries=1,
    retry_delay_seconds=10,
    log_prints=True,
)
def apply_feedback_task(workspace: str, feedback: str) -> str:
    """Apply human feedback to EDL and re-render. Returns new video path."""
    from .iterate import apply_feedback as do_apply_feedback, _find_latest_version

    cfg = Config.load(workspace)
    do_apply_feedback(cfg, feedback)

    version = _find_latest_version(cfg)
    video_path = str(cfg.workspace / "output" / f"vlog_v{version}.mp4")

    logger = _logger()
    logger.info(f"Feedback applied, rendered v{version}")

    create_markdown_artifact(
        key="feedback-result",
        markdown=(
            f"## Feedback Applied\n\n"
            f"- **Feedback:** {feedback[:200]}\n"
            f"- **Output:** `{video_path}`"
        ),
    )

    return video_path


@task(
    name="render-variation",
    retries=1,
    log_prints=True,
    task_run_name="variation-{style}",
)
def variation_task(workspace: str, *, style: str) -> str:
    """Generate and render one style variation."""
    from .iterate import generate_variations

    cfg = Config.load(workspace)
    outputs = generate_variations(cfg, styles=[style])

    if outputs:
        path = str(outputs[0])
        logger = _logger()
        logger.info(f"Variation '{style}': {path}")
        return path
    return ""


# ---------------------------------------------------------------------------
# Stage ordering and skip logic
# ---------------------------------------------------------------------------

STAGES = ["fetch", "preprocess", "analyze", "plan", "assemble", "iterate"]


def _stage_index(name: str) -> int:
    return STAGES.index(name)


def _output_exists(workspace: str, stage: str) -> bool:
    """Check if a stage's output artifact already exists."""
    ws = Path(workspace)
    checks = {
        "fetch": ws / "manifest.json",
        "preprocess": ws / "preprocessed.json",
        "analyze": ws / "analysis.json",
        "plan": ws / "edl.json",
        "assemble": None,  # checked by looking for any vlog_v*.mp4
    }
    if stage == "assemble":
        return any((ws / "output").glob("vlog_v*.mp4")) if (ws / "output").exists() else False
    path = checks.get(stage)
    return path.exists() if path else False


# ---------------------------------------------------------------------------
# Flow definitions
# ---------------------------------------------------------------------------

@flow(name="vlog-pipeline", log_prints=True)
def vlog_pipeline_flow(
    workspace: str,
    *,
    start_from: str = "fetch",
    from_date: str | None = None,
    to_date: str | None = None,
    country: str | None = None,
    first_level: str | None = None,
    district: str | None = None,
    person_ids: list[int] | None = None,
    item_types: list[int] | None = None,
    family_names: list[str] | None = None,
    style: str = "upbeat",
    target_duration: int = 180,
    focus: str = "happiness with family",
    critique_rounds: int = 2,
    assemble_version: int | None = None,
) -> str:
    """Unified vlog pipeline. Stages before start_from are skipped (their
    outputs are assumed to exist). Stages from start_from onward all run."""
    logger = _logger()
    start = _stage_index(start_from)
    ws = Path(workspace)

    def should_run(stage: str) -> bool:
        return _stage_index(stage) >= start

    # --- Fetch ---
    if should_run("fetch"):
        if not from_date or not to_date:
            raise ValueError("fetch requires --from-date and --to-date")
        manifest_path = fetch_task(
            workspace,
            from_date=from_date, to_date=to_date,
            country=country, first_level=first_level, district=district,
            person_ids=person_ids, item_types=item_types,
        )
    else:
        manifest_path = str(ws / "manifest.json")

    # --- Preprocess ---
    if should_run("preprocess"):
        preprocessed_path = preprocess_task(
            workspace, manifest_path, family_names=family_names,
        )
    else:
        preprocessed_path = str(ws / "preprocessed.json")

    # --- Analyze ---
    if should_run("analyze"):
        analysis_path = analyze_task(workspace, preprocessed_path)
    else:
        analysis_path = str(ws / "analysis.json")

    # --- Plan ---
    if should_run("plan"):
        edl_path = plan_task(
            workspace, preprocessed_path, analysis_path,
            style=style, target_duration=target_duration, focus=focus,
        )
    else:
        edl_path = str(ws / "edl.json")

    # --- Assemble ---
    if should_run("assemble"):
        version = assemble_version or 1
        video_path = assemble_task(workspace, edl_path, version=version)
    else:
        from .iterate import _find_latest_version
        cfg = Config.load(workspace)
        v = _find_latest_version(cfg)
        video_path = str(ws / "output" / f"vlog_v{v}.mp4")

    # --- Iterate ---
    if should_run("iterate") and critique_rounds > 0:
        video_path = iterate_flow(
            workspace, style=style, max_rounds=critique_rounds,
        )

    return video_path


@flow(name="iterate-critique", log_prints=True)
def iterate_flow(
    workspace: str,
    *,
    style: str = "upbeat",
    max_rounds: int = 2,
) -> str:
    """Self-critique loop. Each round is a distinct task run visible in the UI."""
    from .iterate import _find_latest_version

    cfg = Config.load(workspace)
    current_version = _find_latest_version(cfg)
    video_path = str(cfg.workspace / "output" / f"vlog_v{current_version}.mp4")

    logger = _logger()
    for round_num in range(1, max_rounds + 1):
        logger.info(f"Starting critique round {round_num}/{max_rounds}")
        video_path = critique_round_task(
            workspace, round_num=round_num, style=style,
        )

    return video_path


@flow(name="apply-feedback", log_prints=True)
def feedback_flow(workspace: str, feedback: str) -> str:
    """Apply human feedback and re-render."""
    return apply_feedback_task(workspace, feedback)


@flow(name="generate-variations", log_prints=True)
def variations_flow(workspace: str, styles: list[str]) -> list[str]:
    """Generate vlog variations, one task per style."""
    results = []
    for style in styles:
        path = variation_task(workspace, style=style)
        results.append(path)
    return results
