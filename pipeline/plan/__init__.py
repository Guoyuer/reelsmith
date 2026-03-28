"""Stage 2: Generate EDL — select photos/videos and arrange into a narrative.

Uses the visual planner (Gemini 3 Flash): Gemini sees actual photos via
individual photo thumbnails and watches video clips (with audio) to create an EDL.

Requires GEMINI_API_KEY in .env.
"""

from ._orchestrate import PlanConfig, plan

__all__ = ["plan", "PlanConfig"]
