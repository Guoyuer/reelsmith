"""Plan stage orchestration: visual planning via Gemini.

Coordinates content building, API call, and post-processing into a
single public `plan()` entry point.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from ..config import Config
from ..edl import EDL, MusicTrack, find_latest_version, save_edl
from ._gemini import _gemini_call
from ._postprocess import (
    deduplicate_items,
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
    target_duration: int  # required — no default
    style: str = "upbeat"
    focus: str = ""
    trip_type: str = "family"
    language: str = "en"
    tz_hours: int | None = None
    model: str | None = None
    thinking_level: str = "HIGH"  # OFF, LOW, HIGH
    music_file: str | None = None
    force: bool = False

    def __post_init__(self) -> None:
        if self.target_duration <= 0:
            raise ValueError(
                f"target_duration must be positive: {self.target_duration}"
            )
        if self.trip_type not in TRIP_TYPES:
            raise ValueError(
                f"Unknown trip_type '{self.trip_type}'. Valid: {TRIP_TYPES}"
            )


def _plan_visual(
    cfg: Config,
    preprocessed: dict,
    analysis_by_id: dict,
    pc: PlanConfig,
    *,
    progress_callback=None,
) -> EDL:
    """Single-pass Gemini planning with chain-of-thought.

    Gemini sees individual photo thumbnails (400px) + video clips,
    designs narrative arc + selects items + self-reviews in one call.
    """
    # Summary
    n_candidates = len(analysis_by_id)
    n_videos = sum(1 for a in analysis_by_id.values() if a.get("media_type") == "video")
    n_photos = n_candidates - n_videos

    trip_summary = f"{n_candidates} candidates ({n_videos} videos, {n_photos} photos)."

    n_items = pc.target_duration // 4
    trip_label = f"{pc.trip_type} trip" if pc.trip_type != "general" else "trip"
    family_line = ""
    if pc.trip_type == "family" and preprocessed.get("family_names"):
        family_line = f"\nFamily: {', '.join(preprocessed['family_names'])}"

    logger.info("=== SINGLE-PASS PLANNING ===")
    logger.info("Building photo thumbnails and video preview...")

    content_blocks, preview_offset_table = _build_visual_content_blocks(
        preprocessed, analysis_by_id, cfg, force=pc.force
    )
    n_img = sum(
        1
        for b in content_blocks
        if isinstance(b, dict) and b.get("type") == "image_bytes"
    )
    n_vid_clips = sum(
        1
        for b in content_blocks
        if isinstance(b, dict) and b.get("type") == "video_bytes"
    )
    logger.info(f"Visual content: {n_img} photos, {n_vid_clips} video file(s)")
    if progress_callback:
        progress_callback(0, 0, f"uploading {n_img} photos + {n_vid_clips} video...")

    intro_text = f"""\
Create a {pc.style} {trip_label} vlog EDL from the photos and videos shown below.

{trip_summary}{family_line}

**FOCUS: {pc.focus}** — This is the creative direction. Every chapter, every selection
decision, and every text overlay should serve this focus. When choosing between two
items of similar quality, pick the one that better supports this focus.

DURATION: Sum of ALL display_duration MUST equal {pc.target_duration}s (±10%).
Photos = 3-4s each, videos = 6-8s each. Select ~{n_items} items to fill {pc.target_duration}s.

**Think step-by-step:**
1. Look at ALL photos and watch the video preview. Identify the best moments.
2. Design a narrative arc — 4-6 chapters based on STORY BEATS.
3. Select items for each chapter. Verify: sum of display_duration = {pc.target_duration}s (±10%).
   If short, add more items or extend video trims. If long, remove weakest items.
4. Self-review: diverse locations? No duplicates? Videos ≥ 50%? No portrait videos > 2?

Output ONE JSON EDL.

All candidates:"""

    visual_parts: list = [intro_text] + content_blocks

    system_prompt = _visual_system_prompt(pc.trip_type, language=pc.language)
    logger.info(f"Sending {len(visual_parts)} parts to Gemini (single pass)...")

    model_kwargs: dict = {}
    if pc.model:
        model_kwargs["model"] = pc.model
    if pc.thinking_level:
        model_kwargs["thinking_level"] = pc.thinking_level
    edl_content = _gemini_call(
        system_prompt,
        visual_parts,
        label="single pass: plan",
        progress_callback=progress_callback,
        **model_kwargs,
    )

    logger.info(f"=== [Gemini] EDL RESPONSE ({len(edl_content)} chars) ===")
    for line in edl_content.split("\n"):
        logger.info(f"  | {line}")
    logger.info("=== [Gemini] END RESPONSE ===")

    # --- Post-processing pipeline ---
    if progress_callback:
        progress_callback(0, 0, "post-processing...")
    edl = parse_and_convert_timestamps(edl_content, preview_offset_table)
    fix_hallucinated_paths(edl, cfg.media_dir)
    validate_trim_points(edl, analysis_by_id)
    deduplicate_items(edl)

    actual_dur = edl.estimated_duration()
    if actual_dur < pc.target_duration * 0.5:
        raise RuntimeError(
            f"EDL is {actual_dur:.0f}s, target is {pc.target_duration}s — "
            f"less than 50% filled. Check Gemini output and post-processing logs."
        )
    if actual_dur < pc.target_duration:
        logger.warning(
            f"EDL is {actual_dur:.0f}s, target is {pc.target_duration}s — underfilled"
        )

    validate_and_fix_edl(edl)
    log_edl_summary(edl, pc.target_duration)

    return edl


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def plan(cfg: Config, pc: PlanConfig, *, progress_callback=None) -> tuple[EDL, int]:
    """Generate an EDL from preprocessed + analysis data using the visual planner."""
    if not cfg.preprocessed_path.exists():
        raise FileNotFoundError(
            f"Preprocessed data not found: {cfg.preprocessed_path}\n"
            "Run the prepare stage first (e.g. vlog prepare -s local -p ./photos)"
        )
    from ..prepare import load_analysis

    effective_focus = pc.focus or _default_focus(pc.trip_type)
    preprocessed = json.loads(cfg.preprocessed_path.read_text())
    analysis_items = load_analysis(cfg)
    analysis_by_id: dict[str, dict] = {str(a["id"]): a for a in analysis_items}

    logger.info(
        f"Planning via Gemini (target {pc.target_duration}s, "
        f"style={pc.style}, trip_type={pc.trip_type}, lang={pc.language}, "
        f"model={pc.model}, thinking={pc.thinking_level})..."
    )
    # Use a copy with effective_focus applied so _plan_visual gets the resolved focus
    visual_pc = PlanConfig(
        style=pc.style,
        target_duration=pc.target_duration,
        focus=effective_focus,
        trip_type=pc.trip_type,
        language=pc.language,
        tz_hours=pc.tz_hours,
        model=pc.model,
        thinking_level=pc.thinking_level,
        music_file=pc.music_file,
        force=pc.force,
    )
    edl = _plan_visual(
        cfg,
        preprocessed,
        analysis_by_id,
        visual_pc,
        progress_callback=progress_callback,
    )

    # Post-process: force effect="none" on video items
    for seg in edl.segments:
        for item in seg.items:
            if item.media_type == "video" and item.effect != "none":
                item.effect = "none"

    # Set metadata on the EDL
    edl.trip_type = pc.trip_type
    edl.style = pc.style
    edl.language = pc.language  # type: ignore[assignment]  # validated by CLI
    edl.intro_style = edl.intro_style or "title_card"
    edl.outro_style = edl.outro_style or "fade_title"
    if not edl.date_range:
        all_dates = sorted({d["date"] for d in preprocessed.get("timeline", [])})
        edl.date_range = _format_date_range(all_dates) if all_dates else ""

    # Store music intent
    if pc.music_file and pc.music_file != "auto" and Path(pc.music_file).exists():
        logger.info(f"Attaching music file: {pc.music_file}")
        edl.music = MusicTrack(file=pc.music_file)
        edl.music_mode = "file"
    elif pc.music_file == "auto":
        edl.music_mode = "auto"
        logger.info("Music mode: auto (will generate in generate_music step)")

    version = find_latest_version(cfg) + 1
    save_edl(cfg, edl, version)

    clips_dir = cfg.clips_dir
    if clips_dir.exists():
        for f in clips_dir.iterdir():
            f.unlink(missing_ok=True)

    logger.info(
        f"EDL v{version}: {len(edl.segments)} segments, "
        f"{len(edl.all_items())} items, ~{edl.estimated_duration():.0f}s"
    )
    return edl, version
