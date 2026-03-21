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

from .assemble import assemble as do_assemble
from .config import Config
from .edl import EDL
from .fetch import fetch as do_fetch
from .music import generate_music_for_edl as do_generate_music
from .plan import plan as do_plan
from .prepare import prepare as do_prepare


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


class PrepareConfig(dg.Config):
    family_names: list[str] | None = None
    force: bool = False
    tz_offset: int | None = None  # UTC offset in hours; None = system local timezone


class PlanConfig(dg.Config):
    trip_type: str = "family"  # family, solo, food, adventure, architecture, general
    style: str = "upbeat"
    target_duration: int = 180
    focus: str = ""  # empty = derive from trip_type
    music_file: str = ""  # path to background music (mp3/m4a/wav)
    language: str = "en"  # en, cn, or both
    width: int = 3840
    height: int = 2160
    fps: int = 60
    quality: float = 1.0  # render quality — stored in EDL for assemble to read


class GenerateMusicConfig(dg.Config):
    music_backend: str = "gemini"  # "gemini" (Lyria RealTime) or "local" (MusicGen)


class AssembleConfig(dg.Config):
    version: int = 0  # 0 = auto-detect next version
    width: int = 3840
    height: int = 2160
    fps: int = 60
    skip_broken: bool = False
    quality: float = 1.0  # bitrate multiplier: 0.5=smaller, 1.0=YouTube quality, 2.0=master



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
    timeline = data.get("timeline", [])
    meta: dict = {
        "family_members": dg.MetadataValue.md(
            ", ".join(data.get("family_names", [])) or "(none detected)"
        ),
        "timeline_days": dg.MetadataValue.int(len(timeline)),
        "chapters": dg.MetadataValue.int(sum(len(d.get("chapters", [])) for d in timeline)),
    }
    if timeline:
        meta["timeline"] = dg.MetadataValue.md(_md_table(
            ["Day", "Chapters", "Items"],
            [[f"{d.get('date','?')} ({d.get('day_name','?')})",
              len(d.get("chapters", [])),
              len([item_id for c in d.get("chapters", []) for item_id in c.get("item_ids", [])])]
             for d in timeline],
        ))
    if extra:
        meta.update(extra)
    return meta


def _prepare_analysis_metadata(results: list[dict], out: str, extra: dict | None = None) -> dict:
    n_photos = sum(1 for r in results if r.get("media_type") == "photo")
    n_videos = len(results) - n_photos
    n_family = sum(1 for r in results if r.get("family_count", 0) >= 1)
    meta = {
        "items_analyzed": dg.MetadataValue.int(len(results)),
        "photos": dg.MetadataValue.int(n_photos),
        "videos": dg.MetadataValue.int(n_videos),
        "with_family": dg.MetadataValue.int(n_family),
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
        context.log.info(f"Fetch: {len(items)} items (all cached, skipping download)")
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
)
def prepare(
    context: dg.AssetExecutionContext,
    fetch_media,
    config: PrepareConfig,
) -> dg.MaterializeResult:
    """Prepare media: family detection, timeline, thumbnails, EXIF, video metadata."""
    io = context.resources.io_manager
    analysis_path = Path(io.workspace_path) / "analysis.json"
    manifest_path = Path(io.workspace_path) / "manifest.json"

    stale = (manifest_path.exists() and analysis_path.exists()
             and manifest_path.stat().st_mtime > analysis_path.stat().st_mtime)
    if stale:
        context.log.info("Manifest is newer — re-preparing")

    if not config.force and not stale and analysis_path.exists():
        results = json.loads(analysis_path.read_text())
        n_photos = sum(1 for r in results if r.get("media_type") == "photo")
        n_videos = len(results) - n_photos
        context.log.info(f"Prepare complete: {len(results)} items ({n_photos} photos, {n_videos} videos) — cached")
        return dg.MaterializeResult(metadata={
            "items": dg.MetadataValue.int(len(results)),
            "photos": dg.MetadataValue.int(n_photos),
            "videos": dg.MetadataValue.int(n_videos),
            "status": dg.MetadataValue.text("finished (cached)"),
        })

    t0 = time.monotonic()
    result = do_prepare(
        io.config,
        family_names=config.family_names,
        force=config.force,
        progress_callback=_progress_cb(context, t0, granularity=20),
        log_fn=context.log.info,
        tz_hours=config.tz_offset,
    )
    elapsed = round((time.monotonic() - t0) / 60, 1)
    analysis_path = Path(io.workspace_path) / "analysis.json"
    n_items = len(json.loads(analysis_path.read_text())) if analysis_path.exists() else 0
    family = ", ".join(result.get("family_names", [])) or "(none detected)"
    context.log.info(f"Prepared {n_items} items in {elapsed}min, family: {family}")

    return dg.MaterializeResult(metadata={
        "items": dg.MetadataValue.int(n_items),
        "family": dg.MetadataValue.text(family),
        "duration_min": dg.MetadataValue.float(elapsed),
    })


@dg.asset(
    group_name="vlog",
)
def plan(
    context: dg.AssetExecutionContext,
    prepare,
    config: PlanConfig,
) -> dg.MaterializeResult:
    """Generate edit decision list via Gemini visual planner. Always re-plans (versioned)."""
    io = context.resources.io_manager

    result, version = do_plan(
        io.config,
        style=config.style,
        target_duration=config.target_duration,
        focus=config.focus,
        trip_type=config.trip_type,
        music_file=config.music_file or None,
        language=config.language,
        resolution=(config.width, config.height),
        fps=config.fps,
        quality=config.quality,
        log_fn=context.log.info,
    )
    all_items = result.all_items()
    n_videos = sum(1 for i in all_items if i.media_type == "video")
    n_photos = len(all_items) - n_videos
    n_keep_audio = sum(1 for i in all_items if i.keep_audio)
    n_speed = sum(1 for i in all_items if (i.playback_speed or 1.0) != 1.0)
    vid_time = sum(i.display_duration for i in all_items if i.media_type == "video")
    total_time = sum(i.display_duration for i in all_items)
    vid_pct = int(vid_time / total_time * 100) if total_time > 0 else 0

    context.log.info(
        f"[Gemini] EDL v{version}: {len(result.segments)} segments, "
        f"{n_photos} photos + {n_videos} videos ({vid_pct}% video), "
        f"~{result.estimated_duration():.0f}s"
    )
    if n_keep_audio:
        context.log.info(f"[Gemini] Audio: {n_keep_audio} clips with speech preserved")
    if n_speed:
        context.log.info(f"[Gemini] Speed ramps: {n_speed} clips")
    for seg in result.segments:
        extras = []
        if getattr(seg, "mode", "narrative") == "montage":
            extras.append("MONTAGE")
        if getattr(seg, "color_temp", "neutral") != "neutral":
            extras.append(f"color={seg.color_temp}")
        extra_str = f" [{', '.join(extras)}]" if extras else ""
        context.log.info(
            f"[Gemini]   {seg.name}: {len(seg.items)} items, "
            f"transition={seg.transition}{extra_str}"
        )

    return dg.MaterializeResult(
        metadata={
            "style": dg.MetadataValue.text(config.style),
            "version": dg.MetadataValue.int(version),
            "target_duration": dg.MetadataValue.int(config.target_duration),
            "estimated_duration": dg.MetadataValue.float(round(result.estimated_duration(), 1)),
            "segments": dg.MetadataValue.int(len(result.segments)),
            "photos": dg.MetadataValue.int(n_photos),
            "videos": dg.MetadataValue.int(n_videos),
            "video_pct": dg.MetadataValue.int(vid_pct),
            "keep_audio": dg.MetadataValue.int(n_keep_audio),
            "speed_ramped": dg.MetadataValue.int(n_speed),
            "segment_summary": dg.MetadataValue.md(_md_table(
                ["Segment", "Items", "Transition", "Mode", "Color"],
                [[seg.name, len(seg.items), seg.transition,
                  getattr(seg, "mode", "narrative"),
                  getattr(seg, "color_temp", "neutral")]
                 for seg in result.segments],
            )),
        }
    )


@dg.asset(
    group_name="vlog",
)
def generate_music(
    context: dg.AssetExecutionContext,
    plan,
    config: GenerateMusicConfig,
) -> dg.MaterializeResult:
    """Generate background music from EDL mood descriptions."""
    io = context.resources.io_manager
    cfg = io.config

    track_path = do_generate_music(
        cfg, backend=config.music_backend, log_fn=context.log.info,
    )

    if track_path:
        size_kb = round(track_path.stat().st_size / 1024) if track_path.exists() else 0
        context.log.info(f"[Gemini Lyria] Music generated: {track_path.name} ({size_kb}KB)")
        return dg.MaterializeResult(
            metadata={
                "status": dg.MetadataValue.text("generated"),
                "music_file": dg.MetadataValue.path(str(track_path)),
                "backend": dg.MetadataValue.text(config.music_backend),
            }
        )
    context.log.info("Music generation skipped (music_mode != auto or no API key)")
    return dg.MaterializeResult(
        metadata={"status": dg.MetadataValue.text("skipped")}
    )


@dg.asset(
    group_name="vlog",
)
def assemble(
    context: dg.AssetExecutionContext,
    generate_music,
    config: AssembleConfig,
) -> dg.MaterializeResult:
    """Render vlog from EDL via FFmpeg. Always re-renders (versioned)."""
    from .edl import find_latest_version

    io = context.resources.io_manager
    cfg = io.config
    version = config.version if config.version > 0 else find_latest_version(cfg)

    t0 = time.monotonic()
    output_path, validation_issues = do_assemble(
        cfg, version=version,
        resolution=(config.width, config.height), fps=config.fps,
        progress_callback=_progress_cb(context, t0),
        skip_broken=config.skip_broken,
        quality=config.quality,
    )
    render_min = round((time.monotonic() - t0) / 60, 1)
    size_mb = round(output_path.stat().st_size / 1024 / 1024, 1) if output_path.exists() else 0
    context.log.info(
        f"[FFmpeg] Assembled: {output_path.name} ({size_mb}MB, {render_min}min render, "
        f"{config.width}x{config.height}@{config.fps}fps, quality={config.quality})"
    )

    # Log validation results
    val_errors = [i for i in validation_issues if i["level"] == "error"]
    val_warnings = [i for i in validation_issues if i["level"] == "warning"]
    if val_warnings:
        for issue in val_warnings:
            context.log.warning(f"[FFmpeg] Validation: {issue['message']}")
    if not validation_issues:
        context.log.info("[FFmpeg] Validation: all checks passed")

    metadata = {
        "output": dg.MetadataValue.path(str(output_path)),
        "version": dg.MetadataValue.int(version),
        "render_time_min": dg.MetadataValue.float(render_min),
        "file_size_mb": dg.MetadataValue.float(size_mb),
        "resolution": dg.MetadataValue.text(f"{config.width}x{config.height}@{config.fps}fps"),
        "quality": dg.MetadataValue.float(config.quality),
        "validation_passed": dg.MetadataValue.bool(len(val_errors) == 0),
        "validation_warnings": dg.MetadataValue.int(len(val_warnings)),
    }
    if validation_issues:
        metadata["validation_details"] = dg.MetadataValue.md(
            _md_table(
                ["Level", "Check", "Message"],
                [[i["level"].upper(), i["check"], i["message"]]
                 for i in validation_issues],
            )
        )

    return dg.MaterializeResult(metadata=metadata)



# ---------------------------------------------------------------------------
# Jobs & Definitions
# ---------------------------------------------------------------------------

full_pipeline = dg.define_asset_job(
    name="full_pipeline",
    selection=dg.AssetSelection.groups("vlog"),
    executor_def=dg.in_process_executor,
)

defs = dg.Definitions(
    assets=[fetch_media, prepare, plan, generate_music, assemble],
    jobs=[full_pipeline],
    resources={
        "io_manager": WorkspaceIOManager(base_dir="./workspace", run_name="default"),
    },
)
