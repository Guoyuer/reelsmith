"""Dagster asset definitions for the vlog pipeline.

Each pipeline stage is a Dagster asset. Assets auto-skip when their output
file already exists (unless force=True in config). Re-materialize any asset
from the Dagster UI to force re-run it + all downstream dependencies.

Each run lives in its own subdirectory under the base workspace:
  workspace/runs/{run_name}/manifest.json, analysis.json, output/vlog_v1.mp4, ...

Set run_name in the IOManager config (Launchpad or CLI -n) to isolate runs.
"""

import json
import time
from pathlib import Path

import dagster as dg

from .analyze import analyze as do_analyze
from .assemble import assemble as do_assemble
from .config import Config
from .edl import EDL
from .fetch import fetch as do_fetch
from .plan import plan as do_plan
from .preprocess import preprocess as do_preprocess


# ---------------------------------------------------------------------------
# IOManager — workspace path + Config provider
# ---------------------------------------------------------------------------

class WorkspaceIOManager(dg.ConfigurableIOManager):
    """Workspace-aware resource for the vlog pipeline.

    - base_dir: root directory containing all runs (default: ./workspace)
    - run_name: subdirectory for this run (default: "default")

    Actual workspace path = {base_dir}/runs/{run_name}

    Set these in the Launchpad to target different runs:
      resources > io_manager > base_dir / run_name
    """

    base_dir: str = "./workspace"
    run_name: str = "default"

    @property
    def workspace_path(self) -> str:
        return Config.run_workspace(self.base_dir, self.run_name)

    @property
    def config(self) -> Config:
        return Config.load(self.workspace_path)

    def handle_output(self, context: dg.OutputContext, obj: object) -> None:
        pass  # stages write their own files

    def load_input(self, context: dg.InputContext) -> str:
        return self.workspace_path


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
    skip_clustering: bool = False  # skip dedup (for --planner visual, Gemini dedupes visually)


class AnalyzeConfig(dg.Config):
    force: bool = False
    skip_vision: bool = False  # skip local vision model (for --planner visual)


class PlanConfig(dg.Config):
    planner: str = "visual"  # "visual" (Gemini sees photos), "api" (text-only), "algo" (deterministic)
    trip_type: str = "family"  # family, solo, food, adventure, architecture, general
    style: str = "upbeat"
    target_duration: int = 180
    focus: str = ""  # empty = derive from trip_type
    music_file: str = ""  # path to background music (mp3/m4a/wav)


class AssembleConfig(dg.Config):
    version: int = 0  # 0 = auto-detect next version
    width: int = 3840
    height: int = 2160
    fps: int = 60
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
# Helpers
# ---------------------------------------------------------------------------

def _progress_cb(context: dg.AssetExecutionContext, t0: float, granularity: int = 10):
    """Create a progress callback that logs and emits AssetObservations."""
    label = context.asset_key.path[-1].capitalize()

    def cb(current: int, total: int, name: str) -> None:
        if current % max(total // granularity, 1) == 0 or current == total:
            elapsed = time.monotonic() - t0
            eta = (elapsed / current * (total - current) / 60) if current else 0
            pct = current / total * 100 if total else 0
            context.log.info(f"{label}: {current}/{total} ({pct:.0f}%) ETA {eta:.1f}min — {name}")
            context.log_event(dg.AssetObservation(
                asset_key=context.asset_key,
                metadata={
                    "progress_pct": dg.MetadataValue.float(pct),
                    "current": dg.MetadataValue.int(current),
                    "total": dg.MetadataValue.int(total),
                    "eta_minutes": dg.MetadataValue.float(round(eta, 1)),
                },
            ))

    return cb


def _md_table(headers: list[str], rows: list[list]) -> str:
    """Build a markdown table from headers and rows."""
    sep = ["-" * max(len(h), 3) for h in headers]
    lines = [" | ".join(headers), " | ".join(sep)]
    lines += [" | ".join(str(c) for c in row) for row in rows]
    return "\n".join(f"| {line} |" for line in lines)


def _preprocess_metadata(data: dict, extra: dict | None = None) -> dict:
    tc = data.get("tier_counts", {})
    timeline = data.get("timeline", [])
    meta: dict = {
        "total_items": dg.MetadataValue.int(data.get("total_items", 0)),
        "selected_items": dg.MetadataValue.int(data.get("selected_items", 0)),
        "family_members": dg.MetadataValue.md(
            ", ".join(data.get("family_names", [])) or "(none detected)"
        ),
        "tiers": dg.MetadataValue.md(_md_table(
            ["Tier", "Count", "Description"],
            [["A", tc.get("A", 0), "Family together"],
             ["B", tc.get("B", 0), "One family member"],
             ["C", tc.get("C", 0), "Scene / B-roll"],
             ["D", tc.get("D", 0), "Skipped"]],
        )),
        "timeline_days": dg.MetadataValue.int(len(timeline)),
        "chapters": dg.MetadataValue.int(sum(len(d.get("chapters", [])) for d in timeline)),
    }
    if timeline:
        meta["timeline"] = dg.MetadataValue.md(_md_table(
            ["Day", "Chapters", "Items"],
            [[f"{d.get('date','?')} ({d.get('day_name','?')})",
              len(d.get("chapters", [])),
              sum(c.get("count", 0) for c in d.get("chapters", []))]
             for d in timeline],
        ))
    if extra:
        meta.update(extra)
    return meta


def _analyze_metadata(results: list[dict], out: str, extra: dict | None = None) -> dict:
    ok = sum(1 for r in results if r.get("vision"))
    scored = sorted(
        (r for r in results if r.get("vision")),
        key=lambda r: r["vision"].get("togetherness", 0) + r["vision"].get("genuine_emotion", 0),
        reverse=True,
    )
    meta = {
        "items_analyzed": dg.MetadataValue.int(len(results)),
        "with_vision": dg.MetadataValue.int(ok),
        "top_scored": dg.MetadataValue.md(_md_table(
            ["File", "Tier", "Together", "Emotion", "Quality", "Description"],
            [[r["filename"][:30], r.get("tier", "?"),
              r["vision"].get("togetherness", "-"), r["vision"].get("genuine_emotion", "-"),
              r["vision"].get("visual_quality", "-"), r["vision"].get("description", "")]
             for r in list(scored)[:10]],
        )),
        "analysis_path": dg.MetadataValue.path(out),
    }
    if extra:
        meta.update(extra)
    return meta


# ---------------------------------------------------------------------------
# Assets — one per pipeline stage
# ---------------------------------------------------------------------------

@dg.asset(
    group_name="vlog",
    retry_policy=dg.RetryPolicy(max_retries=2, delay=30),
)
def fetch_media(
    context: dg.AssetExecutionContext,
    config: FetchConfig,
) -> dg.MaterializeResult:
    """Download photos/videos from Synology Photos."""
    io = context.resources.io_manager
    out = Path(io.workspace_path) / "manifest.json"

    newly_fetched = 0
    if not config.force and out.exists():
        items = json.loads(out.read_text())
        for i, it in enumerate(items, 1):
            context.log.info(f"[{i}/{len(items)}] {it.get('filename', '?')} (cached)")
        context.log.info(f"Fetch complete: {len(items)} items (all cached)")
    else:
        if not config.from_date or not config.to_date:
            raise dg.Failure(
                description="fetch requires from_date and to_date. "
                "Use the 'auto' CLI command or set them in the Launchpad."
            )

        cfg = io.config
        existing_before = set(cfg.media_dir.iterdir()) if cfg.media_dir.exists() else set()
        items = do_fetch(
            cfg,
            from_date=config.from_date,
            to_date=config.to_date,
            country=config.country,
            first_level=config.first_level,
            district=config.district,
            person_ids=config.person_ids,
            item_types=config.item_types,
            log_fn=context.log.info,
        )
        existing_after = set(cfg.media_dir.iterdir()) if cfg.media_dir.exists() else set()
        newly_fetched = len(existing_after - existing_before)
        context.log.info(f"Fetched {len(items)} items ({newly_fetched} new, {len(items) - newly_fetched} cached)")

    sample = [it.get("filename", "?") for it in items[:10]]

    # Compute shared media disk usage
    media_dir = io.config.media_dir
    media_mb = 0.0
    media_files = 0
    if media_dir.exists():
        media_files = sum(1 for f in media_dir.iterdir() if f.is_file())
        media_mb = sum(f.stat().st_size for f in media_dir.iterdir() if f.is_file()) / 1024 / 1024

    return dg.MaterializeResult(
        metadata={
            "items": dg.MetadataValue.int(len(items)),
            "newly_fetched": dg.MetadataValue.int(newly_fetched),
            "from_cache": dg.MetadataValue.int(len(items) - newly_fetched),
            "manifest": dg.MetadataValue.path(str(out)),
            "sample_files": dg.MetadataValue.md("\n".join(f"- {f}" for f in sample)),
            "media_disk_mb": dg.MetadataValue.float(round(media_mb, 1)),
            "media_total_files": dg.MetadataValue.int(media_files),
        }
    )


@dg.asset(
    group_name="vlog",
    retry_policy=dg.RetryPolicy(max_retries=1),
)
def preprocess(
    context: dg.AssetExecutionContext,
    fetch_media,
    config: PreprocessConfig,
) -> dg.MaterializeResult:
    """Tier by family presence, cluster duplicates, build timeline."""
    io = context.resources.io_manager
    out = Path(io.workspace_path) / "preprocessed.json"

    if not config.force and out.exists():
        data = json.loads(out.read_text())
        for i, it in enumerate(data.get("items", []), 1):
            context.log.info(
                f"[{i}/{len(data.get('items', []))}] {it.get('filename', '?')}: "
                f"tier {it.get('tier', '?')} (family: {it.get('family_count', 0)})"
            )
        context.log.info(f"Preprocess complete: {data.get('selected_items', 0)} items")
        return dg.MaterializeResult(
            metadata=_preprocess_metadata(data, {
                "status": dg.MetadataValue.text("finished"),
            })
        )

    result = do_preprocess(io.config, family_names=config.family_names,
                           skip_clustering=config.skip_clustering, log_fn=context.log.info)
    context.log.info(
        f"Preprocessed: {result['selected_items']}/{result['total_items']} items, "
        f"tiers: {result['tier_counts']}"
    )
    return dg.MaterializeResult(metadata=_preprocess_metadata(result))


@dg.asset(
    group_name="vlog",
    retry_policy=dg.RetryPolicy(max_retries=1, delay=10),
)
def analyze(
    context: dg.AssetExecutionContext,
    preprocess,
    config: AnalyzeConfig,
) -> dg.MaterializeResult:
    """Analyze media with vision model (llava:7b)."""
    io = context.resources.io_manager
    out = Path(io.workspace_path) / "analysis.json"

    if not config.force and out.exists():
        results = json.loads(out.read_text())
        for i, r in enumerate(results, 1):
            desc = r.get("vision", {}).get("description", "") if r.get("vision") else "no vision"
            context.log.info(f"[{i}/{len(results)}] {r.get('filename', '?')} — {desc}")
        ok = sum(1 for r in results if r.get("vision"))
        context.log.info(f"Analyze complete: {ok}/{len(results)} with vision (all cached)")
        return dg.MaterializeResult(
            metadata=_analyze_metadata(results, str(out), {
                "status": dg.MetadataValue.text("finished"),
                "from_cache": dg.MetadataValue.int(len(results)),
                "newly_analyzed": dg.MetadataValue.int(0),
            })
        )

    existing_count = 0
    if out.exists():
        existing_count = sum(1 for r in json.loads(out.read_text()) if r.get("vision"))

    t0 = time.monotonic()
    results = do_analyze(
        io.config,
        skip_vision=config.skip_vision,
        progress_callback=_progress_cb(context, t0, granularity=20),
        log_fn=context.log.info,
    )
    ok = sum(1 for r in results if r.get("vision"))
    newly = ok - existing_count
    context.log.info(
        f"Analysis complete: {ok}/{len(results)} with vision — "
        f"{ok - newly} from cache, {newly} newly analyzed"
    )

    return dg.MaterializeResult(
        metadata=_analyze_metadata(results, str(out), {
            "from_cache": dg.MetadataValue.int(ok - newly),
            "newly_analyzed": dg.MetadataValue.int(newly),
            "duration_min": dg.MetadataValue.float(round((time.monotonic() - t0) / 60, 1)),
        })
    )


@dg.asset(
    group_name="vlog",
    retry_policy=dg.RetryPolicy(max_retries=2, delay=15),
)
def plan(
    context: dg.AssetExecutionContext,
    analyze,
    config: PlanConfig,
) -> dg.MaterializeResult:
    """Generate edit decision list using local LLM. Always re-plans (versioned)."""
    io = context.resources.io_manager

    result, version = do_plan(
        io.config,
        style=config.style,
        target_duration=config.target_duration,
        focus=config.focus,
        planner=config.planner,
        trip_type=config.trip_type,
        music_file=config.music_file or None,
        log_fn=context.log.info,
    )
    context.log.info(
        f"EDL v{version}: {len(result.segments)} segments, "
        f"{len(result.all_items())} items, "
        f"~{result.estimated_duration():.0f}s"
    )

    return dg.MaterializeResult(
        metadata={
            "style": dg.MetadataValue.text(config.style),
            "version": dg.MetadataValue.int(version),
            "target_duration": dg.MetadataValue.int(config.target_duration),
            "estimated_duration": dg.MetadataValue.float(round(result.estimated_duration(), 1)),
            "segments": dg.MetadataValue.int(len(result.segments)),
            "total_items": dg.MetadataValue.int(len(result.all_items())),
            "segment_summary": dg.MetadataValue.md(_md_table(
                ["Segment", "Items", "Transition"],
                [[seg.name, len(seg.items), seg.transition] for seg in result.segments],
            )),
        }
    )


@dg.asset(
    group_name="vlog",
    retry_policy=dg.RetryPolicy(max_retries=1, delay=30),
)
def assemble(
    context: dg.AssetExecutionContext,
    plan,
    config: AssembleConfig,
) -> dg.MaterializeResult:
    """Render vlog from EDL via FFmpeg. Always re-renders (versioned)."""
    from .iterate import _find_latest_version

    io = context.resources.io_manager
    cfg = io.config
    version = config.version if config.version > 0 else _find_latest_version(cfg)

    t0 = time.monotonic()
    output_path = do_assemble(
        cfg, version=version,
        resolution=(config.width, config.height), fps=config.fps,
        progress_callback=_progress_cb(context, t0),
        skip_broken=config.skip_broken,
    )
    context.log.info(f"Assembled: {output_path}")

    return dg.MaterializeResult(
        metadata={
            "output": dg.MetadataValue.path(str(output_path)),
            "version": dg.MetadataValue.int(version),
            "render_time_min": dg.MetadataValue.float(round((time.monotonic() - t0) / 60, 1)),
        }
    )


# ---------------------------------------------------------------------------
# Iterate job (op-based, not an asset — mutates existing state)
# ---------------------------------------------------------------------------

@dg.op(
    retry_policy=dg.RetryPolicy(max_retries=1, delay=15),
)
def iterate_op(context: dg.OpExecutionContext, config: IterateConfig) -> None:
    """Self-critique or apply feedback via Claude API, then re-render."""
    from .iterate import apply_feedback, self_critique

    cfg = Config.load(config.workspace)
    if config.feedback:
        apply_feedback(cfg, config.feedback, log_fn=context.log.info)
    else:
        self_critique(cfg, style=config.style, max_rounds=config.max_rounds,
                      log_fn=context.log.info)


@dg.job(name="iterate")
def iterate_job() -> None:
    iterate_op()


@dg.op(
    retry_policy=dg.RetryPolicy(max_retries=1, delay=15),
)
def variations_op(context: dg.OpExecutionContext, config: VariationsConfig) -> None:
    """Generate multiple vlog variations with different styles via Claude API."""
    from .iterate import generate_variations

    cfg = Config.load(config.workspace)
    outputs = generate_variations(cfg, styles=[s.strip() for s in config.styles.split(",")],
                                  log_fn=context.log.info)
    for path in outputs:
        context.log.info(f"  {path}")


@dg.job(name="variations")
def variations_job() -> None:
    variations_op()


# ---------------------------------------------------------------------------
# Jobs & Definitions
# ---------------------------------------------------------------------------

full_pipeline = dg.define_asset_job(
    name="full_pipeline",
    selection=dg.AssetSelection.groups("vlog"),
    executor_def=dg.in_process_executor,
)

defs = dg.Definitions(
    assets=[fetch_media, preprocess, analyze, plan, assemble],
    jobs=[full_pipeline, iterate_job, variations_job],
    resources={
        "io_manager": WorkspaceIOManager(base_dir="./workspace", run_name="default"),
    },
)
