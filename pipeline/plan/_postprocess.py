"""EDL post-processing: fix Gemini output issues before returning.

Four layers of validation/repair applied after Gemini returns raw JSON:
1. Preview timestamp conversion (mega-preview MM:SS → local trim points)
2. Path fixing (fuzzy match hallucinated filenames)
3. Trim point validation (clamp/remove out-of-range trims)
4. Deduplication, duration gap fill, formal EDL validation
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..edl import EDL, validate_edl
from ..media_utils import strip_markdown_fences
from ._prompts import _timestamp_to_secs

logger = logging.getLogger("vlog.plan")


def parse_and_convert_timestamps(
    edl_content: str,
    preview_offset_table: list[tuple[int, float, float]],
) -> EDL:
    """Parse Gemini JSON response and convert preview timestamps to trim points."""
    edl_content = strip_markdown_fences(edl_content)
    raw = json.loads(edl_content)

    # Gemini sometimes puts a string in "music" instead of object/null
    if "music" in raw and isinstance(raw["music"], str):
        raw["music"] = None

    # Convert preview_start/preview_end (MM:SS) → start_time/end_time (seconds)
    n_converted = 0
    for seg in raw.get("segments", []):
        for item in seg.get("items", []):
            ps = item.pop("preview_start", None)
            pe = item.pop("preview_end", None)
            if ps and pe and preview_offset_table:
                ps_secs = _timestamp_to_secs(ps)
                pe_secs = _timestamp_to_secs(pe)
                # Find which clip preview_start belongs to
                for _, dur, offset in preview_offset_table:
                    if offset <= ps_secs < offset + dur:
                        local_start = ps_secs - offset
                        local_end = min(pe_secs - offset, dur)
                        # Guard: minimum 2s clip
                        if local_end - local_start < 2.0:
                            local_start = max(0, local_start - 1)
                            local_end = min(
                                local_start + max(pe_secs - ps_secs, 5.0), dur
                            )
                        item["start_time"] = round(local_start, 1)
                        item["end_time"] = round(local_end, 1)
                        item["display_duration"] = round(local_end - local_start, 1)
                        n_converted += 1
                        logger.info(
                            f"  Preview {ps}-{pe} → trim {item['start_time']}-{item['end_time']}s "
                            f"({item['display_duration']}s)"
                        )
                        break
                else:
                    logger.warning(f"preview {ps} not in any clip, keeping as-is")
    if n_converted:
        logger.info(
            f"  Converted {n_converted} preview timestamps to local trim points"
        )

    edl = EDL.model_validate(raw)
    return edl


def fix_hallucinated_paths(edl: EDL, media_dir: Path) -> int:
    """Resolve filenames to full paths, fuzzy-match hallucinated names.

    Gemini outputs just filenames (e.g. '87656_IMG.heic'). This resolves
    them to full paths under media_dir, with fuzzy fallback for typos.
    Returns count of removed items (unresolvable).
    """
    removed_count = 0
    for seg in edl.segments:
        valid_items = []
        for item in seg.items:
            source = Path(item.source_file)
            # Already a full path that exists
            if source.exists():
                valid_items.append(item)
                continue
            # Try as filename under media_dir
            full = media_dir / source.name
            if full.exists():
                item.source_file = str(full)
                valid_items.append(item)
                continue
            # Fuzzy match (Gemini may hallucinate parts of the filename)
            name = source.name
            parts = name.split("_", 1)
            candidates = list(media_dir.glob(f"*{parts[-1]}")) if len(parts) > 1 else []
            if not candidates:
                candidates = list(media_dir.glob(f"*{name}"))
            # Normalize underscores (Gemini adds/removes _ in timestamps)
            if not candidates:
                norm = name.replace("_", "").lower()
                candidates = [
                    f
                    for f in media_dir.iterdir()
                    if f.name.replace("_", "").lower() == norm
                ]
            if candidates:
                item.source_file = str(candidates[0])
                logger.info(f"  Fixed path: {name} → {candidates[0].name}")
                valid_items.append(item)
            else:
                logger.info(f"  Removed item with missing source: {name}")
                removed_count += 1
        seg.items = valid_items
    edl.segments = [s for s in edl.segments if s.items]
    if removed_count:
        logger.info(
            f"  Path validation: removed {removed_count} items with missing sources"
        )
    return removed_count


def validate_trim_points(edl: EDL, analysis_by_id: dict) -> tuple[int, int]:
    """Clamp or remove invalid video trim points. Returns (fixed, removed) counts."""
    trim_fixed = 0
    trim_removed = 0
    for seg in edl.segments:
        valid_items = []
        for item in seg.items:
            if item.media_type == "video" and item.start_time is not None:
                vid_dur = analysis_by_id.get(
                    next(
                        (
                            aid
                            for aid, a in analysis_by_id.items()
                            if a.get("local_path") == item.source_file
                        ),
                        None,
                    ),
                    {},
                ).get("video_duration")
                if vid_dur and vid_dur > 0:
                    changed = False
                    if item.start_time >= vid_dur:
                        item.start_time = max(vid_dur - 2, 0)
                        changed = True
                    if item.end_time is not None and item.end_time > vid_dur:
                        item.end_time = vid_dur
                        changed = True
                    # Ensure minimum 2s trim after clamping
                    if (
                        item.end_time is not None
                        and item.end_time - item.start_time < 2.0
                    ):
                        # Try widening: move start earlier, then end later
                        needed = 2.0 - (item.end_time - item.start_time)
                        item.start_time = max(0, item.start_time - needed)
                        if item.end_time - item.start_time < 2.0:
                            item.end_time = min(item.start_time + 2.0, vid_dur)
                        changed = True
                    if item.end_time is not None and item.start_time >= item.end_time:
                        logger.info(
                            f"  Trim removal: {Path(item.source_file).name} "
                            f"start={item.start_time:.1f} >= end={item.end_time:.1f} "
                            f"(duration={vid_dur:.1f}s)"
                        )
                        trim_removed += 1
                        continue
                    if changed:
                        logger.info(
                            f"  Trim clamped: {Path(item.source_file).name} "
                            f"to [{item.start_time:.1f}, {item.end_time}] "
                            f"(duration={vid_dur:.1f}s)"
                        )
                        trim_fixed += 1
            valid_items.append(item)
        seg.items = valid_items
    edl.segments = [s for s in edl.segments if s.items]
    if trim_fixed or trim_removed:
        logger.info(f"  Trim validation: {trim_fixed} clamped, {trim_removed} removed")

    # Fix display_duration to match trim range / speed
    dur_fixed = 0
    for seg in edl.segments:
        for item in seg.items:
            if (
                item.media_type == "video"
                and item.start_time is not None
                and item.end_time is not None
            ):
                trim_dur = item.end_time - item.start_time
                speed = item.playback_speed or 1.0
                expected = trim_dur / speed
                if abs(expected - item.display_duration) > 0.5:
                    logger.info(
                        f"  Duration fix: {Path(item.source_file).name} "
                        f"display_duration {item.display_duration:.1f}s → {expected:.1f}s "
                        f"(trim={trim_dur:.1f}s, speed={speed})"
                    )
                    item.display_duration = round(expected, 1)
                    dur_fixed += 1
    if dur_fixed:
        logger.info(f"  Duration alignment: {dur_fixed} items corrected")

    return trim_fixed, trim_removed


def deduplicate_items(edl: EDL) -> int:
    """Remove duplicate source_file entries (keep first). Returns removed count."""
    seen_sources: set[str] = set()
    dedup_removed = 0
    for seg in edl.segments:
        unique_items = []
        for item in seg.items:
            if item.source_file in seen_sources:
                logger.info(f"  Dedup: removed duplicate {Path(item.source_file).name}")
                dedup_removed += 1
            else:
                seen_sources.add(item.source_file)
                unique_items.append(item)
        seg.items = unique_items
    edl.segments = [s for s in edl.segments if s.items]
    if dedup_removed:
        logger.info(f"  Dedup: removed {dedup_removed} duplicate items")
    return dedup_removed


def validate_and_fix_edl(edl: EDL) -> None:
    """Run formal EDL validation and auto-fix media_type mismatches."""
    edl_issues = validate_edl(edl, strict=False)
    for issue in edl_issues:
        level = issue["level"].upper()
        logger.info(f"  EDL {level}: {issue['message']}")
        if "media_type='video' but file is a photo" in issue["message"]:
            for seg in edl.segments:
                for item in seg.items:
                    ext = Path(item.source_file).suffix.lower()
                    if item.media_type == "video" and ext in {
                        ".jpg",
                        ".jpeg",
                        ".png",
                        ".heic",
                        ".heif",
                        ".webp",
                    }:
                        logger.info(
                            f"  Auto-fix: {Path(item.source_file).name} video→photo"
                        )
                        item.media_type = "photo"
                        item.effect = "ken_burns_in"
                        item.start_time = None
                        item.end_time = None
                        item.keep_audio = False


def log_edl_summary(edl: EDL, target_duration: int) -> None:
    """Log structured summary of the final EDL."""
    actual_dur = edl.estimated_duration()
    all_items = edl.all_items()
    n_videos = sum(1 for i in all_items if i.media_type == "video")
    n_photos = len(all_items) - n_videos
    n_keep_audio = sum(1 for i in all_items if i.keep_audio)
    n_text_overlay = sum(1 for i in all_items if i.text_overlay)
    n_speed_ramp = sum(1 for i in all_items if i.playback_speed != 1.0)

    logger.info("=== [Gemini] PARSED EDL ===")
    logger.info(f"  Title: {edl.title}")
    logger.info(
        f"  Segments: {len(edl.segments)}, Items: {len(all_items)} "
        f"({n_photos} photos + {n_videos} videos)"
    )
    logger.info(
        f"  Duration: {actual_dur:.0f}s (target: {target_duration}s, "
        f"{'OK' if actual_dur >= target_duration * 0.8 else 'UNDERFILLED'})"
    )
    logger.info(f"  Speech clips (keep_audio): {n_keep_audio}")
    logger.info(f"  Text overlays: {n_text_overlay}")
    logger.info(f"  Speed ramps: {n_speed_ramp}")

    for si, seg in enumerate(edl.segments):
        seg_dur = sum(i.display_duration for i in seg.items)
        logger.info(
            f"  --- Segment {si}: {seg.name} ({len(seg.items)} items, {seg_dur:.0f}s) ---"
        )
        logger.info(
            f"    Transition: {seg.transition} ({seg.transition_duration}s) | "
            f"Mode: {seg.mode} | Color: {seg.color_temp}"
        )
        logger.info(f"    Music mood: {seg.music_mood[:120]}")
        if seg.narrative_rationale:
            logger.info(f"    Rationale: {seg.narrative_rationale[:150]}")
        for item in seg.items:
            trim = (
                f" trim={item.start_time:.0f}-{item.end_time:.0f}s"
                if item.start_time is not None
                else ""
            )
            flags = []
            if item.keep_audio:
                flags.append("SPEECH")
            if item.playback_speed != 1.0:
                flags.append(f"speed={item.playback_speed}x")
            if item.text_overlay:
                flags.append(f'text="{item.text_overlay.text[:30]}"')
            flag_str = f" [{', '.join(flags)}]" if flags else ""
            logger.info(
                f"    - {item.media_type:5s} {item.display_duration}s "
                f"{item.effect:16s} {Path(item.source_file).name}{trim}{flag_str}"
            )
    logger.info("=== [Gemini] END PARSED EDL ===")
