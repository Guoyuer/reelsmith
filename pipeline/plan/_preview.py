"""Content block builders for Gemini visual planning.

Builds multimodal input: interleaved text metadata + individual photo
thumbnails (400px inline) + concatenated video mega-preview (Files API).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path

from ..config import Config
from ..media_utils import probe_duration, run_subprocess
from ._prompts import _secs_to_timestamp

logger = logging.getLogger("vlog.plan")


def _build_visual_chapter_text(
    chapter: dict,
    day: dict,
    analysis_by_id: dict,
    start_idx: int,
    *,
    tz_hours: int = 0,
) -> tuple[str, list[Path], list[str], list[dict]]:
    """Build text metadata for a chapter and collect image paths.

    Returns (text, photo_paths, photo_labels, video_items) where:
    - text: metadata lines with numbered items
    - photo_paths: ordered list of photo paths for inline upload
    - photo_labels: matching labels for each photo (e.g. "#01")
    - video_items: list of video analysis dicts for preview clips
    """
    lines = []
    photo_paths = []
    photo_labels = []
    video_items = []
    idx = start_idx

    for item_id in chapter.get("item_ids", []):
        a = analysis_by_id.get(str(item_id))
        if not a:
            continue

        local_path = a.get("local_path", "")
        media = a.get("media_type", "photo")
        persons = a.get("persons", [])
        taken_iso = a.get("taken_iso", "")
        time_str = ""
        if taken_iso and len(taken_iso) >= 16:
            try:
                from datetime import datetime, timedelta

                dt = datetime.fromisoformat(taken_iso.replace("Z", "+00:00"))
                local_dt = dt + timedelta(hours=tz_hours)
                time_str = local_dt.strftime("%H:%M")
            except Exception:
                logger.debug("Could not parse timestamp %s", taken_iso, exc_info=True)
                time_str = taken_iso[11:16]

        label = f"#{idx:02d}"
        # Describe who's in the photo
        if a.get("family_count", 0) >= 2:
            who = (
                f"family together ({','.join(persons[:3])})"
                if persons
                else "family together"
            )
        elif a.get("family_count", 0) == 1:
            who = f"{persons[0]}" if persons else "one family member"
        elif persons:
            who = f"people: {','.join(persons[:3])}"
        else:
            who = "unknown"
        parts = [f"{label}: {who}"]
        if time_str:
            parts.append(f"time={time_str}")

        # Per-item location
        item_loc = a.get("district") or a.get("first_level") or a.get("country")
        if item_loc:
            parts.append(f"at={item_loc}")

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
            audio = a.get("audio_level", "")
            audio_str = f" audio={audio}" if audio and audio != "unknown" else ""
            parts.append(f"video={dur_s}{res_str}{orient_str}{fps_str}{audio_str}")
            video_items.append(a)
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
            photo_paths.append(Path(local_path))
            photo_labels.append(label)

        parts.append(f"file={Path(local_path).name}")
        lines.append(" ".join(parts))
        idx += 1

    loc = chapter.get("location", "unknown")
    block = chapter.get("time_block", "")
    n_photos = len(photo_paths)
    n_videos = len(video_items)
    media_note = f"{n_photos} photos" + (f", {n_videos} videos" if n_videos else "")
    header = (
        f"\n--- {day['day_name']} {day['date']}, {block} near {loc} ({media_note}) ---"
    )
    text = header + "\n" + "\n".join(lines)
    return text, photo_paths, photo_labels, video_items


def _concat_previews(
    video_entries: list[tuple[int, float, Path]],
    output_path: Path,
) -> tuple[list[tuple[int, float, float]], Path]:
    """Concatenate video previews into one mega-preview with burned-in labels.

    Returns (offset_table, output_path).
    """
    import shutil
    import tempfile

    # Build offset table and concat list
    offset = 0.0
    offset_table = []
    for item_num, dur, _ in video_entries:
        offset_table.append((item_num, dur, offset))
        offset += dur

    # Write concat demuxer list
    tmp_dir = Path(tempfile.mkdtemp(prefix="mega_"))
    list_file = tmp_dir / "concat.txt"
    with open(list_file, "w") as f:
        for _, dur, preview_path in video_entries:
            safe = str(preview_path.resolve()).replace("\\", "/")
            f.write(f"file '{safe}'\n")
            f.write(f"duration {dur}\n")

    # Build drawtext filter: one label per segment
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
            str(list_file),
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "40",
            "-c:a",
            "aac",
            "-b:a",
            "64k",
            "-ac",
            "1",
            str(output_path),
        ],
        capture_output=True,
        timeout=600,
    )

    shutil.rmtree(tmp_dir, ignore_errors=True)

    size_mb = output_path.stat().st_size / 1024 / 1024 if output_path.exists() else 0
    logger.info(f"  Mega-preview: {size_mb:.1f}MB")

    # Validate duration
    if output_path.exists():
        actual_dur = probe_duration(output_path)
        if abs(actual_dur - offset) > 5:
            raise RuntimeError(
                f"Mega-preview duration mismatch: expected {offset:.0f}s, got {actual_dur:.0f}s"
            )

    return offset_table, output_path


def _build_visual_content_blocks(
    preprocessed: dict,
    analysis_by_id: dict,
    cfg: Config,
    *,
    tz_hours: int = 0,
    force: bool = False,
) -> tuple[list, list[tuple[int, float, float]]]:
    """Build multimodal parts: interleaved text + individual photos + mega video preview.

    Returns (blocks, offset_table) where:
    - blocks: list of str and media dicts suitable for _gemini_call()
    - offset_table: [(item_num, clip_duration, offset_in_preview), ...]
    """
    blocks: list = []

    preview_dir = cfg.preview_clips_dir
    video_entries: list[tuple[int, float, Path]] = []

    global_idx = 1

    for day in preprocessed["timeline"]:
        for chapter in day["chapters"]:
            text, photo_paths, photo_labels, video_items = _build_visual_chapter_text(
                chapter,
                day,
                analysis_by_id,
                global_idx,
                tz_hours=tz_hours,
            )
            n_items = len(photo_paths) + len(video_items)
            if n_items == 0:
                continue

            blocks.append(text)

            # Send each photo's pre-built thumbnail (400px JPEG)
            for pi, p in enumerate(photo_paths):
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

            # Collect video info for concatenated mega-preview
            for vi in video_items:
                vid_id = vi["id"]
                dur = vi.get("video_duration", 0)
                if dur <= 0:
                    continue
                item_num = global_idx + len(photo_paths) + video_items.index(vi)
                preview_path = preview_dir / f"preview_{vid_id}.mp4"
                if preview_path.exists() and preview_path.stat().st_size > 500:
                    video_entries.append((item_num, dur, preview_path))

            global_idx += n_items

    # Build one concatenated mega-preview (cached)
    offset_table: list[tuple[int, float, float]] = []
    if video_entries:
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
                json.dumps(
                    {
                        "key": cache_key,
                        "offset_table": offset_table,
                    }
                )
            )

        # Build preview timestamp index
        preview_ranges: dict[int, str] = {}
        for item_num, dur, offset in offset_table:
            start_ts = _secs_to_timestamp(offset)
            end_ts = _secs_to_timestamp(offset + dur)
            preview_ranges[item_num] = f"{start_ts}-{end_ts}"

        # Inject preview timestamps into text metadata blocks
        for bi, block in enumerate(blocks):
            if not isinstance(block, str):
                continue
            new_lines = []
            for line in block.split("\n"):
                m = re.match(r"^#(\d+):", line)
                if m and "video=" in line:
                    num = int(m.group(1))
                    if num in preview_ranges:
                        line = line + f" preview={preview_ranges[num]}"
                new_lines.append(line)
            blocks[bi] = "\n".join(new_lines)

        blocks.append(
            "--- VIDEO PREVIEW ---\n"
            "All video clips are concatenated below. Each clip has its item number "
            "(e.g. #30) burned into the top-left corner. Use the preview=MM:SS-MM:SS "
            "range in each video's metadata to locate it in the preview.\n"
            "When selecting a video clip, set preview_start and preview_end to the "
            "exact MM:SS timestamps in THIS preview video where the moment you want "
            "begins and ends. Our code will convert these to the correct trim points."
        )
        blocks.append(
            {
                "type": "video_bytes",
                "mime_type": "video/mp4",
                "data": mega_path.read_bytes(),
            }
        )

    # --- Validate everything before sending to Gemini ---
    n_text_blocks = sum(1 for b in blocks if isinstance(b, str))
    n_images = sum(
        1 for b in blocks if isinstance(b, dict) and b.get("type") == "image_bytes"
    )
    n_videos = sum(
        1 for b in blocks if isinstance(b, dict) and b.get("type") == "video_bytes"
    )

    if n_text_blocks == 0:
        raise RuntimeError("No text blocks generated — check analysis_by_id key types")
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

    logger.info(
        f"Validation OK: {n_text_blocks} text, {n_images} images ({inline_bytes / 1024 / 1024:.1f}MB), "
        f"{n_videos} video ({video_bytes / 1024 / 1024:.1f}MB), "
        f"{len(text_item_nums)} items, {len(video_nums_in_mega)} video labels"
    )

    return blocks, offset_table
