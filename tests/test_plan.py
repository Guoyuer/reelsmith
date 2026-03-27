"""Tests for pipeline.plan — Gemini visual planner helpers and fault tolerance.

All tests are pure-function / mocked — no actual Gemini API calls.
"""

from __future__ import annotations

import pytest

from pipeline.edl import EDL, EditItem, Segment
from pipeline.plan._prompts import (
    _default_focus,
    _format_date_range,
    _visual_system_prompt,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_edl(items: list | None = None) -> EDL:
    """Build a minimal EDL for testing."""
    if items is None:
        items = [
            EditItem(
                source_file="/media/photo1.jpg",
                media_type="photo",
                display_duration=4.0,
            ),
            EditItem(
                source_file="/media/photo2.jpg",
                media_type="photo",
                display_duration=4.0,
            ),
        ]
    return EDL(
        title="Test",
        target_duration=10.0,
        segments=[Segment(name="Seg1", items=items, transition="cut")],
        intro_style="none",
        outro_style="none",
    )


SAMPLE_ANALYSIS = {
    "1": {
        "id": 1,
        "local_path": "/media/1_IMG_001.jpg",
        "media_type": "photo",
        "district": "Marina Bay",
        "country": "Singapore",
        "taken_iso": "2025-06-13T14:30:00",
        "exif": {"focal_length": 24.0, "aperture": 1.4, "iso": 100},
    },
    "2": {
        "id": 2,
        "local_path": "/media/2_VID_002.mp4",
        "media_type": "video",
        "video_duration": 45.3,
    },
    "3": {
        "id": 3,
        "local_path": "/media/3_IMG_003.jpg",
        "media_type": "photo",
    },
}

SAMPLE_CHAPTER = {
    "time_block": "afternoon",
    "location": "Marina Bay",
    "item_ids": [1, 2, 3],
}

SAMPLE_DAY = {
    "date": "2025-06-13",
    "day_name": "Friday",
}


# ---------------------------------------------------------------------------
# _default_focus
# ---------------------------------------------------------------------------


class TestDefaultFocus:
    def test_family_default(self):
        assert (
            "family" in _default_focus("family").lower()
            or "happiness" in _default_focus("family").lower()
        )

    def test_solo_default(self):
        assert (
            "journey" in _default_focus("solo").lower()
            or "discovery" in _default_focus("solo").lower()
        )

    def test_all_trip_types_have_focus(self):
        for tt in ["family", "solo", "food", "adventure", "architecture", "general"]:
            result = _default_focus(tt)
            assert len(result) > 5, f"{tt} focus too short: {result}"

    def test_unknown_falls_back(self):
        assert _default_focus("nonexistent") == _default_focus("general")


# ---------------------------------------------------------------------------
# _format_date_range (cross-platform, no %-d)
# ---------------------------------------------------------------------------


class TestFormatDateRange:
    def test_same_month(self):
        result = _format_date_range(["2025-06-13", "2025-06-14", "2025-06-16"])
        assert result == "June 13-16, 2025"

    def test_multi_month(self):
        result = _format_date_range(
            ["2025-06-28", "2025-06-30", "2025-07-01", "2025-07-03"]
        )
        assert result == "June 28 - July 3, 2025"

    def test_single_date(self):
        result = _format_date_range(["2024-03-05"])
        assert "March" in result and "2024" in result

    def test_empty_list(self):
        assert _format_date_range([]) == ""

    def test_no_zero_padding(self):
        result = _format_date_range(["2025-01-03", "2025-02-05"])
        assert "January 3" in result
        assert "February 5" in result


# ---------------------------------------------------------------------------
# _visual_system_prompt
# ---------------------------------------------------------------------------


class TestVisualSystemPrompt:
    def test_contains_narrative_guidance(self):
        prompt = _visual_system_prompt("family")
        assert "family" in prompt.lower() or "happiness" in prompt.lower()

    def test_different_trip_types(self):
        # System prompt no longer varies by trip type (guidance moved to user prompt).
        # Verify different languages produce different system prompts instead.
        en = _visual_system_prompt("family", language="en")
        cn = _visual_system_prompt("family", language="cn")
        assert en != cn

    def test_cn_language_instruction(self):
        prompt = _visual_system_prompt("family", language="cn")
        assert "中文" in prompt or "Chinese" in prompt or "chinese" in prompt

    def test_contains_edl_schema(self):
        prompt = _visual_system_prompt("family")
        assert "source_file" in prompt
        assert "display_duration" in prompt
        assert "keep_audio" in prompt
        assert "playback_speed" in prompt
        assert "music_mood" in prompt

    def test_contains_transition_options(self):
        prompt = _visual_system_prompt("family")
        assert "crossfade" in prompt
        assert "cut" in prompt


# ---------------------------------------------------------------------------
# EDL field completeness
# ---------------------------------------------------------------------------


class TestEdlFieldCompleteness:
    """Verify all Gemini-controlled fields have sensible defaults."""

    def test_keep_audio_defaults_false(self):
        item = EditItem(source_file="a.mp4", media_type="video")
        assert item.keep_audio is False

    def test_playback_speed_defaults_1(self):
        item = EditItem(source_file="a.mp4", media_type="video")
        assert item.playback_speed == 1.0

    def test_color_temp_defaults_neutral(self):
        seg = Segment(name="test", items=[], transition="crossfade")
        assert seg.color_temp == "neutral"

    def test_mode_defaults_narrative(self):
        seg = Segment(name="test", items=[], transition="crossfade")
        assert seg.mode == "narrative"

    def test_transition_options_accepted(self):
        for tr in [
            "crossfade",
            "dissolve",
            "smoothleft",
            "smoothright",
            "circlecrop",
            "fade_black",
            "wipe_left",
            "cut",
        ]:
            seg = Segment(name="t", items=[], transition=tr)
            assert seg.transition == tr


# -----------------------------------------------------------------------
# Prompt file loading (low-level)
# -----------------------------------------------------------------------


class TestPromptFileLoading:
    def test_load_system_template(self):
        from pipeline.plan._prompts import _load_system_template

        template = _load_system_template()
        assert "{lang_instruction}" in template
        assert len(template) > 1000

    def test_load_narrative_guidance(self):
        from pipeline.plan._prompts import _load_narrative_guidance

        data = _load_narrative_guidance()
        assert "family" in data
        assert "general" in data
        assert "_default_focus" in data

    def test_load_lang_instructions(self):
        from pipeline.plan._prompts import _load_lang_instructions

        data = _load_lang_instructions()
        assert "en" in data
        assert "cn" in data
        assert "both" in data

    def test_unknown_trip_type_falls_back(self):
        # Guidance moved to user prompt; system prompt no longer contains trip-type text.
        # Verify _trip_guidance falls back to "general" for unknown trip types.
        from pipeline.plan._prompts import _trip_guidance

        guidance = _trip_guidance("nonexistent")
        assert "Balanced storytelling" in guidance

    def test_missing_prompt_file_raises(self, tmp_path, monkeypatch):
        import pipeline.plan._prompts as prompts_mod
        from pipeline.plan._prompts import _load_json

        monkeypatch.setattr(prompts_mod, "_PROMPTS_DIR", tmp_path / "nonexistent")
        with pytest.raises(FileNotFoundError):
            _load_json("anything.json")
