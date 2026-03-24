"""Prompt loading, formatting helpers, and system prompt builder for the plan stage."""

from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

TRIP_TYPES = {
    "family",
    "solo",
    "food",
    "adventure",
    "architecture",
    "general",
}


def _load_json(name: str) -> dict:
    """Load a JSON file from the prompts directory."""
    path = _PROMPTS_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _load_narrative_guidance() -> dict:
    return _load_json("narrative_guidance.json")


@lru_cache(maxsize=1)
def _load_lang_instructions() -> dict:
    return _load_json("lang_instructions.json")


@lru_cache(maxsize=1)
def _load_system_template() -> str:
    path = _PROMPTS_DIR / "visual_planner_system.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt template not found: {path}")
    return path.read_text(encoding="utf-8")


def _default_focus(trip_type: str) -> str:
    data = _load_narrative_guidance()
    defaults = data.get("_default_focus", {})
    return defaults.get(trip_type, defaults.get("general", "highlights and memorable moments"))


def _video_ratio(trip_type: str) -> int:
    """Minimum video percentage for a trip type (e.g. 70 for family)."""
    data = _load_narrative_guidance()
    ratios = data.get("_video_ratios", {})
    return ratios.get(trip_type, ratios.get("general", 60))


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _format_date_range(dates: list[str]) -> str:
    """Format a list of YYYY-MM-DD dates as 'June 13-16, 2025'."""
    try:
        dts = [datetime.strptime(d, "%Y-%m-%d") for d in dates]
        first, last = min(dts), max(dts)
        if first.month == last.month:
            return f"{first.strftime('%B')} {first.day}-{last.day}, {first.year}"
        return f"{first.strftime('%B')} {first.day} - {last.strftime('%B')} {last.day}, {first.year}"
    except (ValueError, TypeError):
        return ""


def _secs_to_timestamp(secs: float) -> str:
    """Convert seconds to Gemini-style timestamp: MM:SS or H:MM:SS for >=1hr."""
    total = int(round(secs))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _timestamp_to_secs(ts: str) -> float:
    """Parse MM:SS or H:MM:SS timestamp to seconds (supports fractional seconds)."""
    parts = ts.strip().split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    elif len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    return 0.0


# ---------------------------------------------------------------------------
# System prompt builder
# ---------------------------------------------------------------------------


def _visual_system_prompt(trip_type: str, language: str = "en") -> str:
    """System prompt for visual planner — loaded from pipeline/prompts/ files."""
    narrative_data = _load_narrative_guidance()
    lang_data = _load_lang_instructions()
    template = _load_system_template()

    guidance = narrative_data.get(trip_type, narrative_data.get("general", ""))
    lang_instruction = lang_data.get(language, lang_data.get("en", ""))

    return template.format(guidance=guidance, lang_instruction=lang_instruction)
