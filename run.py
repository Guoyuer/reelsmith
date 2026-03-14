"""CLI entry point for the vlog pipeline."""

from __future__ import annotations

import click

from pipeline.config import Config


@click.group()
@click.option("--workspace", "-w", default=None, help="Workspace directory (default: ./workspace)")
@click.pass_context
def cli(ctx: click.Context, workspace: str | None) -> None:
    """Automated vlog pipeline: fetch → preprocess → analyze → plan → assemble → iterate."""
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
    """Download media from Synology Photos."""
    from pipeline.fetch import fetch as do_fetch

    cfg = Config.load(ctx.obj["workspace"])
    person_id_list = [int(x) for x in person_ids.split(",")] if person_ids else None
    type_list = [int(x) for x in item_types.split(",")] if item_types else None

    do_fetch(
        cfg,
        from_date=from_date,
        to_date=to_date,
        country=country,
        first_level=first_level,
        district=district,
        person_ids=person_id_list,
        item_types=type_list,
    )


@cli.command()
@click.option("--family", default=None, help="Comma-separated family member names (auto-detected if omitted)")
@click.pass_context
def preprocess(ctx, family):
    """Tier items by family presence, cluster duplicates, build timeline."""
    from pipeline.preprocess import preprocess as do_preprocess

    cfg = Config.load(ctx.obj["workspace"])
    family_names = [n.strip() for n in family.split(",")] if family else None
    do_preprocess(cfg, family_names=family_names)


@cli.command()
@click.pass_context
def analyze(ctx):
    """Analyze media with vision model (tiered: family photos first)."""
    from pipeline.analyze import analyze as do_analyze

    cfg = Config.load(ctx.obj["workspace"])
    do_analyze(cfg)


@cli.command()
@click.option("--style", default="upbeat", help="Vlog style: upbeat, reflective, cinematic")
@click.option("--duration", default=180, type=int, help="Target duration in seconds")
@click.option("--focus", default="happiness with family", help="What to emphasize")
@click.pass_context
def plan(ctx, style, duration, focus):
    """Generate edit decision list using local LLM."""
    from pipeline.plan import plan as do_plan

    cfg = Config.load(ctx.obj["workspace"])
    do_plan(cfg, style=style, target_duration=duration, focus=focus)


@cli.command()
@click.option("--version", "-v", default=None, type=int, help="Version number (auto-increments if omitted)")
@click.pass_context
def assemble(ctx, version):
    """Render vlog from EDL."""
    from pipeline.assemble import assemble as do_assemble
    from pipeline.iterate import _find_latest_version

    cfg = Config.load(ctx.obj["workspace"])
    if version is None:
        version = _find_latest_version(cfg) + 1
    do_assemble(cfg, version=version)


@cli.command()
@click.option("--feedback", default=None, help="Natural language feedback to apply")
@click.option("--rounds", default=2, type=int, help="Self-critique rounds (if no feedback given)")
@click.option("--style", default="upbeat", help="Style for self-critique")
@click.pass_context
def iterate(ctx, feedback, rounds, style):
    """Improve the vlog via self-critique or human feedback."""
    from pipeline.iterate import apply_feedback, self_critique

    cfg = Config.load(ctx.obj["workspace"])
    if feedback:
        apply_feedback(cfg, feedback)
    else:
        self_critique(cfg, style=style, max_rounds=rounds)


@cli.command()
@click.option("--styles", default="energetic,reflective,cinematic", help="Comma-separated variation styles")
@click.pass_context
def variations(ctx, styles):
    """Generate multiple vlog variations with different styles."""
    from pipeline.iterate import generate_variations

    cfg = Config.load(ctx.obj["workspace"])
    style_list = [s.strip() for s in styles.split(",")]
    outputs = generate_variations(cfg, styles=style_list)
    for path in outputs:
        print(f"  {path}")


@cli.command()
@click.option("--from-date", "-f", required=True, help="Start date (YYYY-MM-DD)")
@click.option("--to-date", "-t", required=True, help="End date (YYYY-MM-DD)")
@click.option("--country", default=None, help="Filter by country")
@click.option("--first-level", default=None, help="Filter by state/province")
@click.option("--district", default=None, help="Filter by district/city")
@click.option("--person-ids", default=None, help="Comma-separated person IDs")
@click.option("--style", default="upbeat", help="Vlog style")
@click.option("--duration", default=180, type=int, help="Target duration in seconds")
@click.option("--focus", default="happiness with family", help="What to emphasize")
@click.option("--critique-rounds", default=2, type=int, help="Self-critique rounds")
@click.pass_context
def auto(ctx, from_date, to_date, country, first_level, district, person_ids,
         style, duration, focus, critique_rounds):
    """Run the full pipeline end-to-end."""
    from pipeline.analyze import analyze as do_analyze
    from pipeline.assemble import assemble as do_assemble
    from pipeline.fetch import fetch as do_fetch
    from pipeline.iterate import self_critique
    from pipeline.plan import plan as do_plan
    from pipeline.preprocess import preprocess as do_preprocess

    cfg = Config.load(ctx.obj["workspace"])
    person_id_list = [int(x) for x in person_ids.split(",")] if person_ids else None

    print("=" * 60)
    print("STAGE 1: Fetch")
    print("=" * 60)
    do_fetch(
        cfg,
        from_date=from_date,
        to_date=to_date,
        country=country,
        first_level=first_level,
        district=district,
        person_ids=person_id_list,
    )

    print("\n" + "=" * 60)
    print("STAGE 2: Preprocess")
    print("=" * 60)
    do_preprocess(cfg)

    print("\n" + "=" * 60)
    print("STAGE 3: Analyze")
    print("=" * 60)
    do_analyze(cfg)

    print("\n" + "=" * 60)
    print("STAGE 4: Plan")
    print("=" * 60)
    do_plan(cfg, style=style, target_duration=duration, focus=focus)

    print("\n" + "=" * 60)
    print("STAGE 5: Assemble")
    print("=" * 60)
    do_assemble(cfg, version=1)

    if critique_rounds > 0:
        print("\n" + "=" * 60)
        print("STAGE 6: Self-Critique")
        print("=" * 60)
        self_critique(cfg, style=style, max_rounds=critique_rounds)

    print("\n" + "=" * 60)
    print("DONE")
    print("=" * 60)
    print(f"Output: {cfg.workspace / 'output'}")


if __name__ == "__main__":
    cli()
