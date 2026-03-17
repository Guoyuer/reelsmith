"""CLI entry point for the vlog pipeline (Dagster-orchestrated).

All commands submit runs to the Dagster webserver (localhost:3000) so they
appear in the UI. Start the webserver first:
    dagster-webserver -m pipeline.definitions -p 3000
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

    # Build run config with resource override for run_name
    config = run_config or {}
    config.setdefault("resources", {})
    config["resources"]["io_manager"] = {
        "config": {"base_dir": "./workspace", "run_name": run_name},
    }

    try:
        client = DagsterGraphQLClient(DAGSTER_HOST, port_number=DAGSTER_PORT)
        # Reload code so the webserver always uses the latest definitions
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
            f"Failed to submit to Dagster webserver at {DAGSTER_HOST}:{DAGSTER_PORT}\n"
            f"Is it running? Start with: dagster-webserver -m pipeline.definitions -p {DAGSTER_PORT}\n"
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
@click.option("--run-name", "-n", default=None, help="Run name (subdirectory under workspace/, default: 'default')")
@click.pass_context
def cli(ctx: click.Context, run_name: str | None) -> None:
    """Automated vlog pipeline: fetch -> preprocess -> analyze -> plan -> assemble -> iterate."""
    ctx.ensure_object(dict)
    ctx.obj["run_name"] = run_name


@cli.command()
@click.pass_context
def resume(ctx):
    """Resume pipeline — auto-skips stages with existing outputs."""
    _submit("full_pipeline", _run_name(ctx))


@cli.command()
@click.option("--from-date", "-f", required=True, help="Start date (YYYY-MM-DD)")
@click.option("--to-date", "-t", required=True, help="End date (YYYY-MM-DD)")
@click.option("--country", default=None, help="Filter by country")
@click.option("--first-level", default=None, help="Filter by state/province")
@click.option("--district", default=None, help="Filter by district/city")
@click.option("--person-ids", default=None, help="Comma-separated person IDs")
@click.option("--item-types", default=None, help="Comma-separated: photo,video,live,motion (or 0,1,3,6)")
@click.option("--style", default="upbeat", help="Vlog style")
@click.option("--duration", default=180, type=int, help="Target duration in seconds")
@click.option("--focus", default="", help="What to emphasize (default: derived from trip-type)")
@click.option("--planner", default="algo", type=click.Choice(["algo", "api"]),
              help="Planning backend: auto (algorithmic) or api (Claude API)")
@click.option("--trip-type", default="family",
              type=click.Choice(["family", "solo", "food", "adventure", "architecture", "general"]),
              help="Trip type for scoring and narrative style")
@click.option("--music", default=None,
              help="Background music: 'auto' to generate via MusicGen, or path to mp3/wav file")
@click.option("--width", default=3840, type=int, help="Output width (default: 3840)")
@click.option("--height", default=2160, type=int, help="Output height (default: 2160)")
@click.option("--fps", default=60, type=int, help="Output FPS (default: 60)")
@click.option("--force-analyze", is_flag=True, help="Force re-run vision analysis (ignore cached analysis.json)")
@click.pass_context
def full(ctx, from_date, to_date, country, first_level, district, person_ids,
         item_types, style, duration, focus, planner, trip_type, music, width, height, fps,
         force_analyze):
    """Run the full pipeline end-to-end."""
    person_id_list = [int(x) for x in person_ids.split(",")] if person_ids else None
    type_list = _parse_item_types(item_types) if item_types else None

    config: dict = {"ops": {}}
    config["ops"]["fetch_media"] = {"config": {
        "from_date": from_date, "to_date": to_date,
        "force": True,
    }}
    config["ops"]["analyze"] = {"config": {"force": force_analyze}}
    if country:
        config["ops"]["fetch_media"]["config"]["country"] = country
    if first_level:
        config["ops"]["fetch_media"]["config"]["first_level"] = first_level
    if district:
        config["ops"]["fetch_media"]["config"]["district"] = district
    if person_id_list:
        config["ops"]["fetch_media"]["config"]["person_ids"] = person_id_list
    if type_list:
        config["ops"]["fetch_media"]["config"]["item_types"] = type_list

    plan_config: dict = {
        "planner": planner, "style": style, "target_duration": duration,
        "focus": focus, "trip_type": trip_type,
    }
    if music:
        plan_config["music_file"] = music
    config["ops"]["plan"] = {"config": plan_config}
    config["ops"]["assemble"] = {"config": {
        "width": width, "height": height, "fps": fps, "skip_broken": True,
    }}

    _submit("full_pipeline", _run_name(ctx), config)


@cli.command()
@click.option("--style", default="upbeat", help="Vlog style: upbeat, reflective, cinematic")
@click.option("--duration", default=180, type=int, help="Target duration in seconds")
@click.option("--focus", default="", help="What to emphasize (default: derived from trip-type)")
@click.option("--planner", default="algo", type=click.Choice(["algo", "api"]),
              help="Planning backend: auto (algorithmic) or api (Claude API)")
@click.option("--trip-type", default="family",
              type=click.Choice(["family", "solo", "food", "adventure", "architecture", "general"]),
              help="Trip type for scoring and narrative style")
@click.pass_context
def plan(ctx, style, duration, focus, planner, trip_type):
    """Re-plan + re-assemble (downstream)."""
    _submit("full_pipeline", _run_name(ctx), {
        "ops": {
            "plan": {"config": {
                "planner": planner, "style": style, "target_duration": duration,
                "focus": focus, "trip_type": trip_type,
            }},
        },
    })


@cli.command()
@click.option("--version", "-v", default=None, type=int, help="Version number")
@click.pass_context
def assemble(ctx, version):
    """Force re-assemble the vlog from current EDL."""
    from pipeline.config import Config as PipelineConfig
    from pipeline.iterate import _find_latest_version

    rn = _run_name(ctx)
    if version is None:
        ws = PipelineConfig.run_workspace(run_name=rn)
        cfg = PipelineConfig.load(ws)
        version = _find_latest_version(cfg) + 1

    _submit("full_pipeline", rn, {
        "ops": {
            "assemble": {"config": {"version": version}},
        },
    })


@cli.command()
@click.option("--feedback", default=None, help="Natural language feedback to apply")
@click.option("--rounds", default=2, type=int, help="Self-critique rounds (if no feedback given)")
@click.option("--style", default="upbeat", help="Style for self-critique")
@click.pass_context
def iterate(ctx, feedback, rounds, style):
    """Improve the vlog via self-critique or human feedback."""
    from pipeline.config import Config as PipelineConfig

    ws = PipelineConfig.run_workspace(run_name=_run_name(ctx))
    config: dict = {"ops": {"iterate_op": {"config": {
        "workspace": ws, "style": style, "max_rounds": rounds,
    }}}}
    if feedback:
        config["ops"]["iterate_op"]["config"]["feedback"] = feedback

    _submit("iterate", _run_name(ctx), config)


@cli.command()
@click.option("--styles", default="energetic,reflective,cinematic", help="Comma-separated variation styles")
@click.pass_context
def variations(ctx, styles):
    """Generate multiple vlog variations with different styles."""
    from pipeline.config import Config as PipelineConfig

    ws = PipelineConfig.run_workspace(run_name=_run_name(ctx))
    _submit("variations", _run_name(ctx), {
        "ops": {"variations_op": {"config": {"workspace": ws, "styles": styles}}},
    })


@cli.command()
@click.option("--clean", type=click.Choice(["media", "cache", "runs", "all"]),
              default=None, help="Delete shared data: media, cache, runs, or all")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
def workspace(clean, yes):
    """Show workspace disk usage and optionally clean up."""
    from pathlib import Path

    ws = Path("./workspace")
    if not ws.exists():
        click.echo("No workspace directory found.")
        return

    # Gather stats
    sections = {
        "media": ("Shared media (photos/videos from NAS)", ws / "media"),
        "cache": ("Shared analysis cache", ws / "analysis_cache"),
        "keyframes": ("Shared video keyframes", ws / "keyframes"),
        "music": ("Shared generated music", ws / "music"),
        "runs": ("Per-run data (EDLs, clips, output)", ws / "runs"),
    }

    click.echo()
    click.echo("=== Workspace Disk Usage ===")
    click.echo()
    total = 0
    for key, (label, path) in sections.items():
        if path.exists():
            size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
            count = sum(1 for f in path.rglob("*") if f.is_file())
        else:
            size, count = 0, 0
        total += size
        mb = size / 1024 / 1024
        click.echo(f"  {mb:8.1f} MB  {count:5d} files  {label}")
        if key == "runs" and path.exists():
            for run_dir in sorted(path.iterdir()):
                if run_dir.is_dir():
                    rs = sum(f.stat().st_size for f in run_dir.rglob("*") if f.is_file())
                    rc = sum(1 for f in run_dir.rglob("*") if f.is_file())
                    click.echo(f"  {rs/1024/1024:8.1f} MB  {rc:5d} files    └─ {run_dir.name}")

    click.echo(f"  {'─' * 8}──")
    click.echo(f"  {total/1024/1024:8.1f} MB  total")
    click.echo()

    if clean is None:
        return

    # Clean
    import shutil
    targets = []
    if clean == "media" or clean == "all":
        targets.append(("media", ws / "media"))
    if clean == "cache" or clean == "all":
        targets.append(("analysis_cache", ws / "analysis_cache"))
        targets.append(("keyframes", ws / "keyframes"))
        targets.append(("music", ws / "music"))
    if clean == "runs" or clean == "all":
        targets.append(("runs", ws / "runs"))

    sizes = []
    for name, path in targets:
        if path.exists():
            s = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
            sizes.append((name, path, s))

    if not sizes:
        click.echo("Nothing to clean.")
        return

    total_clean = sum(s for _, _, s in sizes)
    click.echo(f"Will delete {total_clean/1024/1024:.1f} MB:")
    for name, path, s in sizes:
        click.echo(f"  {s/1024/1024:.1f} MB  {path}")

    if not yes:
        click.confirm("Proceed?", abort=True)

    for name, path, _ in sizes:
        shutil.rmtree(path, ignore_errors=True)
        click.echo(f"  Deleted {path}")
    click.echo("Done.")


if __name__ == "__main__":
    cli()
