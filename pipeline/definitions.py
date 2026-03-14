"""Dagster asset definitions for the vlog pipeline.

Each pipeline stage is a Dagster asset. Assets auto-skip when their output
file already exists (unless force=True in config). Re-materialize any asset
from the Dagster UI to force re-run it + all downstream dependencies.

Each run lives in its own subdirectory under the base workspace:
  workspace/{run_name}/manifest.json, analysis.json, output/vlog_v1.mp4, ...

Set run_name in the IOManager config (Launchpad or CLI -w) to isolate runs.
"""

import time
from pathlib import Path

import dagster as dg

from .config import Config


# ---------------------------------------------------------------------------
# IOManager — workspace-aware file path passing
# ---------------------------------------------------------------------------

OUTPUT_FILES: dict[str, str | None] = {
    "manifest": "manifest.json",
    "preprocessed": "preprocessed.json",
    "analysis": "analysis.json",
    "edl": "edl.json",
    "vlog_video": None,
}


class WorkspaceIOManager(dg.ConfigurableIOManager):
    """IOManager for the vlog pipeline.

    - base_dir: root directory containing all runs (default: ./workspace)
    - run_name: subdirectory for this run (default: "default")

    Actual workspace path = {base_dir}/{run_name}

    Set these in the Launchpad to target different runs:
      resources > io_manager > base_dir / run_name
    """

    base_dir: str = "./workspace"
    run_name: str = "default"

    @property
    def workspace_path(self) -> str:
        return Config.run_workspace(self.base_dir, self.run_name)

    def handle_output(self, context: dg.OutputContext, obj: object) -> None:
        pass  # stages write their own files

    def load_input(self, context: dg.InputContext) -> str:
        key = context.upstream_output.asset_key.path[-1]
        filename = OUTPUT_FILES.get(key)
        if filename:
            path = Path(self.workspace_path) / filename
            if path.exists():
                return str(path)
        raise FileNotFoundError(
            f"Expected output for '{key}' at {self.workspace_path}/{filename}"
        )


def _output_exists(workspace: str, asset_name: str) -> bool:
    """Check if an asset's output file exists."""
    ws = Path(workspace)
    if asset_name == "vlog_video":
        output_dir = ws / "output"
        return output_dir.exists() and any(output_dir.glob("vlog_v*.mp4"))
    filename = OUTPUT_FILES.get(asset_name)
    if filename:
        return (ws / filename).exists()
    return False


# ---------------------------------------------------------------------------
# Config classes
# ---------------------------------------------------------------------------

class FetchConfig(dg.Config):
    from_date: str = ""
    to_date: str = ""
    country: str | None = None
    first_level: str | None = None
    district: str | None = None
    person_ids: list[int] | None = None
    item_types: list[int] | None = None
    force: bool = False


class PreprocessConfig(dg.Config):
    family_names: list[str] | None = None
    force: bool = False


class AnalyzeConfig(dg.Config):
    force: bool = False


class PlanConfig(dg.Config):
    style: str = "upbeat"
    target_duration: int = 180
    focus: str = "happiness with family"
    force: bool = False


class AssembleConfig(dg.Config):
    version: int = 1
    force: bool = False
    skip_broken: bool = False


class IterateConfig(dg.Config):
    workspace: str = Config.run_workspace()
    style: str = "upbeat"
    max_rounds: int = 2
    feedback: str | None = None


class VariationsConfig(dg.Config):
    workspace: str = Config.run_workspace()
    styles: str = "energetic,reflective,cinematic"


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _ws(context: dg.AssetExecutionContext) -> str:
    return context.resources.io_manager.workspace_path


# ---------------------------------------------------------------------------
# Assets — one per pipeline stage
# ---------------------------------------------------------------------------

@dg.asset(
    group_name="vlog",
    retry_policy=dg.RetryPolicy(max_retries=2, delay=30),
)
def manifest(
    context: dg.AssetExecutionContext,
    config: FetchConfig,
) -> str:
    """Stage 1: Download media from Synology Photos."""
    ws = _ws(context)
    out = str(Path(ws) / "manifest.json")

    if not config.force and _output_exists(ws, "manifest"):
        context.log.info("Skipping fetch — manifest.json exists")
        return out

    if not config.from_date or not config.to_date:
        raise dg.Failure(
            description="fetch requires from_date and to_date. "
            "Use the 'auto' CLI command or set them in the Launchpad."
        )

    from .fetch import fetch as do_fetch
    cfg = Config.load(ws)
    items = do_fetch(
        cfg,
        from_date=config.from_date,
        to_date=config.to_date,
        country=config.country,
        first_level=config.first_level,
        district=config.district,
        person_ids=config.person_ids,
        item_types=config.item_types,
    )
    context.log.info(f"Fetched {len(items)} items")
    return out


@dg.asset(
    group_name="vlog",
    retry_policy=dg.RetryPolicy(max_retries=1),
)
def preprocessed(
    context: dg.AssetExecutionContext,
    manifest: str,
    config: PreprocessConfig,
) -> str:
    """Stage 2: Tier items, cluster duplicates, build timeline."""
    ws = _ws(context)
    out = str(Path(ws) / "preprocessed.json")

    if not config.force and _output_exists(ws, "preprocessed"):
        context.log.info("Skipping preprocess — preprocessed.json exists")
        return out

    from .preprocess import preprocess as do_preprocess
    cfg = Config.load(ws)
    result = do_preprocess(cfg, family_names=config.family_names)
    context.log.info(
        f"Preprocessed: {result['selected_items']}/{result['total_items']} items, "
        f"tiers: {result['tier_counts']}"
    )
    return out


@dg.asset(
    group_name="vlog",
    retry_policy=dg.RetryPolicy(max_retries=1, delay=10),
    op_tags={"dagster/concurrency_key": "ollama"},
)
def analysis(
    context: dg.AssetExecutionContext,
    preprocessed: str,
    config: AnalyzeConfig,
) -> str:
    """Stage 3: Analyze media with vision model (llava:7b)."""
    ws = _ws(context)
    out = str(Path(ws) / "analysis.json")

    if not config.force and _output_exists(ws, "analysis"):
        context.log.info("Skipping analyze — analysis.json exists")
        return out

    from .analyze import analyze as do_analyze
    cfg = Config.load(ws)

    t0 = time.monotonic()

    def on_progress(current: int, total: int, filename: str) -> None:
        if current % max(total // 20, 1) == 0 or current == total:
            elapsed = time.monotonic() - t0
            eta_min = (elapsed / current * (total - current) / 60) if current > 0 else 0
            pct = current / total * 100 if total else 0
            context.log.info(f"Analyze: {current}/{total} ({pct:.0f}%) ETA {eta_min:.1f}min — {filename}")
            context.log_event(
                dg.AssetObservation(
                    asset_key=context.asset_key,
                    metadata={
                        "progress_pct": dg.MetadataValue.float(pct),
                        "current": dg.MetadataValue.int(current),
                        "total": dg.MetadataValue.int(total),
                        "eta_minutes": dg.MetadataValue.float(round(eta_min, 1)),
                        "current_item": dg.MetadataValue.text(filename),
                    },
                )
            )

    results = do_analyze(cfg, progress_callback=on_progress)
    ok = sum(1 for r in results if r.get("vision"))
    context.log.info(f"Analysis complete: {ok}/{len(results)} with vision results")
    return out


@dg.asset(
    group_name="vlog",
    retry_policy=dg.RetryPolicy(max_retries=2, delay=15),
    op_tags={"dagster/concurrency_key": "ollama"},
)
def edl(
    context: dg.AssetExecutionContext,
    preprocessed: str,
    analysis: str,
    config: PlanConfig,
) -> str:
    """Stage 4: Generate edit decision list using local LLM."""
    ws = _ws(context)
    out = str(Path(ws) / "edl.json")

    if not config.force and _output_exists(ws, "edl"):
        context.log.info("Skipping plan — edl.json exists")
        return out

    from .plan import plan as do_plan
    cfg = Config.load(ws)
    result = do_plan(
        cfg,
        style=config.style,
        target_duration=config.target_duration,
        focus=config.focus,
    )
    context.log.info(
        f"EDL: {len(result.segments)} segments, "
        f"{len(result.all_items())} items, "
        f"~{result.estimated_duration():.0f}s"
    )
    return out


@dg.asset(
    group_name="vlog",
    retry_policy=dg.RetryPolicy(max_retries=1, delay=30),
)
def vlog_video(
    context: dg.AssetExecutionContext,
    edl: str,
    config: AssembleConfig,
) -> dg.MaterializeResult:
    """Stage 5: Render vlog from EDL via FFmpeg."""
    ws = _ws(context)

    if not config.force and _output_exists(ws, "vlog_video"):
        context.log.info("Skipping assemble — vlog already exists")
        return dg.MaterializeResult(
            metadata={"path": dg.MetadataValue.text("(skipped)"), "version": dg.MetadataValue.int(0)}
        )

    from .assemble import assemble as do_assemble
    cfg = Config.load(ws)

    t0 = time.monotonic()

    def on_progress(current: int, total: int, clip_name: str) -> None:
        if current % max(total // 10, 1) == 0 or current == total:
            elapsed = time.monotonic() - t0
            eta_min = (elapsed / current * (total - current) / 60) if current > 0 else 0
            pct = current / total * 100 if total else 0
            context.log.info(f"Assemble: {current}/{total} ({pct:.0f}%) ETA {eta_min:.1f}min — {clip_name}")
            context.log_event(
                dg.AssetObservation(
                    asset_key=context.asset_key,
                    metadata={
                        "progress_pct": dg.MetadataValue.float(pct),
                        "current": dg.MetadataValue.int(current),
                        "total": dg.MetadataValue.int(total),
                        "eta_minutes": dg.MetadataValue.float(round(eta_min, 1)),
                        "current_clip": dg.MetadataValue.text(clip_name),
                    },
                )
            )

    output_path = do_assemble(
        cfg, version=config.version, progress_callback=on_progress,
        skip_broken=config.skip_broken,
    )
    context.log.info(f"Assembled: {output_path}")

    return dg.MaterializeResult(
        metadata={
            "path": dg.MetadataValue.path(str(output_path)),
            "version": dg.MetadataValue.int(config.version),
        }
    )


# ---------------------------------------------------------------------------
# Iterate job (op-based, not an asset — mutates existing state)
# ---------------------------------------------------------------------------

@dg.op(
    retry_policy=dg.RetryPolicy(max_retries=1, delay=15),
    tags={"dagster/concurrency_key": "ollama"},
)
def iterate_op(config: IterateConfig) -> None:
    """Self-critique or apply feedback, then re-render."""
    from .iterate import apply_feedback, self_critique

    cfg = Config.load(config.workspace)
    if config.feedback:
        apply_feedback(cfg, config.feedback)
    else:
        self_critique(cfg, style=config.style, max_rounds=config.max_rounds)


@dg.job(name="iterate")
def iterate_job() -> None:
    iterate_op()


@dg.op(
    retry_policy=dg.RetryPolicy(max_retries=1, delay=15),
    tags={"dagster/concurrency_key": "ollama"},
)
def variations_op(config: VariationsConfig) -> None:
    """Generate multiple vlog variations with different styles."""
    from .iterate import generate_variations

    cfg = Config.load(config.workspace)
    style_list = [s.strip() for s in config.styles.split(",")]
    outputs = generate_variations(cfg, styles=style_list)
    for path in outputs:
        print(f"  {path}")


@dg.job(name="variations")
def variations_job() -> None:
    variations_op()


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

full_pipeline = dg.define_asset_job(
    name="full_pipeline",
    selection=dg.AssetSelection.groups("vlog"),
)

from_plan = dg.define_asset_job(
    name="from_plan",
    selection=dg.AssetSelection.assets("edl", "vlog_video"),
)


# ---------------------------------------------------------------------------
# Definitions — entry point for Dagster
# ---------------------------------------------------------------------------

defs = dg.Definitions(
    assets=[manifest, preprocessed, analysis, edl, vlog_video],
    jobs=[full_pipeline, from_plan, iterate_job, variations_job],
    resources={
        "io_manager": WorkspaceIOManager(base_dir="./workspace", run_name="default"),
    },
)
