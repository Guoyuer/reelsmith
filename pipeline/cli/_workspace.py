"""Workspace disk-usage inspection and cleanup command."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import click

from ._commands import cli

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt_size(size_bytes: int) -> str:
    if size_bytes >= 1024**3:
        return f"{size_bytes / 1024**3:.1f} GB"
    if size_bytes >= 1024**2:
        return f"{size_bytes / 1024**2:.0f} MB"
    return f"{size_bytes / 1024:.0f} KB"


def _dir_size(path) -> tuple[int, int]:
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
    age = time.time() - mtime
    if age < 3600:
        return f"{max(1, int(age / 60))}m ago"
    if age < 86400:
        return f"{int(age / 3600)}h ago"
    return f"{int(age / 86400)}d ago"


def _latest_mtime(path) -> float:
    path = Path(path)
    latest = 0.0
    if path.exists():
        for f in path.rglob("*"):
            if f.is_file():
                latest = max(latest, f.stat().st_mtime)
    return latest


def _run_detail(run_dir: Path) -> dict[str, Any]:
    info: dict[str, Any] = {"name": run_dir.name, "path": run_dir}
    size, count = _dir_size(run_dir)
    info["size"] = size
    info["file_count"] = count
    info["last_used"] = _latest_mtime(run_dir)

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
                1
                for s in segs
                for i in s.get("items", [])
                if i.get("media_type") == "video"
            )
            info["n_keep_audio"] = sum(
                1 for s in segs for i in s.get("items", []) if i.get("keep_audio")
            )
        except Exception:
            pass

    output_dir = run_dir / "output"
    outputs = sorted(output_dir.glob("reelsmith_v*.mp4")) if output_dir.exists() else []
    info["outputs"] = [
        {"path": o, "version": int(o.stem.split("_v")[1].split("_")[0]), "size": o.stat().st_size}
        for o in outputs
    ]
    info["old_output_bytes"] = (
        sum(o["size"] for o in info["outputs"][:-1]) if len(info["outputs"]) > 1 else 0
    )

    intermediates = (
        (
            list(output_dir.glob("*_nomix.mp4"))
            + list(output_dir.glob("*_speech.wav"))
            + list(output_dir.glob("_group_*.mp4"))
            + list(output_dir.glob("_group_*.txt"))
        )
        if output_dir.exists()
        else []
    )
    info["intermediate_bytes"] = sum(f.stat().st_size for f in intermediates)
    info["intermediate_files"] = intermediates

    render_dir = run_dir / "render"
    render_size, render_count = _dir_size(render_dir)
    info["render_size"] = render_size
    info["render_count"] = render_count

    return info


# ---------------------------------------------------------------------------
# workspace command
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--clean",
    type=click.Choice(["safe", "cache", "media", "all"]),
    default=None,
    help="safe=old outputs+intermediates, cache=analysis/thumbnails, media=source files, all=everything",
)
@click.option("-y", "--yes", is_flag=True, help="Skip confirmation")
def workspace(clean, yes):
    """Show workspace disk usage with pipeline-aware details."""
    import shutil

    ws = Path("./workspace")
    if not ws.exists():
        click.echo("No workspace directory found.")
        return

    shared = [
        ("media", "Source photos & videos", ws / "media"),
        ("music", "Generated music (Lyria cache)", ws / "music"),
        ("previews", "Video preview clips (shared)", ws / "previews"),
        ("thumbnails", "Photo thumbnails", ws / "thumbnails"),
        ("heic_converted", "HEIC\u2192JPEG conversions", ws / "heic_converted"),
    ]

    total = 0
    click.echo("\n=== Workspace ===\n")
    click.echo("Shared data:")

    media_size, media_count = _dir_size(ws / "media")
    total += media_size
    if media_size > 0:
        n_photos = n_videos = 0
        for mf in (
            (ws / "runs").rglob("manifest.json") if (ws / "runs").exists() else []
        ):
            try:
                manifest = json.loads(mf.read_text())
                for item in manifest:
                    t = item.get("type", 0)
                    if t == 0:
                        n_photos += 1
                    elif t == 1:
                        n_videos += 1
                break
            except Exception:
                pass
        media_detail = f"{media_count} files"
        if n_photos or n_videos:
            media_detail = f"{n_photos} photos, {n_videos} videos"
        click.echo(f"  {_fmt_size(media_size):>8s}  {media_detail:>22s}  Source media")

    for key, label, path in shared[1:]:
        size, count = _dir_size(path)
        total += size
        if size > 0:
            click.echo(f"  {_fmt_size(size):>8s}  {count:>17d} files  {label}")

    runs_dir = ws / "runs"
    runs = []
    if runs_dir.exists():
        for d in sorted(runs_dir.iterdir()):
            if d.is_dir():
                runs.append(_run_detail(d))
        runs.sort(key=lambda r: r["last_used"], reverse=True)

    runs_total = sum(r["size"] for r in runs)
    total += runs_total

    click.echo(f"\nRuns: {_fmt_size(runs_total)} across {len(runs)} runs")
    click.echo()

    total_reclaimable = 0
    for r in runs:
        age = _age_str(r["last_used"]) if r["last_used"] else "empty"
        click.echo(f"  {r['name']} ({_fmt_size(r['size'])}, {age})")

        if "edl_latest" in r:
            edl_parts = [
                f"v{r['edl_latest']}: {r['segments']} segments, {r['items']} items"
            ]
            if r.get("n_videos"):
                edl_parts.append(f"{r['n_videos']} videos")
            if r.get("n_keep_audio"):
                edl_parts.append(f"{r['n_keep_audio']} keep_audio")
            edl_parts.append(f"~{r['target_duration']}s")
            if r.get("language", "en") != "en":
                edl_parts.append(f"lang={r['language']}")
            click.echo(f"    EDL {', '.join(edl_parts)}")
            if r.get("title"):
                click.echo(f'    "{r["title"][:60]}"')
        elif r["edl_versions"] > 0:
            click.echo(f"    EDL: {r['edl_versions']} version(s)")

        if r["outputs"]:
            parts = []
            for o in r["outputs"]:
                marker = ""
                if o is r["outputs"][-1] and len(r["outputs"]) > 1:
                    marker = " <-- latest"
                parts.append(f"v{o['version']} ({_fmt_size(o['size'])}){marker}")
            click.echo(f"    Output: {', '.join(parts)}")
        else:
            click.echo("    Output: (none)")

        if r["render_count"] > 0:
            click.echo(
                f"    Render: {r['render_count']} cached ({_fmt_size(r['render_size'])})"
            )

        reclaim_parts = []
        if r["old_output_bytes"]:
            reclaim_parts.append(f"{_fmt_size(r['old_output_bytes'])} old outputs")
        if r["intermediate_bytes"]:
            reclaim_parts.append(f"{_fmt_size(r['intermediate_bytes'])} intermediates")
        if reclaim_parts:
            r_total = r["old_output_bytes"] + r["intermediate_bytes"]
            total_reclaimable += r_total
            click.echo(f"    Prune: {', '.join(reclaim_parts)}")

    click.echo(f"\n{'─' * 50}")
    click.echo(f"Total: {_fmt_size(total)}")
    if total_reclaimable:
        click.echo(f"Reclaimable with --clean safe: {_fmt_size(total_reclaimable)}")

    if clean is None:
        return

    if clean == "safe":
        if total_reclaimable == 0:
            click.echo("\nNothing to clean.")
            return

        click.echo(f"\nWill free {_fmt_size(total_reclaimable)}:")
        to_delete: list[Path] = []
        for r in runs:
            files: list[Path] = []
            if r["old_output_bytes"]:
                files += [o["path"] for o in r["outputs"][:-1]]
            files += r["intermediate_files"]
            if files:
                click.echo(
                    f"  {r['name']}: {len(files)} files ({_fmt_size(sum(f.stat().st_size for f in files))})"
                )
                to_delete += files

        if not yes:
            click.confirm("Proceed?", abort=True)

        freed = 0
        for f in to_delete:
            freed += f.stat().st_size
            f.unlink()
        click.echo(f"Cleaned {len(to_delete)} files, freed {_fmt_size(freed)}.")
        return

    targets = []
    if clean in ("media", "all"):
        targets.append(("media", ws / "media"))
    if clean in ("cache", "all"):
        targets += [
            ("thumbnails", ws / "thumbnails"),
            ("previews", ws / "previews"),
            ("heic_converted", ws / "heic_converted"),
            ("music", ws / "music"),
        ]
    if clean == "all":
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
