"""Plan stage orchestration: visual planning via Gemini.

Coordinates content building, API call, and post-processing into a
single public `plan()` entry point.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .._types import AnalysisEntry
from ..config import Config, ProgressCallback
from ..edl import EDL, Effect, MusicMode, MusicTrack, find_latest_version, save_edl
from ._gemini import _gemini_call
from ._postprocess import (
    PostprocessReport,
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
    _trip_guidance,
    _video_ratio,
    _visual_system_prompt,
)

logger = logging.getLogger("reelsmith.plan")


@dataclass
class PlanConfig:
    target_duration: int  # required — no default
    style: str = "upbeat"
    focus: str = ""
    instruct: str = ""
    trip_type: str = "family"
    language: str = "en"
    model: str = ""  # resolved by CLI from --model (required)
    thinking_level: str = "HIGH"  # resolved by CLI from --model preset
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
    analysis_by_path: dict[str, AnalysisEntry],
    pc: PlanConfig,
    *,
    progress_callback: ProgressCallback = None,
) -> EDL:
    """Single-pass Gemini planning with chain-of-thought.

    Gemini sees individual photo thumbnails (400px) + video clips,
    designs narrative arc + selects items + self-reviews in one call.
    """
    content_blocks, preview_offset_table, n_photos, n_videos = (
        _build_visual_content_blocks(analysis_by_path, cfg, force=pc.force)
    )
    n_candidates = n_photos + n_videos

    # Summary
    trip_summary = f"{n_candidates} candidates ({n_videos} videos, {n_photos} photos)."

    n_items = round(pc.target_duration / 5.5)
    vid_ratio = _video_ratio(pc.trip_type)
    trip_label = f"{pc.trip_type} trip" if pc.trip_type != "general" else "trip"
    guidance_text = _trip_guidance(pc.trip_type)

    if progress_callback:
        progress_callback(0, 0, f"{n_photos} photos, {n_videos} videos → Gemini")

    instruct_block = (
        f"\n**User instructions** (follow these; if they conflict with hard "
        f"constraints above, the user's instructions win):\n{pc.instruct}\n"
        if pc.instruct
        else ""
    )

    intro_text = f"""\
Create a {pc.style} {trip_label} vlog EDL from the photos and videos shown below.

{trip_summary}

**FOCUS: {pc.focus}** — This is the creative direction. Every chapter, every selection
decision, and every text overlay should serve this focus. When choosing between two
items of similar quality, pick the one that better supports this focus.

**Trip style**: {guidance_text}

**Hard constraints:**
- DURATION: Sum of ALL display_duration MUST equal {pc.target_duration}s (±5%). This is the #1 requirement.
- Select ~{n_items} items to fill {pc.target_duration}s. Duration is content-driven — let each moment decide its length.
- Video ratio: at least {vid_ratio}% videos for this {trip_label}.
- Photo time cap: total photo duration ≤ {pc.target_duration * 0.3:.0f}s (30% of target). Photos are punctuation, not filler.
- Location diversity: max 3 items per location, spread across all places visited.
{instruct_block}
**Think step-by-step:**

1. **SCAN** — Look at ALL photos carefully. Watch the ENTIRE video preview with audio.
   For videos: look for steady camera, interesting action, reveals, reactions. Reject
   shaky, too dark, static nothing, camera pointing at ground/sky. Prefer landscape
   over portrait (portrait gets blurred background fill).
   For audio: listen for speech, laughter, children's reactions — these are POSITIVE
   signals. A beautiful silent clip still beats a mediocre clip with speech, but between
   two visually similar videos, prefer the one with interesting speech.
   For photos: judge composition, emotion, lighting. Most photos should be skipped.
   Don't rush — your observations here determine the quality of everything that follows.

2. **FIND PEAKS** — Identify 3-5 PEAK MOMENTS: the strongest emotional beats in the
   entire trip. A child's first reaction, family laughing together, an arrival at a
   stunning view, a quiet goodbye. These are your anchors — every other decision
   serves them.

3. **DESIGN ARC** — Build 4-6 narrative chapters around the peaks (story beats, not
   location buckets, aim for 3-6 items per segment). Shape the emotional arc:
   hook → build → peak → breathe → build → climax → gentle close.
   Alternate high-energy sequences with breathing room — constant intensity exhausts
   the viewer. Every item must fit its chapter's theme — never dump unrelated
   leftovers into a chapter just to fill duration.
   **Opening hook**: Your first item plays right after the title card. Make it the
   single most visually striking or emotionally compelling moment — a flash-forward
   to a peak. The viewer decides in 5 seconds. Lead with your best, not "arriving
   at the airport."

4. **SELECT & FILL** — For each chapter, select items around the peak: add supporting
   material that builds anticipation before it and lets emotion breathe after it.
   Fill gaps with variety shots (establishing shots, details, transitions).
   **Videos with speech**: If you heard speech/laughter, trim AROUND THAT MOMENT —
   the speech IS the content. Include 1s padding before and after. Set keep_audio=true.
   Silent or wind-noise-only → keep_audio=false.
   **Photos** (be ruthless): Every photo needs a ROLE — establishing shot (4-5s),
   emotional peak (4-5s), detail bridge between scenes (2-2.5s), breathing room after
   energetic video (3s), or montage fuel (2-2.5s). SKIP: blurry, dark, generic posed,
   repetitive. Vary durations (3.5s → 2.5s → 4s) — never 3+ photos at same length.
   **Dedup**: If two items show the same subject/framing, pick ONE. A segment with
   5+ items from the same place is almost always wrong.

5. **VERIFY** — Check in PRIORITY ORDER (satisfy earlier items first if conflicts arise):
   □ P1 Duration: total display_duration ≈ {pc.target_duration}s (±5%)?
   □ P2 Video ratio: videos ≥ {vid_ratio}% of items?
   □ P3 Location diversity: max 3 items per location? Spread across full trip?
   □ P4 Photo cap: total photo duration ≤ {pc.target_duration * 0.3:.0f}s (30%)?
   □ P5 keep_audio=true on every video where you heard clear speech or laughter?
   □ P6 No duplicate source_file? No more than 2 portrait videos?
   □ P7 Text overlays ≤ 5 total?
   If short, add more items or extend video trims. If long, remove weakest items.

Output ONE JSON EDL.

All candidates:"""

    visual_parts: list = [intro_text] + content_blocks

    system_prompt = _visual_system_prompt(pc.trip_type, language=pc.language)

    edl_content = _gemini_call(
        system_prompt,
        visual_parts,
        label="single pass: plan",
        model=pc.model,
        thinking_level=pc.thinking_level,
        progress_callback=progress_callback,
    )

    logger.debug("Gemini response: %d chars", len(edl_content))
    for line in edl_content.split("\n"):
        logger.debug("  | %s", line)

    # --- Post-processing pipeline ---
    if progress_callback:
        progress_callback(0, 0, "post-processing...")
    edl = parse_and_convert_timestamps(edl_content, preview_offset_table)
    items_before = len(edl.all_items())
    n_path_removed = fix_hallucinated_paths(edl, cfg.media_dir)
    n_trim_fixed, n_trim_removed, n_dur_fixed, dur_delta = validate_trim_points(
        edl, analysis_by_path
    )
    n_dedup = deduplicate_items(edl)
    items_after = len(edl.all_items())

    # Build report and check thresholds
    report = PostprocessReport(
        items_before=items_before,
        items_after=items_after,
        path_removed=n_path_removed,
        trim_clamped=n_trim_fixed,
        trim_removed=n_trim_removed,
        dedup_removed=n_dedup,
        dur_fixed=n_dur_fixed,
        dur_delta=dur_delta,
    )

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
    if n_dur_fixed:
        pp_parts.append(f"{n_dur_fixed} durations corrected ({dur_delta:+.1f}s)")
    if pp_parts:
        logger.info(
            "Post-processing: %s (%d → %d items)",
            ", ".join(pp_parts),
            items_before,
            items_after,
        )
    else:
        logger.info("Post-processing: no changes (%d items)", items_after)

    # Check removal thresholds (raises RuntimeError if >50% removed)
    report.check_thresholds()

    # Rich post-processing diff
    from ..utils import stderr_console

    console = stderr_console()
    if console and (n_path_removed or n_trim_fixed or n_trim_removed or n_dedup):
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
        console.print(t)

    actual_dur = edl.estimated_duration()
    if actual_dur < pc.target_duration * 0.5:
        raise RuntimeError(
            f"EDL is {actual_dur:.0f}s, target is {pc.target_duration}s — "
            f"less than 50% filled. Check Gemini output and post-processing logs."
        )
    if actual_dur < pc.target_duration:
        logger.warning(
            "EDL is %.0fs, target is %ds — underfilled", actual_dur, pc.target_duration
        )

    # Selection coverage: how many of the total candidates were selected
    selected_paths = {item.source_file for item in edl.all_items()}
    n_selected = len(selected_paths)
    logger.info(
        "Selection: %d / %d candidates used (%.0f%%)",
        n_selected,
        n_candidates,
        n_selected / n_candidates * 100 if n_candidates else 0,
    )
    # Coverage by location
    loc_total: dict[str, int] = {}
    loc_selected: dict[str, int] = {}
    for a in analysis_by_path.values():
        loc = a.get("district") or a.get("country") or "unknown"
        loc_total[loc] = loc_total.get(loc, 0) + 1
        if a["local_path"] in selected_paths:
            loc_selected[loc] = loc_selected.get(loc, 0) + 1
    for loc in sorted(loc_total, key=lambda x: loc_total[x], reverse=True)[:10]:
        sel = loc_selected.get(loc, 0)
        tot = loc_total[loc]
        logger.info("  %s: %d/%d selected", loc, sel, tot)

    log_edl_summary(edl, pc.target_duration)

    return edl


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def plan(
    cfg: Config, pc: PlanConfig, *, progress_callback: ProgressCallback = None
) -> tuple[EDL, int]:
    """Generate an EDL from analysis data using the visual planner."""
    from ..prepare import load_analysis

    effective_focus = pc.focus or _default_focus(pc.trip_type)
    analysis_items = load_analysis(cfg)
    analysis_by_path: dict[str, AnalysisEntry] = {
        a["local_path"]: a for a in analysis_items
    }

    # Use a copy with effective_focus applied so _plan_visual gets the resolved focus
    from dataclasses import replace

    visual_pc = replace(pc, focus=effective_focus)
    edl = _plan_visual(
        cfg,
        analysis_by_path,
        visual_pc,
        progress_callback=progress_callback,
    )

    # Set metadata on the EDL before validation — use user's target, not Gemini's
    edl.target_duration = pc.target_duration
    edl.trip_type = pc.trip_type
    edl.style = pc.style
    edl.language = pc.language  # type: ignore[assignment]  # validated by CLI

    # Fix media_type mismatches first (e.g. photo labeled as video),
    # then force effect="none" on actual videos.
    validate_and_fix_edl(edl)
    for seg in edl.segments:
        for item in seg.items:
            if item.media_type == "video" and item.effect != Effect.NONE:
                item.effect = Effect.NONE
    if not edl.date_range:
        all_dates = sorted(
            {a["taken_at"][:10] for a in analysis_by_path.values() if a.get("taken_at")}
        )
        edl.date_range = _format_date_range(all_dates) if all_dates else ""

    # Store music intent
    if pc.music_file and pc.music_file != "auto" and Path(pc.music_file).exists():
        logger.info("Attaching music file: %s", pc.music_file)
        edl.music = MusicTrack(file=pc.music_file)
        edl.music_mode = MusicMode.FILE
    elif pc.music_file == "auto":
        edl.music_mode = MusicMode.AUTO
        logger.info("Music mode: auto (will generate in generate_music step)")

    version = find_latest_version(cfg) + 1
    save_edl(cfg, edl, version)

    render_dir = cfg.render_dir
    if render_dir.exists():
        for f in render_dir.iterdir():
            f.unlink(missing_ok=True)

    return edl, version
