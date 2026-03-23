"""Plan stage orchestration: visual planning via Gemini.

Coordinates content building, API call, and post-processing into a
single public `plan()` entry point.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from ..config import Config
from ..edl import EDL, MusicTrack, find_latest_version, save_edl
from ._gemini import _gemini_call
from ._postprocess import (
    deduplicate_items,
    fill_duration_gap,
    fix_hallucinated_paths,
    log_edl_summary,
    parse_and_convert_timestamps,
    validate_and_fix_edl,
    validate_trim_points,
)
from ._preview import _build_visual_content_blocks
from ._prompts import (
    TRIP_TYPES,
    _default_focus,
    _format_date_range,
    _visual_system_prompt,
)

logger = logging.getLogger("vlog.plan")


@dataclass
class PlanConfig:
    style: str = "upbeat"
    target_duration: int = 180
    focus: str = ""
    trip_type: str = "family"
    language: str = "en"
    tz_hours: int | None = None
    model: str | None = None


def _plan_visual(
    cfg: Config, preprocessed: dict, analysis_by_id: dict,
    pc: PlanConfig,
) -> EDL:
    """Single-pass Gemini planning with chain-of-thought.

    Gemini sees individual photo thumbnails (400px) + video clips,
    designs narrative arc + selects items + self-reviews in one call.
    """
    # Trip-level summary
    days = preprocessed.get("timeline", [])
    locations: list[str] = []
    n_candidates = 0
    n_videos = 0
    for day in days:
        for ch in day.get("chapters", []):
            loc = ch.get("location", "")
            if loc and loc != "unknown" and loc not in locations:
                locations.append(loc)
            for item_id in ch.get("item_ids", []):
                a = analysis_by_id.get(str(item_id))
                if a:
                    n_candidates += 1
                    if a.get("media_type") == "video":
                        n_videos += 1

    trip_summary = (
        f"Trip overview: {len(days)} day{'s' if len(days) != 1 else ''}, "
        f"{len(locations)} locations, {n_candidates} candidates "
        f"({n_videos} videos, {n_candidates - n_videos} photos)."
    )

    n_items = pc.target_duration // 4
    trip_label = f"{pc.trip_type} trip" if pc.trip_type != "general" else "trip"
    family_line = ""
    if pc.trip_type == "family" and preprocessed.get("family_names"):
        family_line = f"\nFamily: {', '.join(preprocessed['family_names'])}"

    logger.info("=== SINGLE-PASS PLANNING ===")
    logger.info("Building individual photo thumbnails and concatenated video preview...")

    tz_hours = pc.tz_hours
    if tz_hours is None:
        tz_hours = preprocessed.get("tz_hours", -(time.timezone // 3600))

    content_blocks, preview_offset_table = _build_visual_content_blocks(
        preprocessed, analysis_by_id, cfg, tz_hours=tz_hours)
    n_img = sum(1 for b in content_blocks if isinstance(b, dict) and b.get("type") == "image_bytes")
    n_vid_clips = sum(1 for b in content_blocks if isinstance(b, dict) and b.get("type") == "video_bytes")
    n_text = sum(1 for b in content_blocks if isinstance(b, str))
    logger.info(f"Visual content: {n_text} text blocks, {n_img} photos, {n_vid_clips} video file(s)")

    if n_candidates > 0 and n_text == 0:
        raise RuntimeError(
            f"Have {n_candidates} candidates but 0 text blocks — "
            f"analysis_by_id key mismatch (int vs str?)"
        )

    # Build trip structure summary for arc thinking
    arc_lines = []
    for day in preprocessed["timeline"]:
        arc_lines.append(f"\n=== {day['day_name']} {day['date']} ===")
        for ch in day["chapters"]:
            loc = ch["location"]
            block = ch["time_block"]
            count = len(ch.get("item_ids", []))
            n_vids = sum(1 for iid in ch["item_ids"]
                         if analysis_by_id.get(str(iid), {}).get("media_type") == "video")
            line = f"  [{block.upper()}] {loc} — {count} items"
            if n_vids:
                line += f" ({n_vids} videos)"
            arc_lines.append(line)

    min_duration = int(pc.target_duration * 1.2)
    intro_text = f"""\
Create a {pc.style} {trip_label} vlog EDL from the photos and videos shown below.

{trip_summary}{family_line}

**FOCUS: {pc.focus}** — This is the creative direction. Every chapter, every selection
decision, and every text overlay should serve this focus. When choosing between two
items of similar quality, pick the one that better supports this focus.

DURATION: The vlog MUST be {pc.target_duration}s. Select ~{n_items} items.
Sum of display_duration MUST reach {min_duration}s (transitions eat ~20%).
Photos = 3-5s, videos = 5-10s. {n_items} items × ~4s avg = {pc.target_duration}s.

Trip structure:
{"".join(arc_lines)}

**Think step-by-step:**
1. Design a narrative arc — 4-6 chapters based on STORY BEATS (not locations).
2. Select items: scan every photo and video clip. Pick the best for each chapter.
3. Self-review checklist (fix any issues before outputting):
   - [ ] Does the vlog serve the focus "{pc.focus}"?
   - [ ] Sum of display_duration >= {min_duration}s?
   - [ ] At least 40% video items? At least 50% of videos have keep_audio=true?
   - [ ] No audio=silent videos with keep_audio=true?
   - [ ] Items spread across all days/locations (not clustered)?
   - [ ] No more than 2 portrait videos in the whole EDL?
   - [ ] No duplicate source_file paths?

Output ONE JSON with all your thinking and the final EDL.

Candidates by day/location:"""

    visual_parts: list = [intro_text] + content_blocks

    system_prompt = _visual_system_prompt(pc.trip_type, language=pc.language)
    logger.info(f"Sending {len(visual_parts)} parts to Gemini (single pass)...")

    model_kwargs = {"model": pc.model} if pc.model else {}
    edl_content = _gemini_call(system_prompt, visual_parts,
                               label="single pass: plan", **model_kwargs)

    logger.info(f"=== [Gemini] EDL RESPONSE ({len(edl_content)} chars) ===")
    for line in edl_content.split("\n"):
        logger.info(f"  | {line}")
    logger.info("=== [Gemini] END RESPONSE ===")

    # --- Post-processing pipeline ---
    edl, _ = parse_and_convert_timestamps(edl_content, preview_offset_table)
    fix_hallucinated_paths(edl, cfg.media_dir)
    validate_trim_points(edl, analysis_by_id)
    deduplicate_items(edl)
    edl = fill_duration_gap(edl, pc.target_duration, analysis_by_id,
                            system_prompt, model=pc.model)

    actual_dur = edl.estimated_duration()
    if actual_dur < pc.target_duration * 0.5:
        logger.warning(f"EDL is {actual_dur:.0f}s, target is {pc.target_duration}s — severely underfilled")
    elif actual_dur < pc.target_duration * 0.8:
        logger.warning(f"EDL is {actual_dur:.0f}s, target is {pc.target_duration}s — underfilled")

    validate_and_fix_edl(edl)
    log_edl_summary(edl, pc.target_duration)

    return edl


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def plan(
    cfg: Config,
    *,
    style: str = "upbeat",
    target_duration: int = 180,
    focus: str = "",
    trip_type: str = "family",
    music_file: str | None = None,
    language: str = "en",
    tz_hours: int | None = None,
    model: str | None = None,
) -> tuple[EDL, int]:
    """Generate an EDL from preprocessed + analysis data using the visual planner."""
    if trip_type not in TRIP_TYPES:
        logger.warning(f"Unknown trip_type '{trip_type}', falling back to 'general'")
        trip_type = "general"

    if not os.getenv("GEMINI_API_KEY", ""):
        raise RuntimeError(
            "GEMINI_API_KEY not set. Add it to .env for visual planning. "
            "Get a key at https://ai.google.dev/gemini-api/docs/api-key"
        )

    effective_focus = focus or _default_focus(trip_type)
    preprocessed = json.loads(cfg.preprocessed_path.read_text())
    analysis_items = json.loads(cfg.analysis_path.read_text())
    analysis_by_id: dict[str, dict] = {str(a["id"]): a for a in analysis_items}

    logger.info(f"Planning via Gemini with visual input (target {target_duration}s, style={style}, trip_type={trip_type}, lang={language})...")
    plan_config = PlanConfig(
        style=style, target_duration=target_duration,
        focus=effective_focus, trip_type=trip_type,
        language=language, tz_hours=tz_hours, model=model,
    )
    edl = _plan_visual(cfg, preprocessed, analysis_by_id, plan_config)

    # Post-process: force effect="none" on video items
    for seg in edl.segments:
        for item in seg.items:
            if item.media_type == "video" and item.effect != "none":
                item.effect = "none"

    # Set metadata on the EDL
    edl.trip_type = trip_type
    edl.style = style
    edl.language = language
    edl.intro_style = edl.intro_style or "title_card"
    edl.outro_style = edl.outro_style or "fade_title"
    if not edl.date_range:
        all_dates = sorted({d["date"] for d in preprocessed.get("timeline", [])})
        edl.date_range = _format_date_range(all_dates) if all_dates else ""

    # Store music intent
    if music_file and music_file != "auto" and Path(music_file).exists():
        logger.info(f"Attaching music file: {music_file}")
        edl.music = MusicTrack(file=music_file)
        edl.music_mode = "file"
    elif music_file == "auto":
        edl.music_mode = "auto"
        logger.info("Music mode: auto (will generate in generate_music step)")

    version = find_latest_version(cfg) + 1
    save_edl(cfg, edl, version)

    clips_dir = cfg.clips_dir
    if clips_dir.exists():
        for f in clips_dir.iterdir():
            f.unlink(missing_ok=True)

    logger.info(f"EDL v{version}: {len(edl.segments)} segments, "
         f"{len(edl.all_items())} items, ~{edl.estimated_duration():.0f}s")
    return edl, version
