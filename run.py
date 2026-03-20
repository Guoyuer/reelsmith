"""CLI entry point for the vlog pipeline (Dagster-orchestrated).

All commands submit runs to the Dagster webserver. Start services first:
    dagster dev -m pipeline.definitions -p 3000
"""

from __future__ import annotations

import os
import sys
import time

import click


DAGSTER_HOST = os.getenv("DAGSTER_HOST", "localhost")
DAGSTER_PORT = int(os.getenv("DAGSTER_PORT", "3000"))


def _run_name(ctx: click.Context) -> str:
    return ctx.obj["run_name"] or "default"


def _submit(job_name: str, run_name: str, run_config: dict | None = None):
    """Submit a job to the Dagster webserver and stream status."""
    from dagster_graphql import DagsterGraphQLClient

    config = run_config or {}
    config.setdefault("resources", {})
    config["resources"]["io_manager"] = {
        "config": {"base_dir": "./workspace", "run_name": run_name},
    }

    try:
        client = DagsterGraphQLClient(DAGSTER_HOST, port_number=DAGSTER_PORT)
        try:
            click.echo("Reloading code location...")
            client.reload_repository_location("pipeline.definitions")
        except Exception as reload_err:
            click.echo(f"Code reload warning: {reload_err}", err=True)

        # Retry submission — webserver may need a moment after reload
        run_id = None
        for attempt in range(10):
            try:
                run_id = client.submit_job_execution(
                    job_name=job_name, run_config=config,
                )
                break
            except Exception as submit_err:
                if "JobNotFoundError" in str(submit_err) and attempt < 9:
                    time.sleep(1)
                    continue
                raise
    except Exception as e:
        click.echo(
            f"Failed to submit to Dagster at {DAGSTER_HOST}:{DAGSTER_PORT}\n"
            f"Is it running? Start with: dagster dev -m pipeline.definitions -p {DAGSTER_PORT}\n"
            f"Error: {e}",
            err=True,
        )
        raise SystemExit(1)

    url = f"http://{DAGSTER_HOST}:{DAGSTER_PORT}/runs/{run_id}"
    click.echo(f"Run submitted: {run_id}")
    click.echo(f"View at: {url}")

    # Poll until complete
    terminal_states = {"SUCCESS", "FAILURE", "CANCELED"}
    while True:
        time.sleep(5)
        status = client.get_run_status(run_id)
        status_str = status.value if hasattr(status, "value") else str(status)
        click.echo(f"  Status: {status_str}")
        if status_str in terminal_states:
            break

    if status_str != "SUCCESS":
        click.echo(f"Run {status_str}. See details at {url}", err=True)
        raise SystemExit(1)

    click.echo(f"Run completed successfully.")


ITEM_TYPE_NAMES = {"photo": 0, "video": 1, "live": 3, "motion": 6}


def _parse_item_types(value: str) -> list[int]:
    """Parse 'photo,video' or '0,1' into [0, 1]."""
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
    """Automated vlog pipeline: fetch → preprocess → analyze → plan → assemble."""
    ctx.ensure_object(dict)
    ctx.obj["run_name"] = run_name


# ---------------------------------------------------------------------------
# Main commands
# ---------------------------------------------------------------------------

@cli.command()
@click.option("-f", "--from-date", required=True, help="Start date (YYYY-MM-DD)")
@click.option("-t", "--to-date", required=True, help="End date (YYYY-MM-DD)")
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
@click.option("--force-analyze", is_flag=True, help="Force re-analyze (ignore cached)")
@click.option("--lang", default="en", type=click.Choice(["en", "cn", "both"]),
              help="Text language: en=English (default), cn=Chinese, both=bilingual")
@click.option("--family", default=None,
              help="Comma-separated family member names for tiering (default: auto-detect from NAS face data)")
@click.pass_context
def full(ctx, from_date, to_date, duration, trip_type, style, focus,
         item_types, music, width, height, fps, quality,
         country, district, force_analyze, family, lang):
    """Run the full pipeline end-to-end."""
    type_list = _parse_item_types(item_types) if item_types else None

    config: dict = {"ops": {}}

    # Fetch
    fetch_cfg: dict = {"from_date": from_date, "to_date": to_date, "force": True}
    if country:
        fetch_cfg["country"] = country
    if district:
        fetch_cfg["district"] = district
    if type_list:
        fetch_cfg["item_types"] = type_list
    config["ops"]["fetch_media"] = {"config": fetch_cfg}

    # Preprocess
    preprocess_cfg: dict = {}
    if family:
        preprocess_cfg["family_names"] = [n.strip() for n in family.split(",")]
    config["ops"]["preprocess"] = {"config": preprocess_cfg}

    # Analyze
    config["ops"]["analyze"] = {"config": {
        "force": force_analyze,
    }}

    # Plan
    plan_cfg: dict = {
        "style": style, "target_duration": duration,
        "focus": focus, "trip_type": trip_type,
        "language": lang,
    }
    # Parse --music: "auto"|"local" → generate, "/path" → file, "none" → skip
    music_backend = "gemini"
    if music == "local":
        plan_cfg["music_file"] = "auto"
        music_backend = "local"
    elif music == "none":
        pass  # no music_file → plan sets music_mode="none"
    elif music == "auto":
        plan_cfg["music_file"] = "auto"
    else:
        plan_cfg["music_file"] = music  # custom file path
    config["ops"]["plan"] = {"config": plan_cfg}

    # Generate Music
    config["ops"]["generate_music"] = {"config": {
        "music_backend": music_backend,
    }}

    # Assemble
    config["ops"]["assemble"] = {"config": {
        "width": width, "height": height, "fps": fps, "skip_broken": True,
        "quality": quality,
    }}

    _submit("full_pipeline", _run_name(ctx), config)


@cli.command()
@click.pass_context
def resume(ctx):
    """Resume pipeline — auto-skips stages with existing outputs."""
    _submit("full_pipeline", _run_name(ctx))


@cli.command()
@click.option("--duration", default=60, type=int, help="Target vlog length in seconds")
@click.option("--trip-type", default="family",
              type=click.Choice(["family", "solo", "food", "adventure", "architecture", "general"]))
@click.option("--style", default="upbeat",
              type=click.Choice(["upbeat", "cinematic", "reflective", "energetic"]))
@click.option("--focus", default="", help="What to emphasize")
@click.option("--lang", default="en", type=click.Choice(["en", "cn", "both"]),
              help="Text language: en=English (default), cn=Chinese, both=bilingual")
@click.pass_context
def plan(ctx, duration, trip_type, style, focus, lang):
    """Re-plan and re-assemble (uses cached media + analysis)."""
    _submit("full_pipeline", _run_name(ctx), {
        "ops": {
            "plan": {"config": {
                "style": style, "target_duration": duration,
                "focus": focus, "trip_type": trip_type,
                "language": lang,
            }},
        },
    })


@cli.command()
@click.option("-v", "--version", default=None, type=int, help="EDL version to render")
@click.option("--quality", default=1.0, type=float,
              help="Bitrate multiplier: 0.5=draft, 1.0=YouTube (default), 2.0=master")
@click.pass_context
def assemble(ctx, version, quality):
    """Re-render the vlog from current or specified EDL version."""
    from pipeline.config import Config as PipelineConfig
    from pipeline.edl import find_latest_version

    rn = _run_name(ctx)
    if version is None:
        ws = PipelineConfig.run_workspace(run_name=rn)
        cfg = PipelineConfig.load(ws)
        version = find_latest_version(cfg) + 1

    _submit("full_pipeline", rn, {
        "ops": {
            "assemble": {"config": {"version": version, "quality": quality}},
        },
    })


# ---------------------------------------------------------------------------
# Workspace management
# ---------------------------------------------------------------------------

def _fmt_size(size_bytes: int) -> str:
    """Human-readable file size."""
    if size_bytes >= 1024**3:
        return f"{size_bytes / 1024**3:.1f} GB"
    if size_bytes >= 1024**2:
        return f"{size_bytes / 1024**2:.0f} MB"
    return f"{size_bytes / 1024:.0f} KB"


def _dir_size(path) -> tuple[int, int]:
    """(total_bytes, file_count) for a directory."""
    from pathlib import Path
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
    """Human-readable age from an mtime."""
    age = time.time() - mtime
    if age < 3600:
        return f"{max(1, int(age / 60))}m ago"
    if age < 86400:
        return f"{int(age / 3600)}h ago"
    return f"{int(age / 86400)}d ago"


def _latest_mtime(path) -> float:
    """Most recent file mtime in a directory tree."""
    from pathlib import Path
    path = Path(path)
    latest = 0.0
    if path.exists():
        for f in path.rglob("*"):
            if f.is_file():
                latest = max(latest, f.stat().st_mtime)
    return latest


def _run_detail(run_dir) -> dict:
    """Extract pipeline-aware metadata from a run directory."""
    import json
    from pathlib import Path

    info: dict = {"name": run_dir.name, "path": run_dir}
    size, count = _dir_size(run_dir)
    info["size"] = size
    info["file_count"] = count
    info["last_used"] = _latest_mtime(run_dir)

    # EDL versions
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

    # Output versions
    output_dir = run_dir / "output"
    outputs = sorted(output_dir.glob("vlog_v*.mp4")) if output_dir.exists() else []
    info["outputs"] = [
        {"path": o, "version": int(o.stem.split("_v")[1]), "size": o.stat().st_size}
        for o in outputs
    ]

    # Reclaimable: old output versions
    info["old_output_bytes"] = sum(o["size"] for o in info["outputs"][:-1]) if len(info["outputs"]) > 1 else 0

    # Reclaimable: leftover intermediates (_nomix, _speech, concat_list)
    intermediates = (
        list(output_dir.glob("*_nomix.mp4")) +
        list(output_dir.glob("*_speech.wav")) +
        list(output_dir.glob("concat_list.txt"))
    ) if output_dir.exists() else []
    info["intermediate_bytes"] = sum(f.stat().st_size for f in intermediates)
    info["intermediate_files"] = intermediates

    # Reclaimable: legacy _txt.mp4 clips (text overlay now baked in)
    clips_dir = run_dir / "clips"
    legacy = list(clips_dir.glob("*_txt.mp4")) if clips_dir.exists() else []
    info["legacy_txt_bytes"] = sum(f.stat().st_size for f in legacy)
    info["legacy_txt_files"] = legacy

    # Clips info
    clips_size, clips_count = _dir_size(clips_dir)
    info["clips_size"] = clips_size
    info["clips_count"] = clips_count - len(legacy)  # exclude legacy from count

    # Contact sheets
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
    import json
    import shutil
    from pathlib import Path

    ws = Path("./workspace")
    if not ws.exists():
        click.echo("No workspace directory found.")
        return

    # --- Shared data ---
    shared = [
        ("media", "Source photos & videos", ws / "media"),
        ("music", "Generated music (Lyria cache)", ws / "music"),
        ("keyframes", "Video keyframe cache", ws / "keyframes"),
        ("analysis_cache", "Analysis cache", ws / "analysis_cache"),
        ("thumbnails", "Photo thumbnails", ws / "thumbnails"),
    ]

    total = 0
    click.echo("\n=== Workspace ===\n")
    click.echo("Shared data:")

    # Media: show photo/video counts from any manifest
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
                break  # use first manifest found
            except Exception:
                pass
        media_detail = f"{media_count} files"
        if n_photos or n_videos:
            media_detail = f"{n_photos} photos, {n_videos} videos"
        click.echo(f"  {_fmt_size(media_size):>8s}  {media_detail:>22s}  Source media from NAS")

    for key, label, path in shared[1:]:
        size, count = _dir_size(path)
        total += size
        if size > 0:
            click.echo(f"  {_fmt_size(size):>8s}  {count:>17d} files  {label}")

    # --- Per-run data ---
    runs_dir = ws / "runs"
    runs = []
    if runs_dir.exists():
        for d in sorted(runs_dir.iterdir()):
            if d.is_dir():
                runs.append(_run_detail(d))
        # Sort by last used (most recent first)
        runs.sort(key=lambda r: r["last_used"], reverse=True)

    runs_total = sum(r["size"] for r in runs)
    total += runs_total

    click.echo(f"\nRuns: {_fmt_size(runs_total)} across {len(runs)} runs")
    click.echo()

    total_reclaimable = 0
    for r in runs:
        age = _age_str(r["last_used"]) if r["last_used"] else "empty"
        click.echo(f"  {r['name']} ({_fmt_size(r['size'])}, {age})")

        # EDL line
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

        # Outputs line
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

        # Clips line
        if r["clips_count"] > 0:
            click.echo(f"    Clips: {r['clips_count']} cached ({_fmt_size(r['clips_size'])})")

        # Reclaimable annotations
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

    # --- Summary ---
    click.echo(f"\n{'─' * 50}")
    click.echo(f"Total: {_fmt_size(total)}")
    if total_reclaimable:
        click.echo(f"Reclaimable with --prune: {_fmt_size(total_reclaimable)}")

    # --- Handle --prune ---
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

    # --- Handle --clean ---
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
