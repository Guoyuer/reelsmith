"""CLI entry point for the vlog pipeline (Dagster-orchestrated).

Each command materializes Dagster assets. Assets auto-skip when their output
file exists (unless force=True). Use `dagster-webserver -m pipeline.definitions`
for the web UI.
"""

from __future__ import annotations

import os

import click
import dagster as dg


def _workspace(ctx: click.Context) -> str:
    return ctx.obj["workspace"] or os.getenv("WORKSPACE", "./workspace")


def _materialize(workspace: str, selection=None, run_config=None):
    """Materialize assets with the given workspace path."""
    from pipeline.definitions import (
        manifest, preprocessed, analysis, edl, vlog_video,
        WorkspaceIOManager,
    )

    result = dg.materialize(
        [manifest, preprocessed, analysis, edl, vlog_video],
        resources={"io_manager": WorkspaceIOManager(workspace=workspace)},
        selection=selection,
        run_config=run_config,
    )
    if not result.success:
        raise SystemExit(1)


@click.group()
@click.option("--workspace", "-w", default=None, help="Workspace directory (default: ./workspace)")
@click.pass_context
def cli(ctx: click.Context, workspace: str | None) -> None:
    """Automated vlog pipeline: fetch -> preprocess -> analyze -> plan -> assemble -> iterate."""
    ctx.ensure_object(dict)
    ctx.obj["workspace"] = workspace


@cli.command()
@click.pass_context
def resume(ctx):
    """Resume pipeline — auto-skips stages with existing outputs."""
    _materialize(_workspace(ctx))


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
    from pipeline.definitions import FetchConfig, PlanConfig

    person_id_list = [int(x) for x in person_ids.split(",")] if person_ids else None
    type_list = [int(x) for x in item_types.split(",")] if item_types else None

    _materialize(
        _workspace(ctx),
        run_config=dg.RunConfig(
            ops={
                "manifest": FetchConfig(
                    from_date=from_date, to_date=to_date,
                    country=country, first_level=first_level, district=district,
                    person_ids=person_id_list, item_types=type_list,
                    force=True,
                ),
                "edl": PlanConfig(style=style, target_duration=duration, focus=focus),
            },
        ),
    )


@cli.command()
@click.option("--style", default="upbeat", help="Vlog style: upbeat, reflective, cinematic")
@click.option("--duration", default=180, type=int, help="Target duration in seconds")
@click.option("--focus", default="happiness with family", help="What to emphasize")
@click.pass_context
def plan(ctx, style, duration, focus):
    """Force re-plan + re-assemble (downstream)."""
    from pipeline.definitions import PlanConfig, AssembleConfig

    _materialize(
        _workspace(ctx),
        selection=["edl", "vlog_video"],
        run_config=dg.RunConfig(
            ops={
                "edl": PlanConfig(style=style, target_duration=duration, focus=focus, force=True),
                "vlog_video": AssembleConfig(force=True),
            },
        ),
    )


@cli.command()
@click.option("--version", "-v", default=None, type=int, help="Version number")
@click.pass_context
def assemble(ctx, version):
    """Force re-assemble the vlog from current EDL."""
    from pipeline.definitions import AssembleConfig
    from pipeline.config import Config as PipelineConfig
    from pipeline.iterate import _find_latest_version

    ws = _workspace(ctx)
    if version is None:
        cfg = PipelineConfig.load(ws)
        version = _find_latest_version(cfg) + 1

    _materialize(
        ws,
        selection=["vlog_video"],
        run_config=dg.RunConfig(
            ops={"vlog_video": AssembleConfig(version=version, force=True)},
        ),
    )


@cli.command()
@click.option("--feedback", default=None, help="Natural language feedback to apply")
@click.option("--rounds", default=2, type=int, help="Self-critique rounds (if no feedback given)")
@click.option("--style", default="upbeat", help="Style for self-critique")
@click.pass_context
def iterate(ctx, feedback, rounds, style):
    """Improve the vlog via self-critique or human feedback."""
    from pipeline.definitions import IterateConfig, iterate_job

    ws = _workspace(ctx)
    iterate_job.execute_in_process(
        run_config=dg.RunConfig(
            ops={
                "iterate_op": IterateConfig(
                    workspace=ws,
                    style=style,
                    max_rounds=rounds,
                    feedback=feedback,
                ),
            },
        ),
    )


@cli.command()
@click.option("--styles", default="energetic,reflective,cinematic", help="Comma-separated variation styles")
@click.pass_context
def variations(ctx, styles):
    """Generate multiple vlog variations with different styles."""
    from pipeline.iterate import generate_variations
    from pipeline.config import Config as PipelineConfig

    cfg = PipelineConfig.load(_workspace(ctx))
    style_list = [s.strip() for s in styles.split(",")]
    outputs = generate_variations(cfg, styles=style_list)
    for path in outputs:
        print(f"  {path}")


if __name__ == "__main__":
    cli()
