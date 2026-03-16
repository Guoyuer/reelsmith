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

    segments: list[Segment] = []
    total_dur = 0.0
    prev_location = None

    for day in preprocessed["timeline"]:
        date_str = day["date"]
        is_first_item_of_day = True

        for chapter in day["chapters"]:
            location = chapter["location"]

            # Gather scored candidates for this chapter
            candidates = []
            for item_id in chapter["item_ids"]:
                a = analysis_by_id.get(item_id)
                if not a or not a.get("vision"):
                    continue
                if a.get("tier") == "D":
                    continue
                candidates.append(a)

            if not candidates:
                continue

            # Sort by score, pick top items
            candidates.sort(key=_item_score, reverse=True)

            # Tier A/B first, then best C items for scene-setting
            ab = [c for c in candidates if c.get("tier") in ("A", "B")]
            c_items = [c for c in candidates if c.get("tier") == "C"][:2]

            # Lead with a scene-setter if available, then family shots
            selected = c_items[:1] + ab + c_items[1:]

            # Cap per chapter: 3-8 items depending on how much budget remains
            budget_items = max(3, int((target_duration - total_dur) / base_dur))
            selected = selected[:min(8, budget_items)]

            if not selected:
                continue

            # Vary durations: tier A gets longer, tier C shorter
            items: list[EditItem] = []
            for i, a in enumerate(selected):
                tier = a.get("tier", "C")
                dur = base_dur + (vary if tier == "A" else -vary if tier == "C" else 0)

                # Text overlay: location on first item of new location, date on first of day
                overlay = None
                if location != prev_location:
                    overlay = TextOverlay(text=location, position="bottom")
                    prev_location = location
                if is_first_item_of_day:
                    overlay = TextOverlay(text=f"{date_str}  {location}", position="bottom")
                    is_first_item_of_day = False

                items.append(EditItem(
                    source_file=a["local_path"],
                    media_type=a.get("media_type", "photo"),
                    display_duration=round(dur, 1),
                    effect=next(effect_cycle),
                    text_overlay=overlay,
                ))
                total_dur += dur

            segments.append(Segment(
                name=f"{chapter['time_block'].replace('_', ' ').title()} — {location}",
                items=items,
                transition=seg_transition,
                transition_duration=seg_td,
            ))

            if total_dur >= target_duration:
                break
        if total_dur >= target_duration:
            break

    # Ensure strong opening (highest-scored landmark/family) and warm closing
    if segments and len(segments[0].items) > 1:
        all_first = segments[0].items
        best_open = max(range(len(all_first)),
                        key=lambda i: _item_score(analysis_by_id.get(
                            _id_from_path(all_first[i].source_file, analysis_by_id), {})))
        if best_open != 0:
            all_first[0], all_first[best_open] = all_first[best_open], all_first[0]

    return EDL(
        title=f"{preprocessed.get('family_names', ['Family'])[0]}'s Trip",
        target_duration=target_duration,
        segments=segments,
    )


def _id_from_path(path: str, analysis_by_id: dict) -> int:
    """Find item ID from a local_path."""
    for aid, a in analysis_by_id.items():
        if a.get("local_path") == path:
            return aid
    return 0


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
        model="claude-sonnet-4-6-20250514",
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


