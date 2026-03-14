"""Stage 3: Generate an Edit Decision List using Claude as the narrative director."""

from __future__ import annotations

import json
from pathlib import Path

import anthropic

from .config import Config
from .edl import EDL

SYSTEM_PROMPT = """\
You are a professional travel vlog editor. Given analyzed media from a trip,
create an Edit Decision List (EDL) for a short, engaging vlog.

Your goals:
- Tell a story: opening → highlights → closing
- Prioritize joyful, happy moments (high happiness_score)
- Include variety: mix food, landmarks, family, activities
- Follow roughly chronological order but group by theme within segments
- Keep pacing dynamic: alternate between photos (3-5s) and video clips (3-8s)
- Skip low-quality items (quality_score < 5) unless they capture a unique moment
- Target the specified duration

Output ONLY valid JSON matching this schema, no other text:
{
  "title": "string",
  "target_duration": <seconds>,
  "resolution": [1920, 1080],
  "fps": 30,
  "segments": [
    {
      "name": "Segment Name",
      "items": [
        {
          "source_file": "path/to/file",
          "media_type": "photo" or "video",
          "start_time": null or <seconds for video trim start>,
          "end_time": null or <seconds for video trim end>,
          "display_duration": <seconds on screen>,
          "effect": "ken_burns_in" | "ken_burns_out" | "ken_burns_left" | "ken_burns_right" | "static" | "none",
          "text_overlay": null or {"text": "string", "position": "bottom", "font_size": 48}
        }
      ],
      "transition": "crossfade" | "cut" | "fade_black",
      "transition_duration": 0.8
    }
  ],
  "music": null
}

Rules:
- source_file must be the exact local_path from the analysis
- For videos, set start_time/end_time to select the best portion
- For photos, use varied Ken Burns effects for visual interest
- First segment should be a strong opening (best landmark or group shot)
- Last segment should feel like a warm closing
- Add text overlays sparingly: location names, dates for section headers only
- Each segment should have 3-8 items
- Use crossfade transitions between items, fade_black between major segments"""


def plan(
    cfg: Config,
    *,
    style: str = "upbeat",
    target_duration: int = 180,
    focus: str = "happiness with family",
) -> EDL:
    """Read analysis.json, ask Claude to create an EDL, save to edl.json."""
    analysis_path = cfg.workspace / "analysis.json"
    edl_path = cfg.workspace / "edl.json"

    analysis = json.loads(analysis_path.read_text())

    # Build summary for the LLM (full analysis can be very large)
    media_summary = []
    for item in analysis:
        summary = {
            "local_path": item["local_path"],
            "filename": item["filename"],
            "media_type": item["media_type"],
            "taken_iso": item.get("taken_iso"),
            "duration_sec": round(item["duration_ms"] / 1000, 1) if item.get("duration_ms") else None,
            "persons": item.get("persons", []),
            "location": item.get("district") or item.get("first_level") or item.get("country"),
        }
        if item.get("vision"):
            v = item["vision"]
            summary["description"] = v.get("description")
            summary["happiness"] = v.get("happiness_score")
            summary["quality"] = v.get("quality_score")
            summary["scene"] = v.get("scene_type")
            summary["vlog_worthy"] = v.get("vlog_worthy")
        if item.get("transcript"):
            summary["transcript"] = item["transcript"][:200]
        media_summary.append(summary)

    user_message = f"""\
Create a {style} travel vlog EDL from these {len(media_summary)} media items.
Target duration: {target_duration} seconds (~{target_duration // 60} min {target_duration % 60}s).
Focus: {focus}.

Media items (chronological):
{json.dumps(media_summary, indent=2)}"""

    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
    print(f"Planning vlog ({len(media_summary)} items, target {target_duration}s)...")

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8192,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    content = response.content[0].text.strip()
    # Handle markdown code blocks
    if content.startswith("```"):
        content = content.split("\n", 1)[1].rsplit("```", 1)[0]

    edl = EDL.model_validate_json(content)
    edl_path.write_text(edl.model_dump_json(indent=2))
    print(f"EDL saved: {len(edl.segments)} segments, ~{edl.estimated_duration():.0f}s estimated")
    return edl
