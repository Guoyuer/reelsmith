"""Workspace disk-usage inspection and cleanup command."""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import click

from ._commands import cli

logger = logging.getLogger("reelsmith.workspace")

_SHARED_DIRS = (
    ("media", "Source photos & videos"),
    ("music", "Generated music (Lyria cache)"),
    ("previews", "Video preview clips (shared)"),
    ("thumbnails", "Photo thumbnails"),
    ("heic_converted", "HEIC\u2192JPEG conversions"),
)

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


def _version_from_name(path: Path) -> int:
    match = re.search(r"_v(\d+)", path.stem)
    return int(match.group(1)) if match else -1


def _edl_summary(edl_path: Path) -> dict[str, Any]:
    data = json.loads(edl_path.read_text())
    segs = data.get("segments", [])
    items = [i for s in segs for i in s.get("items", [])]
    return {
        "edl_latest": _version_from_name(edl_path),
        "title": data.get("title", ""),
        "segments": len(segs),
        "items": len(items),
        "target_duration": data.get("target_duration", 0),
        "language": data.get("language", "en"),
        "n_videos": sum(1 for i in items if i.get("media_type") == "video"),
        "n_keep_audio": sum(1 for i in items if i.get("keep_audio")),
    }


def _output_details(output_dir: Path) -> list[dict[str, Any]]:
    if not output_dir.exists():
        return []
    versioned = [
        (path, _version_from_name(path)) for path in output_dir.glob("reelsmith_v*.mp4")
    ]
    return [
        {"path": path, "version": version, "size": path.stat().st_size}
        for path, version in sorted(versioned, key=lambda x: x[1])
        if version >= 0
    ]


def _intermediate_files(output_dir: Path) -> list[Path]:
    if not output_dir.exists():
        return []
    patterns = ("*_nomix.mp4", "*_speech.wav", "_group_*.mp4", "_group_*.txt")
    return [path for pattern in patterns for path in output_dir.glob(pattern)]


def _manifest_media_counts(runs_dir: Path) -> tuple[int, int]:
    """Return photo/video counts from the first available manifest."""
    if not runs_dir.exists():
        return 0, 0
    for mf in runs_dir.rglob("manifest.json"):
        try:
            manifest = json.loads(mf.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.debug("Skipping unreadable manifest %s: %s", mf, e)
            continue
        n_photos = n_videos = 0
        for item in manifest:
            item_type = item.get("item_type", item.get("type", 0))
            if item_type == 0:
                n_photos += 1
            elif item_type == 1:
                n_videos += 1
        return n_photos, n_videos
    return 0, 0


def _run_detail(run_dir: Path) -> dict[str, Any]:
    info: dict[str, Any] = {"name": run_dir.name, "path": run_dir}
    size, count = _dir_size(run_dir)
    info["size"] = size
    info["file_count"] = count
    info["last_used"] = _latest_mtime(run_dir)

    edls = sorted(run_dir.glob("edl_v*.json"), key=_version_from_name)
    info["edl_versions"] = len(edls)
    if edls:
        try:
            info.update(_edl_summary(edls[-1]))
        except (json.JSONDecodeError, OSError, KeyError, TypeError) as e:
            logger.debug("Skipping unreadable EDL %s: %s", edls[-1], e)

    output_dir = run_dir / "output"
    info["outputs"] = _output_details(output_dir)
    info["old_output_bytes"] = (
        sum(o["size"] for o in info["outputs"][:-1]) if len(info["outputs"]) > 1 else 0
    )

    intermediates = _intermediate_files(output_dir)
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


def _collect_runs(runs_dir: Path) -> list[dict[str, Any]]:
    if not runs_dir.exists():
        return []
    runs = [_run_detail(path) for path in sorted(runs_dir.iterdir()) if path.is_dir()]
    return sorted(runs, key=lambda r: r["last_used"], reverse=True)


def _print_shared_data(ws: Path) -> int:
    click.echo("\n=== Workspace ===\n")
    click.echo("Shared data:")

    total = 0
    media_size, media_count = _dir_size(ws / "media")
    total += media_size
    if media_size > 0:
        n_photos, n_videos = _manifest_media_counts(ws / "runs")
        media_detail = f"{media_count} files"
        if n_photos or n_videos:
            media_detail = f"{n_photos} photos, {n_videos} videos"
        click.echo(f"  {_fmt_size(media_size):>8s}  {media_detail:>22s}  Source media")

    for key, label in _SHARED_DIRS[1:]:
        size, count = _dir_size(ws / key)
        total += size
        if size > 0:
            click.echo(f"  {_fmt_size(size):>8s}  {count:>17d} files  {label}")

    return total


def _print_run_summary(runs: list[dict[str, Any]]) -> int:
    runs_total = sum(r["size"] for r in runs)
    click.echo(f"\nRuns: {_fmt_size(runs_total)} across {len(runs)} runs")
    click.echo()

    total_reclaimable = 0
    for run in runs:
        total_reclaimable += _print_run_detail(run)
    return total_reclaimable


def _print_run_detail(run: dict[str, Any]) -> int:
    age = _age_str(run["last_used"]) if run["last_used"] else "empty"
    click.echo(f"  {run['name']} ({_fmt_size(run['size'])}, {age})")

    if "edl_latest" in run:
        edl_parts = [
            f"v{run['edl_latest']}: {run['segments']} segments, {run['items']} items"
        ]
        if run.get("n_videos"):
            edl_parts.append(f"{run['n_videos']} videos")
        if run.get("n_keep_audio"):
            edl_parts.append(f"{run['n_keep_audio']} keep_audio")
        edl_parts.append(f"~{run['target_duration']}s")
        if run.get("language", "en") != "en":
            edl_parts.append(f"lang={run['language']}")
        click.echo(f"    EDL {', '.join(edl_parts)}")
        if run.get("title"):
            click.echo(f'    "{run["title"][:60]}"')
    elif run["edl_versions"] > 0:
        click.echo(f"    EDL: {run['edl_versions']} version(s)")

    if run["outputs"]:
        parts = []
        for output in run["outputs"]:
            marker = ""
            if output is run["outputs"][-1] and len(run["outputs"]) > 1:
                marker = " <-- latest"
            parts.append(f"v{output['version']} ({_fmt_size(output['size'])}){marker}")
        click.echo(f"    Output: {', '.join(parts)}")
    else:
        click.echo("    Output: (none)")

    if run["render_count"] > 0:
        click.echo(
            f"    Render: {run['render_count']} cached ({_fmt_size(run['render_size'])})"
        )

    reclaim_parts = []
    if run["old_output_bytes"]:
        reclaim_parts.append(f"{_fmt_size(run['old_output_bytes'])} old outputs")
    if run["intermediate_bytes"]:
        reclaim_parts.append(f"{_fmt_size(run['intermediate_bytes'])} intermediates")
    if not reclaim_parts:
        return 0

    reclaimable = run["old_output_bytes"] + run["intermediate_bytes"]
    click.echo(f"    Prune: {', '.join(reclaim_parts)}")
    return reclaimable


def _safe_clean_files(runs: list[dict[str, Any]]) -> list[Path]:
    files: list[Path] = []
    for run in runs:
        if run["old_output_bytes"]:
            files += [output["path"] for output in run["outputs"][:-1]]
        files += run["intermediate_files"]
    return files


def _clean_safe(runs: list[dict[str, Any]], total_reclaimable: int, yes: bool) -> None:
    if total_reclaimable == 0:
        click.echo("\nNothing to clean.")
        return

    click.echo(f"\nWill free {_fmt_size(total_reclaimable)}:")
    to_delete = _safe_clean_files(runs)
    for run in runs:
        files: list[Path] = []
        if run["old_output_bytes"]:
            files += [output["path"] for output in run["outputs"][:-1]]
        files += run["intermediate_files"]
        if files:
            size = sum(path.stat().st_size for path in files)
            click.echo(f"  {run['name']}: {len(files)} files ({_fmt_size(size)})")

    if not yes:
        click.confirm("Proceed?", abort=True)

    freed = 0
    for path in to_delete:
        freed += path.stat().st_size
        path.unlink()
    click.echo(f"Cleaned {len(to_delete)} files, freed {_fmt_size(freed)}.")


def _cleanup_targets(ws: Path, clean: str) -> list[tuple[str, Path]]:
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
    return targets


def _clean_targets(ws: Path, clean: str, yes: bool) -> None:
    import shutil

    sizes = [
        (name, path, _dir_size(path)[0])
        for name, path in _cleanup_targets(ws, clean)
        if path.exists()
    ]
    if not sizes:
        click.echo("Nothing to clean.")
        return

    total_clean = sum(size for _, _, size in sizes)
    click.echo(f"\nWill delete {_fmt_size(total_clean)}:")
    for name, path, size in sizes:
        click.echo(f"  {_fmt_size(size):>8s}  {path}")

    if not yes:
        click.confirm("Proceed?", abort=True)

    for name, path, _ in sizes:
        shutil.rmtree(path, ignore_errors=True)
        click.echo(f"  Deleted {path}")
    click.echo("Done.")


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
    ws = Path("./workspace")
    if not ws.exists():
        click.echo("No workspace directory found.")
        return

    runs_dir = ws / "runs"
    runs = _collect_runs(runs_dir)
    total = _print_shared_data(ws)
    runs_total = sum(r["size"] for r in runs)
    total += runs_total
    total_reclaimable = _print_run_summary(runs)

    click.echo(f"\n{'─' * 50}")
    click.echo(f"Total: {_fmt_size(total)}")
    if total_reclaimable:
        click.echo(f"Reclaimable with --clean safe: {_fmt_size(total_reclaimable)}")

    if clean is None:
        return

    if clean == "safe":
        _clean_safe(runs, total_reclaimable, yes)
        return

    _clean_targets(ws, clean, yes)
