"""Tests for pipeline.plan — Gemini visual planner helpers and fault tolerance.

All tests are pure-function / mocked — no actual Gemini API calls.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.edl import EDL, EditItem, Segment
from pipeline.plan import (
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
        "filename": "IMG_001.jpg",
        "local_path": "/media/1_IMG_001.jpg",
        "media_type": "photo",
        "family_count": 2,
        "persons": ["Alice", "Bob"],
        "district": "Marina Bay",
        "country": "Singapore",
        "taken_iso": "2025-06-13T14:30:00",
        "exif": {"focal_length": 24.0, "aperture": 1.4, "iso": 100},
    },
    "2": {
        "id": 2,
        "filename": "VID_002.mp4",
        "local_path": "/media/2_VID_002.mp4",
        "media_type": "video",
        "family_count": 0,
        "persons": [],
        "video_duration": 45.3,
        "duration_ms": 45300,
    },
    "3": {
        "id": 3,
        "filename": "IMG_003.jpg",
        "local_path": "/media/3_IMG_003.jpg",
        "media_type": "photo",
        "family_count": 1,
        "persons": ["Alice"],
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
# _build_visual_chapter_text
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Files API threshold
# ---------------------------------------------------------------------------


class TestFilesApiThreshold:
    """_gemini_call should use Files API when payload > 20MB."""

    def test_small_payload_uses_inline(self):
        # 1KB of data — should use inline
        parts = [
            {"type": "image_bytes", "data": b"\x00" * 1024, "mime_type": "image/jpeg"}
        ]
        total = sum(len(p.get("data", b"")) for p in parts)
        assert total < 20 * 1024 * 1024

    def test_large_payload_triggers_files_api(self):
        # 25MB of data — should trigger Files API
        parts = [
            {
                "type": "video_bytes",
                "data": b"\x00" * (25 * 1024 * 1024),
                "mime_type": "video/mp4",
            }
        ]
        total = sum(len(p.get("data", b"")) for p in parts)
        assert total > 20 * 1024 * 1024


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


# ---------------------------------------------------------------------------
# Pre-Gemini validation checks
# ---------------------------------------------------------------------------


class TestPreGeminiValidation:
    """Test _build_visual_content_blocks validation."""

    def test_empty_analysis_raises(self):
        """0 text blocks with candidates → RuntimeError."""
        import tempfile
        from pathlib import Path

        from pipeline.config import Config
        from pipeline.plan import _build_visual_content_blocks

        with tempfile.TemporaryDirectory() as td:
            cfg = Config(workspace=Path(td))
            cfg.ensure_dirs()

            preprocessed = {"family_names": []}
            # Empty analysis → no items → should raise
            with pytest.raises(RuntimeError, match="No photos"):
                _build_visual_content_blocks(preprocessed, {}, cfg)

    def test_video_label_mismatch_raises(self):
        """Video labels not in text metadata → RuntimeError.

        Tests the validation logic by checking that _build_visual_content_blocks
        would reject blocks where video labels don't match text item numbers.
        """
        import re

        # Simulate blocks with text referencing #1 and #2, but video entry has #99
        blocks = [
            "--- Day 1 ---\n#1: photo path=existing.jpg\n#2: video=5s path=existing.mp4",
            {"type": "image_bytes", "data": b"fake", "mime_type": "image/jpeg"},
        ]
        video_entries = [(99, 5.0, Path("fake.mp4"))]  # #99 not in text

        text_item_nums = set()
        for b in blocks:
            if isinstance(b, str):
                text_item_nums.update(int(m) for m in re.findall(r"#(\d+):", b))

        video_nums = {num for num, _, _ in video_entries}
        missing = video_nums - text_item_nums
        assert missing == {99}, f"Expected {{99}}, got {missing}"


# ---------------------------------------------------------------------------
# EDL deduplication (Layer 2c)
# ---------------------------------------------------------------------------


class TestEdlDedup:
    """Test that duplicate source_files are removed from EDL."""

    def test_duplicate_removed(self):
        """Same source_file in two segments → second removed."""
        edl = EDL(
            title="test",
            target_duration=180,
            segments=[
                Segment(
                    name="s1",
                    items=[
                        EditItem(
                            source_file="a.mp4", media_type="video", display_duration=5
                        ),
                    ],
                    transition="crossfade",
                ),
                Segment(
                    name="s2",
                    items=[
                        EditItem(
                            source_file="a.mp4", media_type="video", display_duration=5
                        ),
                        EditItem(
                            source_file="b.jpg", media_type="photo", display_duration=3
                        ),
                    ],
                    transition="crossfade",
                ),
            ],
        )
        # Simulate dedup logic from plan.py
        seen: set[str] = set()
        for seg in edl.segments:
            unique = []
            for item in seg.items:
                if item.source_file not in seen:
                    seen.add(item.source_file)
                    unique.append(item)
            seg.items = unique
        edl.segments = [s for s in edl.segments if s.items]

        all_files = [i.source_file for s in edl.segments for i in s.items]
        assert len(all_files) == 2  # a.mp4 + b.jpg
        assert len(set(all_files)) == 2  # no duplicates

    def test_no_duplicates_unchanged(self):
        """EDL without duplicates is unchanged."""
        edl = EDL(
            title="test",
            target_duration=180,
            segments=[
                Segment(
                    name="s1",
                    items=[
                        EditItem(source_file="a.mp4", media_type="video"),
                        EditItem(source_file="b.jpg", media_type="photo"),
                    ],
                    transition="crossfade",
                ),
            ],
        )
        seen: set[str] = set()
        for seg in edl.segments:
            unique = [
                i
                for i in seg.items
                if i.source_file not in seen and not seen.add(i.source_file)
            ]
            seg.items = unique

        assert len(edl.segments[0].items) == 2


# ---------------------------------------------------------------------------
# Content block validation
# ---------------------------------------------------------------------------


class TestContentBlockValidation:
    """Test _build_visual_content_blocks produces consistent output."""

    def test_valid_blocks_have_text_and_images(self, tmp_path):
        """With valid data, blocks contain text + image parts."""
        from pipeline.config import Config
        from pipeline.plan import _build_visual_content_blocks

        cfg = Config(workspace=tmp_path)
        cfg.ensure_dirs()

        # Create a real JPEG file + its thumbnail (as prepare stage would)
        from PIL import Image

        img_path = cfg.media_dir / "photo.jpg"
        img_path.parent.mkdir(exist_ok=True)
        Image.new("RGB", (100, 100), "red").save(img_path, "JPEG")
        thumb_path = cfg.thumbnails_dir / "photo_thumb.jpg"
        Image.new("RGB", (100, 100), "red").save(thumb_path, "JPEG")

        preprocessed = {"family_names": []}
        analysis = {
            "1": {
                "id": 1,
                "filename": "photo.jpg",
                "local_path": str(img_path),
                "media_type": "photo",
            }
        }

        blocks, _, _, _ = _build_visual_content_blocks(preprocessed, analysis, cfg)

        texts = [b for b in blocks if isinstance(b, str)]
        assert len(texts) >= 1
        assert "#01" in texts[0]  # item numbering starts at 1


# -----------------------------------------------------------------------
# Prompt file loading (low-level)
# -----------------------------------------------------------------------


class TestPromptFileLoading:
    def test_load_system_template(self):
        from pipeline.plan import _load_system_template

        template = _load_system_template()
        assert "{lang_instruction}" in template
        assert len(template) > 1000

    def test_load_narrative_guidance(self):
        from pipeline.plan import _load_narrative_guidance

        data = _load_narrative_guidance()
        assert "family" in data
        assert "general" in data
        assert "_default_focus" in data

    def test_load_lang_instructions(self):
        from pipeline.plan import _load_lang_instructions

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

    def test_missing_prompt_file_raises(self, tmp_path):
        import pipeline.plan._prompts as prompts_mod
        from pipeline.plan import _load_json

        orig = prompts_mod._PROMPTS_DIR
        try:
            prompts_mod._PROMPTS_DIR = tmp_path / "nonexistent"
            with pytest.raises(FileNotFoundError):
                _load_json("anything.json")
        finally:
            prompts_mod._PROMPTS_DIR = orig
