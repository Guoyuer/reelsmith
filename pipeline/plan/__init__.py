"""Stage 3: Generate EDL — select photos/videos and arrange into a narrative.

Uses the visual planner (Gemini 3 Flash): Gemini sees actual photos via
individual photo thumbnails and watches video clips (with audio) to create an EDL.

Requires GEMINI_API_KEY in .env.
"""

from ._orchestrate import plan, PlanConfig

# Re-exports for backward compatibility (tests import these by name)
from ._prompts import (  # noqa: F401
    TRIP_TYPES,
    _PROMPTS_DIR,
    _default_focus,
    _format_date_range,
    _load_json,
    _load_lang_instructions,
    _load_narrative_guidance,
    _load_system_template,
    _secs_to_timestamp,
    _timestamp_to_secs,
    _visual_system_prompt,
)
from ._preview import (  # noqa: F401
    _build_visual_chapter_text,
    _build_visual_content_blocks,
)

__all__ = ["plan", "PlanConfig"]
