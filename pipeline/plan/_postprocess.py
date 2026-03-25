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

from .._types import AnalysisEntry
from ..edl import EDL, validate_edl
from ._prompts import _timestamp_to_secs

logger = logging.getLogger("vlog.plan")


def parse_and_convert_timestamps(
    edl_content: str,
    preview_offset_table: list[tuple[int, float, float]],
) -> EDL:
    """Parse Gemini JSON response and convert preview timestamps to trim points."""
    raw = json.loads(edl_content)

    # Convert preview_start/preview_end (MM:SS) → start_time/end_time (seconds)
    n_converted = 0
    for seg in raw.get("segments", []):
        for item in seg.get("items", []):
            ps = item.pop("preview_start", None)
            pe = item.pop("preview_end", None)
            if ps and pe and preview_offset_table:
                ps_secs = _timestamp_to_secs(ps)
                pe_secs = _timestamp_to_secs(pe)
                window = pe_secs - ps_secs

                # Match by finding which clip's offset range contains the
                # MIDPOINT of the selected window (robust against Gemini
                # using clip-end as preview_start)
                mid_secs = (ps_secs + pe_secs) / 2
                matched = None
                for item_num, dur, offset in preview_offset_table:
                    if offset <= mid_secs < offset + dur:
                        matched = (item_num, dur, offset)
                        break

                if not matched:
                    # Fallback: best overlap
                    best_overlap = 0.0
                    for item_num, dur, offset in preview_offset_table:
                        ov = max(0, min(pe_secs, offset + dur) - max(ps_secs, offset))
                        if ov > best_overlap:
                            best_overlap = ov
                            matched = (item_num, dur, offset)

                if matched:
                    _, dur, offset = matched
                    local_start = max(0, ps_secs - offset)
                    local_end = min(pe_secs - offset, dur)
                    # If window fell mostly outside this clip (Gemini used
                    # clip-end as start), treat as "select from start"
                    if local_end - local_start < window * 0.5:
                        local_start = max(0, mid_secs - offset - window / 2)
                        local_end = min(local_start + window, dur)
                        local_start = max(0, local_end - window)
                    # Guard: minimum 2s clip
                    original_span = local_end - local_start
                    if original_span < 2.0:
                        mid = (local_start + local_end) / 2
                        half = max(window, 5.0) / 2
                        local_start = max(0, mid - half)
                        local_end = min(local_start + max(window, 5.0), dur)
                        local_start = max(0, local_end - max(window, 5.0))
                        widened = local_end - local_start
                        if widened - original_span > 2.0:
                            logger.warning(
                                "  Min-2s guard widened clip #%s "
                                "from %.1fs to %.1fs "
                                "(Gemini trim may have been off)",
                                matched[0],
                                original_span,
                                widened,
                            )
                    item["start_time"] = round(local_start, 1)
                    item["end_time"] = round(local_end, 1)
                    item["display_duration"] = round(local_end - local_start, 1)
                    n_converted += 1
                    logger.info(
                        "  Preview %s-%s → clip #%s trim %s-%ss (%ss)",
                        ps,
                        pe,
                        matched[0],
                        item["start_time"],
                        item["end_time"],
                        item["display_duration"],
                    )
                else:
                    logger.warning("preview %s not in any clip, keeping as-is", ps)
    if n_converted:
        logger.info(
            "  Converted %d preview timestamps to local trim points", n_converted
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
                if len(candidates) > 1:
                    logger.warning(
                        "  Fuzzy path: %s matched %d candidates: %s — using first",
                        name,
                        len(candidates),
                        [c.name for c in candidates[:5]],
                    )
                else:
                    logger.info("  Fixed path: %s → %s", name, candidates[0].name)
                valid_items.append(item)
            else:
                logger.info("  Removed item with missing source: %s", name)
                removed_count += 1
        seg.items = valid_items
    edl.segments = [s for s in edl.segments if s.items]
    if removed_count:
        logger.info(
            "  Path validation: removed %d items with missing sources", removed_count
        )
    return removed_count


def validate_trim_points(
    edl: EDL, analysis_by_id: dict[str, AnalysisEntry]
) -> tuple[int, int]:
    """Clamp or remove invalid video trim points. Returns (fixed, removed) counts."""
    trim_fixed = 0
    trim_removed = 0
    for seg in edl.segments:
        valid_items = []
        for item in seg.items:
            if item.media_type == "video" and item.start_time is not None:
                matched_id = next(
                    (
                        aid
                        for aid, a in analysis_by_id.items()
                        if a.get("local_path") == item.source_file
                    ),
                    None,
                )
                vid_dur = (
                    analysis_by_id[matched_id].get("video_duration")
                    if matched_id is not None
                    else None
                )
                if vid_dur and vid_dur > 0:
                    changed = False
                    st = item.start_time
                    et = item.end_time
                    if st >= vid_dur:
                        st = max(vid_dur - 2, 0)
                        changed = True
                    if et is not None and et > vid_dur:
                        et = vid_dur
                        changed = True
                    # Ensure minimum 2s trim after clamping
                    if et is not None and et - st < 2.0:
                        # Try widening: move start earlier, then end later
                        needed = 2.0 - (et - st)
                        st = max(0, st - needed)
                        if et - st < 2.0:
                            et = min(st + 2.0, vid_dur)
                        changed = True
                    item.start_time = st
                    item.end_time = et
                    if et is not None and st >= et:
                        logger.info(
                            "  Trim removal: %s start=%.1f >= end=%.1f (duration=%.1fs)",
                            Path(item.source_file).name,
                            item.start_time,
                            item.end_time,
                            vid_dur,
                        )
                        trim_removed += 1
                        continue
                    if changed:
                        logger.info(
                            "  Trim clamped: %s to [%.1f, %s] (duration=%.1fs)",
                            Path(item.source_file).name,
                            item.start_time,
                            item.end_time,
                            vid_dur,
                        )
                        trim_fixed += 1
            valid_items.append(item)
        seg.items = valid_items
    edl.segments = [s for s in edl.segments if s.items]
    if trim_fixed or trim_removed:
        logger.info(
            "  Trim validation: %d clamped, %d removed", trim_fixed, trim_removed
        )

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
                        "  Duration fix: %s display_duration %.1fs → %.1fs "
                        "(trim=%.1fs, speed=%s)",
                        Path(item.source_file).name,
                        item.display_duration,
                        expected,
                        trim_dur,
                        speed,
                    )
                    item.display_duration = round(expected, 1)
                    dur_fixed += 1
    if dur_fixed:
        logger.info("  Duration alignment: %d items corrected", dur_fixed)

    return trim_fixed, trim_removed


def deduplicate_items(edl: EDL) -> int:
    """Remove duplicate source_file entries (keep first). Returns removed count."""
    seen_sources: set[str] = set()
    dedup_removed = 0
    for seg in edl.segments:
        unique_items = []
        for item in seg.items:
            if item.source_file in seen_sources:
                logger.info(
                    "  Dedup: removed duplicate %s", Path(item.source_file).name
                )
                dedup_removed += 1
            else:
                seen_sources.add(item.source_file)
                unique_items.append(item)
        seg.items = unique_items
    edl.segments = [s for s in edl.segments if s.items]
    if dedup_removed:
        logger.info("  Dedup: removed %d duplicate items", dedup_removed)
    return dedup_removed


def validate_and_fix_edl(edl: EDL) -> None:
    """Run formal EDL validation and auto-fix media_type mismatches."""
    edl_issues = validate_edl(edl, strict=False)
    for issue in edl_issues:
        level = issue["level"].upper()
        logger.info("  EDL %s: %s", level, issue["message"])
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
                            "  Auto-fix: %s video→photo",
                            Path(item.source_file).name,
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
    logger.info("  Title: %s", edl.title)
    logger.info(
        "  Segments: %d, Items: %d (%d photos + %d videos)",
        len(edl.segments),
        len(all_items),
        n_photos,
        n_videos,
    )
    status = "OK" if actual_dur >= target_duration * 0.8 else "UNDERFILLED"
    logger.info(
        "  Duration: %.0fs (target: %ds, %s)", actual_dur, target_duration, status
    )
    logger.info("  Speech clips (keep_audio): %d", n_keep_audio)
    logger.info("  Text overlays: %d", n_text_overlay)
    logger.info("  Speed ramps: %d", n_speed_ramp)

    for si, seg in enumerate(edl.segments):
        seg_dur = sum(i.display_duration for i in seg.items)
        logger.info(
            "  --- Segment %d: %s (%d items, %.0fs) ---",
            si,
            seg.name,
            len(seg.items),
            seg_dur,
        )
        logger.info(
            "    Transition: %s (%ss) | Mode: %s | Color: %s",
            seg.transition,
            seg.transition_duration,
            seg.mode,
            seg.color_temp,
        )
        logger.info("    Music mood: %s", seg.music_mood[:120])
        if seg.narrative_rationale:
            logger.info("    Rationale: %s", seg.narrative_rationale[:150])
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
                "    - %-5s %ss %-16s %s%s%s",
                item.media_type,
                item.display_duration,
                item.effect,
                Path(item.source_file).name,
                trim,
                flag_str,
            )

    # Rich tree display to terminal
    try:
        import sys

        from rich.console import Console
        from rich.tree import Tree

        status = (
            "[green]OK[/green]"
            if actual_dur >= target_duration * 0.8
            else "[red]UNDERFILLED[/red]"
        )
        tree = Tree(
            f"[bold]{edl.title}[/bold]  {actual_dur:.0f}s/{target_duration}s {status}"
        )
        for seg in edl.segments:
            seg_dur = sum(i.display_duration for i in seg.items)
            branch = tree.add(
                f"[bold cyan]{seg.name}[/bold cyan]  "
                f"[dim]{len(seg.items)} items, {seg_dur:.0f}s, {seg.color_temp}[/dim]"
            )
            for item in seg.items:
                name = Path(item.source_file).name
                flags = []
                if item.keep_audio:
                    flags.append("[yellow]speech[/yellow]")
                if item.playback_speed != 1.0:
                    flags.append(f"[magenta]{item.playback_speed}x[/magenta]")
                if item.text_overlay:
                    flags.append(f'[italic]"{item.text_overlay.text[:20]}"[/italic]')
                flag_str = " " + " ".join(flags) if flags else ""
                if item.media_type == "video":
                    trim = (
                        f" [{item.start_time:.0f}-{item.end_time:.0f}s]"
                        if item.start_time is not None
                        else ""
                    )
                    branch.add(
                        f"[blue]\U0001f3ac {item.display_duration}s[/blue] {name}{trim}{flag_str}"
                    )
                else:
                    branch.add(
                        f"[green]\U0001f4f7 {item.display_duration}s[/green] {name}{flag_str}"
                    )
        if sys.stderr.isatty():
            Console(stderr=True).print(tree)
    except ImportError:
        pass
