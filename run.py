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


DAGSTER_HOST = "localhost"
DAGSTER_PORT = 3000


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
        run_id = client.submit_job_execution(
            job_name=job_name,
            run_config=config,
        )
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
@click.option("--item-types", default=None, help="Comma-separated item types (0=photo,1=video,3=live,6=motion)")
@click.option("--style", default="upbeat", help="Vlog style")
@click.option("--duration", default=180, type=int, help="Target duration in seconds")
@click.option("--focus", default="happiness with family", help="What to emphasize")
@click.pass_context
def auto(ctx, from_date, to_date, country, first_level, district, person_ids,
         item_types, style, duration, focus):
    """Run the full pipeline end-to-end."""
    person_id_list = [int(x) for x in person_ids.split(",")] if person_ids else None
    type_list = [int(x) for x in item_types.split(",")] if item_types else None

    config: dict = {"ops": {}}
    config["ops"]["manifest"] = {"config": {
        "from_date": from_date, "to_date": to_date,
        "force": True,
    }}
    if country:
        config["ops"]["manifest"]["config"]["country"] = country
    if first_level:
        config["ops"]["manifest"]["config"]["first_level"] = first_level
    if district:
        config["ops"]["manifest"]["config"]["district"] = district
    if person_id_list:
        config["ops"]["manifest"]["config"]["person_ids"] = person_id_list
    if type_list:
        config["ops"]["manifest"]["config"]["item_types"] = type_list

    config["ops"]["edl"] = {"config": {
        "style": style, "target_duration": duration, "focus": focus,
    }}

    _submit("full_pipeline", _run_name(ctx), config)


@cli.command()
@click.option("--style", default="upbeat", help="Vlog style: upbeat, reflective, cinematic")
@click.option("--duration", default=180, type=int, help="Target duration in seconds")
@click.option("--focus", default="happiness with family", help="What to emphasize")
@click.pass_context
def plan(ctx, style, duration, focus):
    """Force re-plan + re-assemble (downstream)."""
    _submit("from_plan", _run_name(ctx), {
        "ops": {
            "edl": {"config": {"style": style, "target_duration": duration, "focus": focus, "force": True}},
            "vlog_video": {"config": {"force": True}},
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
            "vlog_video": {"config": {"version": version, "force": True}},
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


if __name__ == "__main__":
    cli()
