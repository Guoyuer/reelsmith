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
    _video_ratio,
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
    content_blocks, preview_offset_table, n_photos, n_videos = (
        _build_visual_content_blocks(preprocessed, analysis_by_id, cfg, force=pc.force)
    )
    n_candidates = n_photos + n_videos

    # Summary
    trip_summary = f"{n_candidates} candidates ({n_videos} videos, {n_photos} photos)."

    n_items = round(pc.target_duration / 5.5)
    vid_ratio = _video_ratio(pc.trip_type)
    trip_label = f"{pc.trip_type} trip" if pc.trip_type != "general" else "trip"
    family_line = ""
    if pc.trip_type == "family" and preprocessed.get("family_names"):
        family_line = f"\nFamily: {', '.join(preprocessed['family_names'])}"

    if progress_callback:
        progress_callback(0, 0, f"{n_photos} photos, {n_videos} videos → Gemini")

    intro_text = f"""\
Create a {pc.style} {trip_label} vlog EDL from the photos and videos shown below.

{trip_summary}{family_line}

**FOCUS: {pc.focus}** — This is the creative direction. Every chapter, every selection
decision, and every text overlay should serve this focus. When choosing between two
items of similar quality, pick the one that better supports this focus.

DURATION: Sum of ALL display_duration MUST equal {pc.target_duration}s (±5%). This is the #1 hard requirement.
Select ~{n_items} items to fill {pc.target_duration}s. Duration is content-driven — let each moment decide its length.
Video ratio: at least {vid_ratio}% videos for this {trip_label}.

**Think step-by-step:**
1. Look at ALL photos and watch the video preview. Identify the best moments.
2. Design a narrative arc — 4-6 chapters based on STORY BEATS (aim for 3-6 items per segment).
3. Select items for each chapter. Verify: sum of display_duration ≈ {pc.target_duration}s (±5%).
   If short, add more items or extend video trims. If long, remove weakest items.
4. Self-review — check in PRIORITY ORDER (satisfy earlier items first if conflicts arise):
   □ P1 Duration: total display_duration ≈ {pc.target_duration}s (±5%)?
   □ P2 Video ratio: videos ≥ {vid_ratio}% of items?
   □ P3 Location diversity: max 3 items per location? Spread across full trip?
   □ P4 Photo cap: total photo duration ≤ {pc.target_duration * 0.3:.0f}s (30%)?
   □ P5 keep_audio=true on every video where you heard clear speech or laughter?
   □ P6 No duplicate source_file? No more than 2 portrait videos?
   □ P7 Text overlays ≤ 5 total?

Output ONE JSON EDL.

All candidates:"""

    visual_parts: list = [intro_text] + content_blocks

    system_prompt = _visual_system_prompt(pc.trip_type, language=pc.language)

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

    logger.info(f"Gemini response: {len(edl_content)} chars")
    for line in edl_content.split("\n"):
        logger.debug(f"  | {line}")

    # --- Post-processing pipeline ---
    if progress_callback:
        progress_callback(0, 0, "post-processing...")
    edl = parse_and_convert_timestamps(edl_content, preview_offset_table)
    items_before = len(edl.all_items())
    n_path_removed = fix_hallucinated_paths(edl, cfg.media_dir)
    n_trim_fixed, n_trim_removed = validate_trim_points(edl, analysis_by_id)
    n_dedup = deduplicate_items(edl)
    items_after = len(edl.all_items())

    # Log post-processing summary at INFO
    pp_parts = []
    if n_path_removed:
        pp_parts.append(f"{n_path_removed} bad paths removed")
    if n_trim_fixed:
        pp_parts.append(f"{n_trim_fixed} trims clamped")
    if n_trim_removed:
        pp_parts.append(f"{n_trim_removed} bad trims removed")
    if n_dedup:
        pp_parts.append(f"{n_dedup} duplicates removed")
    if pp_parts:
        logger.info(
            f"Post-processing: {', '.join(pp_parts)} ({items_before} → {items_after} items)"
        )
    else:
        logger.info(f"Post-processing: no changes ({items_after} items)")

    # Rich post-processing diff
    try:
        import sys

        if sys.stderr.isatty() and (
            n_path_removed or n_trim_fixed or n_trim_removed or n_dedup
        ):
            from rich.console import Console
            from rich.table import Table

            t = Table(title="Post-processing", border_style="dim", title_style="bold")
            t.add_column("Step")
            t.add_column("Result", justify="right")
            if n_path_removed:
                t.add_row("Items removed (bad path)", f"[red]{n_path_removed}[/red]")
            if n_trim_fixed:
                t.add_row("Trim points clamped", f"[yellow]{n_trim_fixed}[/yellow]")
            if n_trim_removed:
                t.add_row("Items removed (bad trim)", f"[red]{n_trim_removed}[/red]")
            if n_dedup:
                t.add_row("Duplicates removed", f"[yellow]{n_dedup}[/yellow]")
            t.add_section()
            t.add_row("[bold]Items", f"[bold]{items_before} \u2192 {items_after}")
            Console(stderr=True).print(t)
    except ImportError:
        pass

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

    # Use a copy with effective_focus applied so _plan_visual gets the resolved focus
    visual_pc = PlanConfig(
        style=pc.style,
        target_duration=pc.target_duration,
        focus=effective_focus,
        trip_type=pc.trip_type,
        language=pc.language,
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

    # Validate AFTER effect fix (avoids noise from effect errors)
    validate_and_fix_edl(edl)

    # Set metadata on the EDL — use user's target, not Gemini's
    edl.target_duration = pc.target_duration
    edl.trip_type = pc.trip_type
    edl.style = pc.style
    edl.language = pc.language  # type: ignore[assignment]  # validated by CLI
    edl.intro_style = edl.intro_style or "title_card"
    edl.outro_style = edl.outro_style or "fade_title"
    if not edl.date_range:
        all_dates = sorted(
            {a["taken_iso"][:10] for a in analysis_by_id.values() if a.get("taken_iso")}
        )
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

    return edl, version
