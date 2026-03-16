"""Stage 3: Generate EDL — algorithmic or API-based planning.

Two backends:
  - "algo" (default): deterministic algorithm using analysis scores
  - "api": Claude API call for narrative-aware planning

Set via PlanConfig.planner or CLI --planner flag.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

from .config import Config
from .edl import EDL, EditItem, Segment, TextOverlay


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def plan(
    cfg: Config,
    *,
    style: str = "upbeat",
    target_duration: int = 180,
    focus: str = "happiness with family",
    planner: str = "algo",
    log_fn=None,
) -> tuple[EDL, int]:
    """Generate an EDL from preprocessed + analysis data.

    *planner*: "algo" for algorithmic, "api" for Claude API.
    """
    _log = log_fn or print
    preprocessed = json.loads((cfg.workspace / "preprocessed.json").read_text())
    analysis_items = json.loads((cfg.workspace / "analysis.json").read_text())
    analysis_by_id: dict[int, dict] = {a["id"]: a for a in analysis_items}

    if planner == "api":
        _log(f"Planning via Claude API (target {target_duration}s, style={style})...")
        edl = _plan_api(cfg, preprocessed, analysis_by_id, analysis_items,
                        style=style, target_duration=target_duration, focus=focus, log_fn=_log)
    else:
        _log(f"Planning algorithmically (target {target_duration}s, style={style})...")
        edl = _plan_auto(preprocessed, analysis_by_id,
                         style=style, target_duration=target_duration)

    # Save versioned EDL and clear clips
    from .iterate import _find_latest_version, _save_edl
    version = _find_latest_version(cfg) + 1
    _save_edl(cfg, edl, version)

    clips_dir = cfg.workspace / "clips"
    if clips_dir.exists():
        for f in clips_dir.iterdir():
            f.unlink(missing_ok=True)

    _log(f"EDL v{version}: {len(edl.segments)} segments, "
         f"{len(edl.all_items())} items, ~{edl.estimated_duration():.0f}s")
    return edl, version


# ---------------------------------------------------------------------------
# Backend 1: Algorithmic planner (default)
# ---------------------------------------------------------------------------

EFFECTS = ["ken_burns_in", "ken_burns_out", "ken_burns_left", "ken_burns_right"]

STYLE_PARAMS = {
    "upbeat":     {"base_dur": 3.5, "vary": 0.5, "transition": "crossfade", "td": 0.6},
    "cinematic":  {"base_dur": 5.0, "vary": 1.0, "transition": "fade_black", "td": 1.2},
    "reflective": {"base_dur": 5.5, "vary": 1.5, "transition": "crossfade", "td": 1.0},
    "energetic":  {"base_dur": 2.5, "vary": 0.5, "transition": "crossfade", "td": 0.4},
}


def _item_score(a: dict) -> float:
    """Score an analyzed item for selection priority."""
    v = a.get("vision", {})
    tier_bonus = {"A": 20, "B": 10, "C": 0}.get(a.get("tier", "C"), 0)
    togetherness = v.get("togetherness", 0)
    emotion = v.get("genuine_emotion", 0)
    quality = v.get("visual_quality", 5)
    return tier_bonus + togetherness * 2 + emotion * 1.5 + quality


def _word_overlap(words_a: set[str], words_b: set[str]) -> float:
    """Jaccard similarity between two word sets."""
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


def _plan_auto(
    preprocessed: dict, analysis_by_id: dict,
    style: str, target_duration: int,
) -> EDL:
    """Deterministic EDL from analysis scores and timeline structure."""
    params = STYLE_PARAMS.get(style, STYLE_PARAMS["upbeat"])
    base_dur = params["base_dur"]
    vary = params["vary"]
    seg_transition = params["transition"]
    seg_td = params["td"]
    effect_cycle = itertools.cycle(EFFECTS)

    # Phase 1: Collect the best candidates from each day, family-first
    days = preprocessed["timeline"]
    n_days = len(days)
    target_items = max(10, int(target_duration / base_dur))
    items_per_day = max(3, target_items // n_days) if n_days else target_items

    day_selections: list[list[tuple[dict, dict, str]]] = []  # [(analysis, chapter, date)]

    for day in days:
        date_str = day["date"]
        day_candidates = []

        for chapter in day["chapters"]:
            location = chapter["location"]
            for item_id in chapter["item_ids"]:
                a = analysis_by_id.get(item_id)
                if not a or not a.get("vision") or a.get("tier") == "D":
                    continue
                v = a["vision"]
                # Skip low quality, transport shots (highway, bus, toll)
                if v.get("visual_quality", 5) < 4:
                    continue
                beat = v.get("story_beat", v.get("scene_type", ""))
                if beat == "transport":
                    continue
                day_candidates.append((a, chapter, date_str))

        # Sort: tier A first, then by score
        day_candidates.sort(key=lambda x: _item_score(x[0]), reverse=True)

        # Pick best items for this day: prioritize family (A/B), limit scenery (C)
        selected = []
        seen_descs: list[set[str]] = []
        c_count = 0
        for a, ch, ds in day_candidates:
            if len(selected) >= items_per_day:
                break
            # Limit scenery to 1/3 of day's items
            if a.get("tier") == "C":
                if c_count >= max(1, items_per_day // 3):
                    continue
                c_count += 1
            # Description dedup
            desc_words = set(a.get("vision", {}).get("description", "").lower().split())
            if any(_word_overlap(desc_words, d) > 0.5 for d in seen_descs):
                continue
            seen_descs.append(desc_words)
            selected.append((a, ch, ds))

        day_selections.append(selected)

    # Phase 2: Build segments — group by day, merge consecutive same-location
    segments: list[Segment] = []
    total_dur = 0.0

    for day_idx, selections in enumerate(day_selections):
        if not selections:
            continue

        # Sort by takentime within the day
        selections.sort(key=lambda x: x[0].get("takentime", 0))

        # Group consecutive items at same location into one segment
        current_items: list[EditItem] = []
        current_location = ""
        current_date = selections[0][2] if selections else ""
        is_first_of_day = True

        for a, ch, date_str in selections:
            location = ch["location"]
            if location == "unknown":
                location = ""

            # New segment if location changes (and current has items)
            if location != current_location and current_items:
                segments.append(Segment(
                    name=current_location or current_date,
                    items=current_items,
                    transition=seg_transition,
                    transition_duration=seg_td,
                ))
                current_items = []

            current_location = location
            tier = a.get("tier", "C")
            dur = base_dur + (vary if tier == "A" else -vary if tier == "C" else 0)

            # Text overlay
            overlay = None
            if is_first_of_day:
                label = f"{date_str}  {location}".strip() if location else date_str
                overlay = TextOverlay(text=label, position="bottom")
                is_first_of_day = False
            elif not current_items and location:
                # First item at new location
                overlay = TextOverlay(text=location, position="bottom")

            current_items.append(EditItem(
                source_file=a["local_path"],
                media_type=a.get("media_type", "photo"),
                display_duration=round(dur, 1),
                effect=next(effect_cycle),
                text_overlay=overlay,
            ))
            total_dur += dur

        # Flush last segment of the day
        if current_items:
            segments.append(Segment(
                name=current_location or current_date,
                items=current_items,
                transition=seg_transition,
                transition_duration=seg_td,
            ))

        # Use fade_black between days (not within a day)
        if segments and day_idx < len(day_selections) - 1:
            segments[-1].transition = "fade_black"
            segments[-1].transition_duration = 1.0

    return EDL(
        title=f"{preprocessed.get('family_names', ['Family'])[0]}'s Trip",
        target_duration=target_duration,
        segments=segments,
    )



# ---------------------------------------------------------------------------
# Backend 2: Claude API planner
# ---------------------------------------------------------------------------

API_SYSTEM_PROMPT = """\
You are a professional travel vlog editor. You create emotionally resonant
highlight reels from family trip photos by selecting and sequencing the best
moments into a cinematic narrative.

You will receive scored photo candidates organized by day/time/location.
Your job: select the best items and arrange them into an EDL (Edit Decision List).

## Narrative principles

1. **Emotional arc**: Build from curiosity (arrival) → joy (discoveries, meals,
   activities) → warmth (family moments) → nostalgia (departure). Every vlog
   should feel like a complete story.

2. **Family is the heart**: Tier A items (2+ family members together) are the
   emotional core. Use their togetherness and genuine_emotion scores to find
   the most authentic moments — real laughter > posed smiles.

3. **Rhythm and pacing**: Alternate between wide establishing shots (tier C)
   and intimate family moments (tier A/B). Vary display_duration: shorter for
   action/movement (3s), longer for emotional beats (5s). Use Ken Burns effects
   that match the mood — slow zoom-in for intimate moments, pan for landscapes.

4. **Visual storytelling**: Use story_beat and description to create variety —
   don't follow 3 "meal" shots with another "meal" shot. Each chapter should
   open with a scene-setter and close with its best moment.

5. **Text overlays**: Add location names when the setting changes and dates
   at the start of each new day. Keep text minimal — let the visuals speak.

## Technical rules

- display_duration: 3-5s per photo (vary for rhythm)
- Segments: one per location/chapter, 3-8 items each
- Transitions: crossfade within segments, fade_black between segments
- Skip chapters with no good candidates
- CRITICAL: source_file must be the EXACT local_path value from the input data

Output valid JSON only:
{
  "title": "string",
  "target_duration": <seconds>,
  "resolution": [3840, 2160],
  "fps": 60,
  "segments": [
    {
      "name": "Chapter Name",
      "items": [
        {
          "source_file": "<exact local_path>",
          "media_type": "photo",
          "display_duration": 3.0-5.0,
          "effect": "ken_burns_in|ken_burns_out|ken_burns_left|ken_burns_right|static",
          "text_overlay": null or {"text": "string", "position": "bottom", "font_size": 48}
        }
      ],
      "transition": "crossfade|fade_black",
      "transition_duration": 0.8
    }
  ],
  "music": null
}"""


def _plan_api(
    cfg: Config, preprocessed: dict, analysis_by_id: dict,
    analysis_items: list[dict],
    style: str, target_duration: int, focus: str, log_fn,
) -> EDL:
    """Use Claude API to generate an EDL with narrative awareness."""
    import anthropic

    chapters_text = _build_chapters_prompt(preprocessed, analysis_by_id)

    n_items = target_duration // 4
    user_message = f"""\
Create a {style} family trip vlog EDL.

**Brief**: {target_duration}s (~{target_duration // 60}m{target_duration % 60:02d}s) highlight reel.
Focus: {focus}.
Family: {', '.join(preprocessed['family_names'])}
Select ~{n_items} items (at ~4s each = {n_items * 4}s).

**Scored candidates by day/location** (use the `path:` values as source_file):
{chapters_text}

Craft a narrative that tells the story of this trip — not just the best-scored
photos in order, but a sequence that builds emotion and feels like a journey."""

    client = anthropic.Anthropic()  # uses ANTHROPIC_API_KEY env var
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8192,
        system=API_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    content = response.content[0].text
    log_fn(f"API response: {response.usage.input_tokens} in, {response.usage.output_tokens} out")

    # Strip markdown fences if present
    from .media_utils import strip_markdown_fences
    content = strip_markdown_fences(content)

    return EDL.model_validate_json(content)


def _build_chapters_prompt(preprocessed: dict, analysis_by_id: dict) -> str:
    """Build a structured text representation of the timeline with scores."""
    lines = []

    for day in preprocessed["timeline"]:
        lines.append(f"\n=== {day['day_name']} {day['date']} ===")

        for chapter in day["chapters"]:
            loc = chapter["location"]
            block = chapter["time_block"]

            ab_items = []
            c_items = []
            for item_id in chapter["item_ids"]:
                a = analysis_by_id.get(item_id)
                if not a or not a.get("vision"):
                    continue
                tier = a.get("tier", "?")
                if tier in ("A", "B", "?"):
                    ab_items.append(a)
                elif tier == "C":
                    c_items.append(a)

            if not ab_items and not c_items:
                continue

            lines.append(f"\n  [{block.upper()}] {loc}")

            for a in ab_items:
                v = a["vision"]
                tog = v.get("togetherness", v.get("happiness_score", "?"))
                emo = v.get("genuine_emotion", "?")
                beat = v.get("story_beat", v.get("scene_type", "?"))
                desc = v.get("description", "")[:60]
                lines.append(
                    f"    [{a.get('tier','?')}] fam={a.get('family_count',0)} "
                    f"tog={tog} emo={emo} beat={beat} | {desc}"
                    f"\n      path: {a['local_path']}"
                )

            c_items.sort(key=lambda x: x["vision"].get("visual_quality", 0), reverse=True)
            for a in c_items[:2]:
                v = a["vision"]
                scene = v.get("scene_type", "?")
                desc = v.get("description", "")[:60]
                lines.append(
                    f"    [C] scene={scene} | {desc}"
                    f"\n      path: {a['local_path']}"
                )

    return "\n".join(lines)


