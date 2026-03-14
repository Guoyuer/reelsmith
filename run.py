"""CLI entry point for the vlog pipeline (Prefect-orchestrated)."""

from __future__ import annotations

import os

import click


def _workspace(ctx: click.Context) -> str:
    return ctx.obj["workspace"] or os.getenv("WORKSPACE", "./workspace")


@click.group()
@click.option("--workspace", "-w", default=None, help="Workspace directory (default: ./workspace)")
@click.pass_context
def cli(ctx: click.Context, workspace: str | None) -> None:
    """Automated vlog pipeline: fetch -> preprocess -> analyze -> plan -> assemble -> iterate."""
    ctx.ensure_object(dict)
    ctx.obj["workspace"] = workspace


@cli.command()
@click.option("--from-date", "-f", required=True, help="Start date (YYYY-MM-DD)")
@click.option("--to-date", "-t", required=True, help="End date (YYYY-MM-DD)")
@click.option("--country", default=None, help="Filter by country")
@click.option("--first-level", default=None, help="Filter by state/province")
@click.option("--district", default=None, help="Filter by district/city")
@click.option("--person-ids", default=None, help="Comma-separated person IDs")
@click.option("--item-types", default=None, help="Comma-separated item types (0=photo,1=video,3=live,6=motion)")
@click.pass_context
def fetch(ctx, from_date, to_date, country, first_level, district, person_ids, item_types):
    """Download media from Synology Photos (runs full pipeline from fetch)."""
    from pipeline.flows import vlog_pipeline_flow

    person_id_list = [int(x) for x in person_ids.split(",")] if person_ids else None
    type_list = [int(x) for x in item_types.split(",")] if item_types else None

    vlog_pipeline_flow(
        _workspace(ctx),
        start_from="fetch",
        from_date=from_date, to_date=to_date,
        country=country, first_level=first_level, district=district,
        person_ids=person_id_list, item_types=type_list,
        critique_rounds=0,
    )


@cli.command()
@click.option("--family", default=None, help="Comma-separated family member names (auto-detected if omitted)")
@click.pass_context
def preprocess(ctx, family):
    """Tier items by family presence, cluster duplicates, build timeline."""
    from pipeline.flows import vlog_pipeline_flow

    family_names = [n.strip() for n in family.split(",")] if family else None
    vlog_pipeline_flow(
        _workspace(ctx),
        start_from="preprocess",
        family_names=family_names,
        critique_rounds=0,
    )


@cli.command()
@click.pass_context
def analyze(ctx):
    """Analyze media with vision model (tiered: family photos first)."""
    from pipeline.flows import vlog_pipeline_flow

    vlog_pipeline_flow(_workspace(ctx), start_from="analyze", critique_rounds=0)


@cli.command()
@click.option("--style", default="upbeat", help="Vlog style: upbeat, reflective, cinematic")
@click.option("--duration", default=180, type=int, help="Target duration in seconds")
@click.option("--focus", default="happiness with family", help="What to emphasize")
@click.pass_context
def plan(ctx, style, duration, focus):
    """Generate edit decision list using local LLM."""
    from pipeline.flows import vlog_pipeline_flow

    vlog_pipeline_flow(
        _workspace(ctx),
        start_from="plan",
        style=style, target_duration=duration, focus=focus,
        critique_rounds=0,
    )


@cli.command()
@click.option("--version", "-v", default=None, type=int, help="Version number (auto-increments if omitted)")
@click.pass_context
def assemble(ctx, version):
    """Render vlog from EDL."""
    from pipeline.flows import vlog_pipeline_flow
    from pipeline.config import Config
    from pipeline.iterate import _find_latest_version

    ws = _workspace(ctx)
    if version is None:
        cfg = Config.load(ws)
        version = _find_latest_version(cfg) + 1
    vlog_pipeline_flow(
        ws, start_from="assemble", assemble_version=version, critique_rounds=0,
    )


@cli.command()
@click.option("--feedback", default=None, help="Natural language feedback to apply")
@click.option("--rounds", default=2, type=int, help="Self-critique rounds (if no feedback given)")
@click.option("--style", default="upbeat", help="Style for self-critique")
@click.pass_context
def iterate(ctx, feedback, rounds, style):
    """Improve the vlog via self-critique or human feedback."""
    ws = _workspace(ctx)
    if feedback:
        from pipeline.flows import feedback_flow
        feedback_flow(ws, feedback)
    else:
        from pipeline.flows import vlog_pipeline_flow
        vlog_pipeline_flow(
            ws, start_from="iterate", style=style, critique_rounds=rounds,
        )


@cli.command()
@click.option("--styles", default="energetic,reflective,cinematic", help="Comma-separated variation styles")
@click.pass_context
def variations(ctx, styles):
    """Generate multiple vlog variations with different styles."""
    from pipeline.flows import variations_flow

    style_list = [s.strip() for s in styles.split(",")]
    outputs = variations_flow(_workspace(ctx), style_list)
    for path in outputs:
        print(f"  {path}")


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
@click.option("--critique-rounds", default=2, type=int, help="Self-critique rounds")
@click.pass_context
def auto(ctx, from_date, to_date, country, first_level, district, person_ids,
         item_types, style, duration, focus, critique_rounds):
    """Run the full pipeline end-to-end."""
    from pipeline.flows import vlog_pipeline_flow

    person_id_list = [int(x) for x in person_ids.split(",")] if person_ids else None
    type_list = [int(x) for x in item_types.split(",")] if item_types else None

    vlog_pipeline_flow(
        _workspace(ctx),
        start_from="fetch",
        from_date=from_date, to_date=to_date,
        country=country, first_level=first_level, district=district,
        person_ids=person_id_list, item_types=type_list,
        style=style, target_duration=duration, focus=focus,
        critique_rounds=critique_rounds,
    )


if __name__ == "__main__":
    cli()
