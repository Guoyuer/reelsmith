"""Build multimodal content for Gemini visual planning.

Produces a flat list of interleaved text metadata + inline photo
thumbnails + concatenated video mega-preview (Files API).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from pathlib import Path

from .. import constants as C
from .._types import VIDEO_EXTENSIONS, AnalysisEntry, cache_id
from ..config import Config
from ..utils.media import probe_duration, run_subprocess
from ._prompts import _secs_to_timestamp

logger = logging.getLogger("vlog.plan")


# ---------------------------------------------------------------------------
# Burst photo dedup (HSV histogram similarity)
# ---------------------------------------------------------------------------


def _photo_histogram(path: Path) -> list[int] | None:
    """Compute HSV histogram from a thumbnail (64x64)."""
    try:
        from PIL import Image

        return (
            Image.open(path)
            .convert("HSV")
            .resize((C.DEDUP_THUMB_SIZE, C.DEDUP_THUMB_SIZE))
            .histogram()
        )
    except Exception:
        return None


def _histogram_similarity(h1: list[int], h2: list[int]) -> float:
    """Cosine similarity between two PIL histograms."""
    dot = sum(a * b for a, b in zip(h1, h2))
    mag1 = math.sqrt(sum(a * a for a in h1))
    mag2 = math.sqrt(sum(b * b for b in h2))
    if mag1 == 0 or mag2 == 0:
        return 0.0
    return dot / (mag1 * mag2)


def _group_by_timestamp(
    photos: list[AnalysisEntry],
    window_secs: float,
) -> list[list[AnalysisEntry]]:
    """Group a pre-sorted list of photos into burst groups.

    Consecutive photos within ``window_secs`` of each other are placed in the
    same burst group.  Returns a list of groups (each group is a non-empty list).
    """
    from datetime import datetime

    bursts: list[list[AnalysisEntry]] = [[photos[0]]]
    for photo in photos[1:]:
        prev_t = bursts[-1][-1]["taken_at"]
        curr_t = photo["taken_at"]
        try:
            t1 = datetime.fromisoformat(prev_t.replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(curr_t.replace("Z", "+00:00"))
            if abs((t2 - t1).total_seconds()) <= window_secs:
                bursts[-1].append(photo)
            else:
                bursts.append([photo])
        except (ValueError, TypeError):
            bursts.append([photo])
    return bursts


def _select_from_bursts(
    bursts: list[list[AnalysisEntry]],
    thumbnails_dir: Path,
    threshold: float,
) -> tuple[list[AnalysisEntry], int]:
    """For each burst group, cluster by histogram similarity and keep best photo.

    Returns ``(kept_photos, removed_count)``.
    """
    kept = []
    removed_total = 0
    for burst in bursts:
        if len(burst) <= 1:
            kept.extend(burst)
            continue

        # Load histograms from thumbnails (fast — 400px already cached)
        hists = []
        for photo in burst:
            thumb = (
                thumbnails_dir / f"{Path(photo.get('local_path', '')).stem}_thumb.jpg"
            )
            hists.append(_photo_histogram(thumb) if thumb.exists() else None)

        # Cluster similar photos
        used = [False] * len(burst)
        for i in range(len(burst)):
            if used[i]:
                continue
            cluster = [i]
            used[i] = True
            if hists[i] is not None:
                for j in range(i + 1, len(burst)):
                    if used[j] or hists[j] is None:
                        continue
                    if _histogram_similarity(hists[i], hists[j]) > threshold:
                        cluster.append(j)
                        used[j] = True

            # Keep best from cluster (largest file)
            best = max(
                cluster,
                key=lambda k: burst[k].get("filesize", 0),
            )
            kept.append(burst[best])
            if len(cluster) > 1:
                removed = [
                    Path(burst[k]["local_path"]).name for k in cluster if k != best
                ]
                removed_total += len(removed)
                logger.debug(
                    "  Burst dedup: kept %s, removed %d: %s%s",
                    Path(burst[best]["local_path"]).name,
                    len(removed),
                    ", ".join(removed[:3]),
                    "..." if len(removed) > 3 else "",
                )

    return kept, removed_total


def _dedup_burst_photos(
    items: list[AnalysisEntry],
    thumbnails_dir: Path,
    threshold: float = C.BURST_SIMILARITY_THRESHOLD,
) -> list[AnalysisEntry]:
    """Remove near-identical burst photos before sending to Gemini.

    Two-pass: group by 10s time window, then compare histograms within each
    group. Keeps the photo with the largest file size.
    Videos pass through untouched.
    """
    photos = []
    others = []
    for entry in items:
        suffix = Path(entry["local_path"]).suffix.lower()
        if suffix not in VIDEO_EXTENSIONS and entry["media_type"] != "video":
            photos.append(entry)
        else:
            others.append(entry)

    if len(photos) < 2:
        return items

    photos.sort(key=lambda x: x["taken_at"])

    bursts = _group_by_timestamp(photos, C.BURST_WINDOW_SECS)
    kept, removed_total = _select_from_bursts(bursts, thumbnails_dir, threshold)

    if removed_total:
        removed_bytes = 0
        kept_paths = {e["local_path"] for e in kept}
        for p in photos:
            if p["local_path"] not in kept_paths:
                thumb = thumbnails_dir / f"{Path(p['local_path']).stem}_thumb.jpg"
                if thumb.exists():
                    removed_bytes += thumb.stat().st_size
        logger.info(
            "  Burst dedup: %d → %d photos (%d removed, %.1fMB saved)",
            len(photos),
            len(kept),
            removed_total,
            removed_bytes / 1024 / 1024,
        )

    return kept + others


def _build_item_text(idx: int, entry: AnalysisEntry) -> tuple[str, Path | None]:
    """Build text metadata for one item. Returns (text_line, photo_path_or_None)."""
    local_path = entry["local_path"]
    media = entry["media_type"]

    label = f"#{idx:02d}:"
    parts = [label]

    item_loc = entry.get("district") or entry.get("first_level") or entry.get("country")
    if item_loc:
        parts.append(f"at={item_loc}")

    photo_path = None
    if media == "video":
        duration = entry.get("video_duration", 0)
        duration_str = f"{duration:.0f}s" if duration else "?"
        video_width = entry.get("video_width", 0)
        video_height = entry.get("video_height", 0)
        res_str = (
            f" {video_width}x{video_height}" if video_width and video_height else ""
        )
        orient = entry.get("video_orientation", "")
        orient_str = f"({orient})" if orient == "portrait" else ""
        video_fps = entry.get("video_fps", 0)
        fps_str = f" {video_fps}fps" if video_fps and video_fps >= 48 else ""
        parts.append(f"video={duration_str}{res_str}{orient_str}{fps_str}")
    else:
        exif_data = entry.get("exif", {})
        if exif_data:
            exif_parts = []
            focal_length = exif_data.get("focal_length")
            aperture = exif_data.get("aperture")
            iso = exif_data.get("iso_speed")
            if focal_length:
                exif_parts.append(f"{focal_length:.0f}mm")
            if aperture:
                exif_parts.append(f"f/{aperture:.1f}")
            if iso:
                exif_parts.append(f"ISO{iso}")
            if exif_parts:
                parts.append(" ".join(exif_parts))
        photo_path = Path(local_path)

    parts.append(f"file={Path(local_path).name}")
    return " ".join(parts), photo_path


def _build_offset_table(
    video_entries: list[tuple[int, float, Path]],
) -> tuple[list[tuple[int, float, float]], list[tuple[int, float, Path]]]:
    """Probe actual durations of preview clips and build the offset table.

    Entries whose duration cannot be determined are silently skipped with a
    warning logged.

    Returns ``(offset_table, valid_entries)`` where:
    - ``offset_table`` is a list of ``(item_num, actual_duration, offset)``
    - ``valid_entries`` is a list of ``(item_num, actual_duration, preview_path)``
    """
    offset = 0.0
    offset_table: list[tuple[int, float, float]] = []
    valid_entries: list[tuple[int, float, Path]] = []
    for item_num, _meta_duration, preview_path in video_entries:
        actual_duration = probe_duration(preview_path)
        if actual_duration <= 0:
            logger.warning("  Skipping preview #%s: could not probe duration", item_num)
            continue
        offset_table.append((item_num, actual_duration, offset))
        valid_entries.append((item_num, actual_duration, preview_path))
        offset += actual_duration
    return offset_table, valid_entries


def _concat_previews(
    video_entries: list[tuple[int, float, Path]],
    output_path: Path,
) -> tuple[list[tuple[int, float, float]], Path]:
    """Concatenate video previews into one mega-preview with burned-in labels."""
    import tempfile

    # Use actual preview clip durations (not metadata) for accurate offsets
    offset_table, valid_entries = _build_offset_table(video_entries)
    offset = sum(dur for _, dur, _ in offset_table)

    with tempfile.TemporaryDirectory() as td:
        concat_file = Path(td) / "concat.txt"
        with open(concat_file, "w") as f:
            for _, duration, preview_path in valid_entries:
                safe = str(preview_path.resolve()).replace("\\", "/")
                f.write(f"file '{safe}'\n")
                f.write(f"duration {duration}\n")

        drawtext_parts = []
        for item_num, duration, seg_offset in offset_table:
            label = f"\\\\\\#{item_num}"
            end = seg_offset + duration
            drawtext_parts.append(
                f"drawtext=text='{label}'"
                f":fontsize=36:fontcolor=yellow"
                f":box=1:boxcolor=black@0.8:boxborderw=8:x=10:y=8"
                f":enable='between(t,{seg_offset:.1f},{end:.1f})'"
            )
        vf = ",".join(drawtext_parts) if drawtext_parts else "null"

        logger.info(
            "Building mega-preview (%d videos, %.0fs)...", len(video_entries), offset
        )
        run_subprocess(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_file),
                "-vf",
                vf,
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "34",
                "-c:a",
                "aac",
                "-b:a",
                "64k",
                "-ac",
                "1",
                str(output_path),
            ],
            capture_output=True,
            text=True,
            timeout=600,
        )

    size_mb = output_path.stat().st_size / 1024 / 1024 if output_path.exists() else 0
    logger.info("  Mega-preview: %.1fMB", size_mb)

    if output_path.exists():
        actual_duration = probe_duration(output_path)
        if offset > 0 and actual_duration / offset < 0.5:
            raise RuntimeError(
                f"Mega-preview duration mismatch: expected ~{offset:.0f}s, got {actual_duration:.0f}s (<50%)"
            )
        if abs(actual_duration - offset) > 5:
            logger.warning(
                "Mega-preview duration drift: expected %.0fs, got %.0fs",
                offset,
                actual_duration,
            )

    return offset_table, output_path


def _collect_items(
    analysis_by_path: dict[str, AnalysisEntry],
    cfg: Config,
    preview_dir: Path,
) -> tuple[str, list[Path], list[tuple[int, float, Path]], int, int]:
    """Iterate all items, build text lines, collect photo paths and video entries.

    Returns (text_block, photo_paths, video_entries, n_photos, n_videos).
    """
    all_items = sorted(analysis_by_path.values(), key=lambda entry: entry["local_path"])
    all_items = _dedup_burst_photos(all_items, cfg.thumbnails_dir)

    lines: list[str] = []
    photo_paths: list[Path] = []
    video_entries: list[tuple[int, float, Path]] = []
    idx = 1
    n_photos = 0
    n_videos = 0

    for entry in all_items:
        local_path = entry["local_path"]
        if not local_path or not Path(local_path).exists():
            continue

        text_line, photo_path = _build_item_text(idx, entry)
        lines.append(text_line)

        if photo_path:
            photo_paths.append(photo_path)
            n_photos += 1
        else:
            # Video — collect for mega-preview
            duration = entry.get("video_duration", 0)
            if duration > 0:
                preview_path = (
                    preview_dir / f"preview_{cache_id(entry['local_path'])}.mp4"
                )
                if preview_path.exists() and preview_path.stat().st_size > 500:
                    video_entries.append((idx, duration, preview_path))
            n_videos += 1

        idx += 1

    header = f"--- All media ({n_photos} photos, {n_videos} videos) ---"
    text_block = header + "\n" + "\n".join(lines)
    return text_block, photo_paths, video_entries, n_photos, n_videos


def _build_mega_preview(
    video_entries: list[tuple[int, float, Path]],
    preview_dir: Path,
    *,
    force: bool = False,
) -> tuple[list[tuple[int, float, float]], Path | None]:
    """Handle mega-preview caching, concatenation, and offset table.

    Returns (offset_table, mega_path or None).
    """
    if not video_entries:
        return [], None

    mega_path = preview_dir / "_mega_preview.mp4"
    meta_path = preview_dir / "_mega_preview.json"

    if force:
        if mega_path.exists():
            mega_path.unlink()
        if meta_path.exists():
            meta_path.unlink()
        logger.info("Force: deleted cached mega-preview")

    cache_key = hashlib.md5(
        str([(n, p.name, d) for n, d, p in video_entries]).encode()
    ).hexdigest()

    cached_meta = None
    if mega_path.exists() and meta_path.exists():
        try:
            cached_meta = json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError):
            logger.debug(
                "Could not read mega-preview cache %s", meta_path, exc_info=True
            )

    if cached_meta and cached_meta.get("key") == cache_key:
        logger.info("Mega-preview cached (%d videos)", len(video_entries))
        offset_table = [tuple(e) for e in cached_meta["offset_table"]]
    else:
        offset_table, mega_path = _concat_previews(video_entries, mega_path)
        meta_path.write_text(
            json.dumps({"key": cache_key, "offset_table": offset_table})
        )

    return offset_table, mega_path


def _build_visual_content_blocks(
    analysis_by_path: dict[str, AnalysisEntry],
    cfg: Config,
    *,
    force: bool = False,
) -> tuple[list, list[tuple[int, float, float]], int, int]:
    """Build flat multimodal content: text metadata + photos + mega video preview.

    Items are sent as a flat numbered list.
    Gemini sees all photos/videos and decides the narrative structure.

    Returns (blocks, offset_table, n_photos, n_videos).
    """
    blocks: list = []
    preview_dir = cfg.previews_dir

    # --- Phase 1: collect items ---
    text_block, photo_paths, video_entries, n_photos, n_videos = _collect_items(
        analysis_by_path, cfg, preview_dir
    )
    blocks.append(text_block)

    # --- Phase 2: add photo thumbnails inline ---
    for photo in photo_paths:
        thumb = cfg.thumbnails_dir / f"{photo.stem}_thumb.jpg"
        if not thumb.exists():
            raise FileNotFoundError(
                f"Thumbnail missing for {photo.name} — run prepare first"
            )
        blocks.append(
            {
                "type": "image_bytes",
                "mime_type": "image/jpeg",
                "data": thumb.read_bytes(),
            }
        )

    photo_bytes = sum(
        len(b.get("data", b""))
        for b in blocks
        if isinstance(b, dict) and b.get("type") == "image_bytes"
    )
    logger.info(
        "Photo thumbnails: %d files, %.1fMB",
        n_photos,
        photo_bytes / 1024 / 1024,
    )

    # --- Phase 3: build mega-preview and inject timestamps ---
    offset_table, mega_path = _build_mega_preview(
        video_entries, preview_dir, force=force
    )

    if offset_table and mega_path:
        # Inject preview timestamps into text block
        preview_ranges: dict[int, str] = {}
        for item_num, dur, offset in offset_table:
            start_ts = _secs_to_timestamp(offset)
            end_ts = _secs_to_timestamp(offset + dur)
            preview_ranges[item_num] = f"{start_ts}-{end_ts}"

        # Update text block with preview ranges
        updated_lines = []
        for line in text_block.split("\n"):
            m = re.match(r"#(\d+):", line)
            if m:
                num = int(m.group(1))
                if num in preview_ranges:
                    line = line + f" preview={preview_ranges[num]}"
            updated_lines.append(line)

        # Replace text block and add video intro
        blocks[0] = "\n".join(updated_lines)
        blocks.append(
            "--- VIDEO PREVIEW ---\n"
            "All video clips are concatenated below. Each clip has its item number "
            "(e.g. #30) burned into the top-left corner. Use the preview=MM:SS-MM:SS "
            "range in each video's metadata to locate it in the preview.\n"
            "When selecting a video clip, set preview_start and preview_end to the "
            "exact MM:SS timestamps in THIS preview video where the moment you want "
            "begins and ends."
        )
        blocks.append(
            {
                "type": "video_bytes",
                "mime_type": "video/mp4",
                "data": mega_path.read_bytes(),
            }
        )

    # --- Phase 4: validate ---
    n_images = sum(
        1
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "image_bytes"
    )

    if n_images == 0:
        raise RuntimeError("No photos generated — check source files")

    text_item_nums = set()
    for block in blocks:
        if isinstance(block, str):
            text_item_nums.update(int(m) for m in re.findall(r"#(\d+):", block))
    if len(text_item_nums) == 0:
        raise RuntimeError("No items (#XX) found in text metadata")

    video_nums_in_mega = (
        {num for num, _, _ in video_entries} if video_entries else set()
    )
    missing_in_text = video_nums_in_mega - text_item_nums
    if missing_in_text:
        raise RuntimeError(
            f"{len(missing_in_text)} video labels not in text metadata: "
            f"{sorted(missing_in_text)[:10]}. Numbering out of sync."
        )

    file_pattern = re.compile(r"file=(\S+)")
    n_files = 0
    for block in blocks:
        if isinstance(block, str):
            n_files += len(file_pattern.findall(block))
    if n_files == 0 and len(text_item_nums) > 0:
        raise RuntimeError("No file= references found in text metadata")

    inline_bytes = sum(
        len(block.get("data", b""))
        for block in blocks
        if isinstance(block, dict) and block.get("type") == "image_bytes"
    )
    if inline_bytes > 75 * 1024 * 1024:
        raise RuntimeError(
            f"Inline image payload {inline_bytes / 1024 / 1024:.0f}MB exceeds ~75MB limit "
            f"(100MB base64). Reduce photo count or thumbnail size."
        )

    return blocks, offset_table, n_photos, n_videos
