"""Stage 3: Generate EDL using pre-built timeline structure + AI analysis scores."""

from __future__ import annotations

import json
from pathlib import Path

from .config import Config
from .edl import EDL
from .llm import ollama_chat
from .media_utils import strip_markdown_fences

SYSTEM_PROMPT = """\
You are a travel vlog editor creating a highlight reel of a family trip.

You will receive a pre-organized timeline with chapters (day → time_block → location).
Each chapter lists candidate photos with scores. Your job is to SELECT the best
items and arrange them into a vlog EDL.

Selection priorities (most to least important):
1. Family together (tier A, family_count >= 2) — these are the emotional core
2. High togetherness + genuine_emotion scores — real joy over posed shots
3. Story beats: arrivals, discoveries, meals, candid moments > posed photos
4. Visual variety: don't pick 3 similar shots from same cluster
5. Scene-setting shots (tier C) to establish locations — use 1-2 per chapter max

Pacing rules:
- Photos: 3-5 seconds each, use varied Ken Burns effects
- Total duration must match the target
- Open strong (best landmark or family group), close warm (tender family moment)
- Each segment = one chapter from the timeline
- 3-8 items per segment, skip chapters with nothing good
- Use crossfade within segments, fade_black between segments
- Text overlays: location name on first item of each new location, date on first item of each day

Output ONLY valid JSON matching this EDL schema, no other text:
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
          "source_file": "<exact local_path from the data>",
          "media_type": "photo",
          "start_time": null,
          "end_time": null,
          "display_duration": <3-5 seconds>,
          "effect": "ken_burns_in" | "ken_burns_out" | "ken_burns_left" | "ken_burns_right" | "static",
          "text_overlay": null or {"text": "string", "position": "bottom", "font_size": 48}
        }
      ],
      "transition": "crossfade" | "fade_black",
      "transition_duration": 0.8
    }
  ],
  "music": null
}"""


def plan(
    cfg: Config,
    *,
    style: str = "upbeat",
    target_duration: int = 180,
    focus: str = "happiness with family",
    log_fn=None,
) -> EDL:
    """Build structured prompt from timeline + analysis, ask LLM to select and arrange."""
    preprocessed_path = cfg.workspace / "preprocessed.json"
    analysis_path = cfg.workspace / "analysis.json"
    edl_path = cfg.workspace / "edl.json"

    preprocessed = json.loads(preprocessed_path.read_text())
    analysis_items = json.loads(analysis_path.read_text())

    # Index analysis by item ID for quick lookup
    analysis_by_id: dict[int, dict] = {a["id"]: a for a in analysis_items}

    # Build structured chapter-based prompt
    chapters_text = _build_chapters_prompt(preprocessed, analysis_by_id)

    user_message = f"""\
Create a {style} family trip vlog EDL.
Target duration: {target_duration} seconds (~{target_duration // 60}m{target_duration % 60:02d}s).
Focus: {focus}.
Family members: {', '.join(preprocessed['family_names'])}

Timeline with scored candidates:
{chapters_text}

Select ~{target_duration // 4} items total. Pick the warmest, happiest moments."""

    _log = log_fn or print
    _log(f"Planning vlog (target {target_duration}s, model={cfg.planning_model})...")

    content = ollama_chat(cfg, system=SYSTEM_PROMPT, prompt=user_message, log_fn=_log)

    content = strip_markdown_fences(content)

    edl = EDL.model_validate_json(content)
    edl_path.write_text(edl.model_dump_json(indent=2))
    _log(f"EDL saved: {len(edl.segments)} segments, ~{edl.estimated_duration():.0f}s estimated")
    return edl


def _build_chapters_prompt(preprocessed: dict, analysis_by_id: dict) -> str:
    """Build a structured text representation of the timeline with scores.

    Only includes tier A+B items fully, and the best 2 tier-C items per chapter
    to keep prompt size manageable for small LLMs.
    """
    lines = []

    for day in preprocessed["timeline"]:
        lines.append(f"\n=== {day['day_name']} {day['date']} ===")

        for chapter in day["chapters"]:
            loc = chapter["location"]
            block = chapter["time_block"]

            # Collect items for this chapter
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

            # Skip chapters with nothing good
            if not ab_items and not c_items:
                continue

            lines.append(f"\n  [{block.upper()}] {loc}")

            # All tier A+B items (these are the priority)
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

            # Best 2 tier-C items per chapter (by visual_quality)
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
