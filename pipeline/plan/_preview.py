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

from ..config import Config
from ..media_utils import run_subprocess

logger = logging.getLogger("vlog.plan")

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v"}


# ---------------------------------------------------------------------------
# Burst photo dedup (HSV histogram similarity)
# ---------------------------------------------------------------------------


def _photo_histogram(path: Path) -> list[int] | None:
    """Compute RGB histogram from a thumbnail (64x64)."""
    try:
        from PIL import Image

        return Image.open(path).convert("RGB").resize((64, 64)).histogram()
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


def _dedup_burst_photos(
    items: list[dict], thumbnails_dir: Path, threshold: float = 0.92
) -> list[dict]:
    """Remove near-identical burst photos before sending to Gemini.

    Two-pass: group by 10s time window, then compare histograms within each
    group. Keeps the photo with highest family_count (then largest file).
    Videos pass through untouched.
    """
    photos = []
    others = []
    for a in items:
        suffix = Path(a.get("local_path", "")).suffix.lower()
        if suffix not in VIDEO_EXTENSIONS and a.get("media_type") != "video":
            photos.append(a)
        else:
            others.append(a)

    if len(photos) < 2:
        return items

    photos.sort(key=lambda x: x.get("taken_iso", "") or "")

    # Group consecutive photos within 10s (by filename timestamp as proxy)
    bursts: list[list[dict]] = [[photos[0]]]
    for p in photos[1:]:
        prev_t = bursts[-1][-1].get("taken_iso", "") or ""
        curr_t = p.get("taken_iso", "") or ""
        # Compare ISO timestamps: if within 10s, same burst
        try:
            from datetime import datetime

            t1 = datetime.fromisoformat(prev_t.replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(curr_t.replace("Z", "+00:00"))
            if abs((t2 - t1).total_seconds()) <= 10:
                bursts[-1].append(p)
            else:
                bursts.append([p])
        except (ValueError, TypeError):
            bursts.append([p])

    kept = []
    removed_total = 0
    for burst in bursts:
        if len(burst) <= 1:
            kept.extend(burst)
            continue

        # Load histograms from thumbnails (fast — 400px already cached)
        hists = []
        for p in burst:
            thumb = thumbnails_dir / f"{Path(p.get('local_path', '')).stem}_thumb.jpg"
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

            # Keep best from cluster
            best = max(
                cluster,
                key=lambda k: (burst[k].get("family_count", 0), burst[k].get("filesize", 0)),
            )
            kept.append(burst[best])
            if len(cluster) > 1:
                removed = [burst[k]["filename"] for k in cluster if k != best]
                removed_total += len(removed)
                logger.debug(
                    f"  Burst dedup: kept {burst[best]['filename']}, "
                    f"removed {len(removed)}: {', '.join(removed[:3])}"
                    f"{'...' if len(removed) > 3 else ''}"
                )

    if removed_total:
        logger.info(f"  Burst dedup: {len(photos)} → {len(kept)} photos ({removed_total} removed)")

    return kept + others


def _build_item_text(idx: int, a: dict) -> tuple[str, Path | None]:
    """Build text metadata for one item. Returns (text_line, photo_path_or_None)."""
    local_path = a.get("local_path", "")
    media = a.get("media_type", "photo")
    persons = a.get("persons", [])

    label = f"#{idx:02d}:"
    parts = [label]
    if a.get("family_count", 0) >= 2:
        who = f"family together ({','.join(persons[:3])})" if persons else "family together"
        parts.append(who)
    elif a.get("family_count", 0) == 1:
        parts.append(f"{persons[0]}" if persons else "one family member")
    elif persons:
        parts.append(f"people: {','.join(persons[:3])}")

    item_loc = a.get("district") or a.get("first_level") or a.get("country")
    if item_loc:
        parts.append(f"at={item_loc}")

    photo_path = None
    if media == "video":
        dur = a.get("video_duration") or (a.get("duration_ms", 0) / 1000)
        dur_s = f"{dur:.0f}s" if dur else "?"
        vw = a.get("video_width", 0)
        vh = a.get("video_height", 0)
        res_str = f" {vw}x{vh}" if vw and vh else ""
        orient = a.get("video_orientation", "")
        orient_str = f"({orient})" if orient == "portrait" else ""
        vfps = a.get("video_fps", 0)
        fps_str = f" {vfps}fps" if vfps and vfps >= 48 else ""
        parts.append(f"video={dur_s}{res_str}{orient_str}{fps_str}")
    else:
        exif_data = a.get("exif", {})
        if exif_data:
            exif_parts = []
            if exif_data.get("focal_length"):
                exif_parts.append(f"{exif_data['focal_length']:.0f}mm")
            if exif_data.get("aperture"):
                exif_parts.append(f"f/{exif_data['aperture']:.1f}")
            if exif_data.get("iso"):
                exif_parts.append(f"ISO{exif_data['iso']}")
            if exif_parts:
                parts.append(" ".join(exif_parts))
        photo_path = Path(local_path)

    parts.append(f"file={Path(local_path).name}")
    return " ".join(parts), photo_path


def probe_duration(path: Path) -> float:
    """Probe video duration via ffprobe."""
    try:
        r = run_subprocess(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return float(r.stdout.strip()) if r.stdout.strip() else 0.0
    except Exception:
        return 0.0


def _concat_previews(
    video_entries: list[tuple[int, float, Path]],
    output_path: Path,
) -> tuple[list[tuple[int, float, float]], Path]:
    """Concatenate video previews into one mega-preview with burned-in labels."""
    import tempfile

    # Use actual preview clip durations (not metadata) for accurate offsets
    offset = 0.0
    offset_table = []
    valid_entries = []
    for item_num, _meta_dur, preview_path in video_entries:
        actual_dur = probe_duration(preview_path)
        if actual_dur <= 0:
            logger.warning(f"  Skipping preview #{item_num}: could not probe duration")
            continue
        offset_table.append((item_num, actual_dur, offset))
        valid_entries.append((item_num, actual_dur, preview_path))
        offset += actual_dur

    with tempfile.TemporaryDirectory() as td:
        concat_file = Path(td) / "concat.txt"
        with open(concat_file, "w") as f:
            for _, dur, preview_path in valid_entries:
                safe = str(preview_path.resolve()).replace("\\", "/")
                f.write(f"file '{safe}'\n")
                f.write(f"duration {dur}\n")

        drawtext_parts = []
        for item_num, dur, seg_offset in offset_table:
            label = f"\\\\\\#{item_num}"
            end = seg_offset + dur
            drawtext_parts.append(
                f"drawtext=text='{label}'"
                f":fontsize=36:fontcolor=yellow"
                f":box=1:boxcolor=black@0.8:boxborderw=8:x=10:y=8"
                f":enable='between(t,{seg_offset:.1f},{end:.1f})'"
            )
        vf = ",".join(drawtext_parts) if drawtext_parts else "null"

        logger.info(
            f"Building mega-preview ({len(video_entries)} videos, {offset:.0f}s)..."
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
    logger.info(f"  Mega-preview: {size_mb:.1f}MB")

    if output_path.exists():
        actual_dur = probe_duration(output_path)
        if offset > 0 and actual_dur / offset < 0.5:
            raise RuntimeError(
                f"Mega-preview duration mismatch: expected ~{offset:.0f}s, got {actual_dur:.0f}s (<50%)"
            )
        if abs(actual_dur - offset) > 5:
            logger.warning(
                f"Mega-preview duration drift: expected {offset:.0f}s, got {actual_dur:.0f}s"
            )

    return offset_table, output_path


def _secs_to_timestamp(s: float) -> str:
    h, rem = divmod(int(s), 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def _collect_items(
    analysis_by_id: dict,
    cfg: Config,
    preview_dir: Path,
) -> tuple[str, list[Path], list[tuple[int, float, Path]], int, int]:
    """Iterate all items, build text lines, collect photo paths and video entries.

    Returns (text_block, photo_paths, video_entries, n_photos, n_videos).
    """
    all_items = sorted(analysis_by_id.values(), key=lambda a: a.get("id", 0))
    all_items = _dedup_burst_photos(all_items, cfg.thumbnails_dir)

    lines: list[str] = []
    photo_paths: list[Path] = []
    video_entries: list[tuple[int, float, Path]] = []
    idx = 1
    n_photos = 0
    n_videos = 0

    for a in all_items:
        local_path = a.get("local_path", "")
        if not local_path or not Path(local_path).exists():
            continue

        text_line, photo_path = _build_item_text(idx, a)
        lines.append(text_line)

        if photo_path:
            photo_paths.append(photo_path)
            n_photos += 1
        else:
            # Video — collect for mega-preview
            vid_id = a["id"]
            dur = a.get("video_duration", 0)
            if dur > 0:
                preview_path = preview_dir / f"preview_{vid_id}.mp4"
                if preview_path.exists() and preview_path.stat().st_size > 500:
                    video_entries.append((idx, dur, preview_path))
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
        logger.info(f"Mega-preview cached ({len(video_entries)} videos)")
        offset_table = [tuple(e) for e in cached_meta["offset_table"]]
    else:
        offset_table, mega_path = _concat_previews(video_entries, mega_path)
        meta_path.write_text(
            json.dumps({"key": cache_key, "offset_table": offset_table})
        )

    return offset_table, mega_path


def _build_visual_content_blocks(
    preprocessed: dict,
    analysis_by_id: dict,
    cfg: Config,
    *,
    force: bool = False,
) -> tuple[list, list[tuple[int, float, float]]]:
    """Build flat multimodal content: text metadata + photos + mega video preview.

    No timeline grouping — items are sent as a flat numbered list.
    Gemini sees all photos/videos and decides the narrative structure.

    Returns (blocks, offset_table).
    """
    blocks: list = []
    preview_dir = cfg.preview_clips_dir

    # --- Phase 1: collect items ---
    text_block, photo_paths, video_entries, n_photos, n_videos = _collect_items(
        analysis_by_id, cfg, preview_dir
    )
    blocks.append(text_block)

    # --- Phase 2: add photo thumbnails inline ---
    for p in photo_paths:
        thumb = cfg.thumbnails_dir / f"{p.stem}_thumb.jpg"
        if not thumb.exists():
            raise FileNotFoundError(
                f"Thumbnail missing for {p.name} — run prepare first"
            )
        blocks.append(
            {
                "type": "image_bytes",
                "mime_type": "image/jpeg",
                "data": thumb.read_bytes(),
            }
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
    n_text_blocks = sum(1 for b in blocks if isinstance(b, str))
    n_images = sum(
        1 for b in blocks if isinstance(b, dict) and b.get("type") == "image_bytes"
    )
    n_video_blocks = sum(
        1 for b in blocks if isinstance(b, dict) and b.get("type") == "video_bytes"
    )

    if n_images == 0:
        raise RuntimeError("No photos generated — check source files")

    text_item_nums = set()
    for b in blocks:
        if isinstance(b, str):
            text_item_nums.update(int(m) for m in re.findall(r"#(\d+):", b))
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
    for b in blocks:
        if isinstance(b, str):
            n_files += len(file_pattern.findall(b))
    if n_files == 0 and len(text_item_nums) > 0:
        raise RuntimeError("No file= references found in text metadata")

    inline_bytes = sum(
        len(b.get("data", b""))
        for b in blocks
        if isinstance(b, dict) and b.get("type") == "image_bytes"
    )
    if inline_bytes > 75 * 1024 * 1024:
        raise RuntimeError(
            f"Inline image payload {inline_bytes / 1024 / 1024:.0f}MB exceeds ~75MB limit "
            f"(100MB base64). Reduce photo count or thumbnail size."
        )

    video_bytes = sum(
        len(b.get("data", b""))
        for b in blocks
        if isinstance(b, dict) and b.get("type") == "video_bytes"
    )

    return blocks, offset_table
