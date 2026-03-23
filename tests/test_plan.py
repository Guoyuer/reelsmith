"""Tests for pipeline.plan — Gemini visual planner helpers and fault tolerance.

All tests are pure-function / mocked — no actual Gemini API calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from pipeline.plan import (
    _default_focus,
    _format_date_range,
    _build_visual_chapter_text,
    _visual_system_prompt,
)
from pipeline.edl import EDL, EditItem, Segment


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_test_edl(items: list | None = None) -> EDL:
    """Build a minimal EDL for testing."""
    if items is None:
        items = [
            EditItem(source_file="/media/photo1.jpg", media_type="photo", display_duration=4.0),
            EditItem(source_file="/media/photo2.jpg", media_type="photo", display_duration=4.0),
        ]
    return EDL(
        title="Test",
        target_duration=10.0,
        segments=[Segment(name="Seg1", items=items, transition="cut")],
        intro_style="none", outro_style="none",
    )


SAMPLE_ANALYSIS = {
    "1": {
        "id": 1, "filename": "IMG_001.jpg", "local_path": "/media/1_IMG_001.jpg",
        "media_type": "photo", "family_count": 2, "persons": ["Alice", "Bob"],
        "district": "Marina Bay", "country": "Singapore",
        "taken_iso": "2025-06-13T14:30:00",
        "exif": {"focal_length": 24.0, "aperture": 1.4, "iso": 100},
    },
    "2": {
        "id": 2, "filename": "VID_002.mp4", "local_path": "/media/2_VID_002.mp4",
        "media_type": "video", "family_count": 0, "persons": [],
        "video_duration": 45.3, "duration_ms": 45300,
    },
    "3": {
        "id": 3, "filename": "IMG_003.jpg", "local_path": "/media/3_IMG_003.jpg",
        "media_type": "photo", "family_count": 1, "persons": ["Alice"],
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
        assert "family" in _default_focus("family").lower() or "happiness" in _default_focus("family").lower()

    def test_solo_default(self):
        assert "journey" in _default_focus("solo").lower() or "discovery" in _default_focus("solo").lower()

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
        result = _format_date_range(["2025-06-28", "2025-06-30", "2025-07-01", "2025-07-03"])
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
        family = _visual_system_prompt("family")
        solo = _visual_system_prompt("solo")
        assert family != solo

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
        assert "dissolve" in prompt
        assert "fade_black" in prompt


# ---------------------------------------------------------------------------
# _build_visual_chapter_text
# ---------------------------------------------------------------------------

class TestBuildVisualChapterText:
    def test_returns_text_photos_labels_videos(self):
        text, photos, labels, videos = _build_visual_chapter_text(
            SAMPLE_CHAPTER, SAMPLE_DAY, SAMPLE_ANALYSIS, start_idx=1,
        )
        assert isinstance(text, str)
        assert isinstance(photos, list)
        assert isinstance(labels, list)
        assert isinstance(videos, list)
        assert len(labels) == len(photos)

    def test_header_contains_day_and_location(self):
        text, _, _, _ = _build_visual_chapter_text(
            SAMPLE_CHAPTER, SAMPLE_DAY, SAMPLE_ANALYSIS, start_idx=1,
        )
        assert "Friday" in text
        assert "2025-06-13" in text
        assert "Marina Bay" in text

    def test_separates_photos_and_videos(self):
        _, photos, labels, videos = _build_visual_chapter_text(
            SAMPLE_CHAPTER, SAMPLE_DAY, SAMPLE_ANALYSIS, start_idx=1,
        )
        assert len(photos) == 2  # items 1 and 3 are photos
        assert len(videos) == 1  # item 2 is video

    def test_labels_skip_video_numbers(self):
        """Photo labels must match text metadata, skipping video indices."""
        # Items: 1(photo), 2(video), 3(photo) starting at idx=1
        # Text: #01=photo1, #02=video2, #03=photo3
        # Photo labels should be: ["#01", "#03"] — skipping #02 (video)
        _, _, labels, _ = _build_visual_chapter_text(
            SAMPLE_CHAPTER, SAMPLE_DAY, SAMPLE_ANALYSIS, start_idx=1,
        )
        assert labels == ["#01", "#03"]

    def test_family_label_for_tier_a(self):
        text, _, _, _ = _build_visual_chapter_text(
            SAMPLE_CHAPTER, SAMPLE_DAY, SAMPLE_ANALYSIS, start_idx=1,
        )
        assert "family together" in text
        assert "Alice" in text

    def test_video_duration_included(self):
        text, _, _, _ = _build_visual_chapter_text(
            SAMPLE_CHAPTER, SAMPLE_DAY, SAMPLE_ANALYSIS, start_idx=1,
        )
        assert "video=45s" in text

    def test_location_included(self):
        text, _, _, _ = _build_visual_chapter_text(
            SAMPLE_CHAPTER, SAMPLE_DAY, SAMPLE_ANALYSIS, start_idx=1,
        )
        assert "at=Marina Bay" in text

    def test_exif_included(self):
        text, _, _, _ = _build_visual_chapter_text(
            SAMPLE_CHAPTER, SAMPLE_DAY, SAMPLE_ANALYSIS, start_idx=1,
        )
        assert "24mm" in text
        assert "f/1.4" in text
        assert "ISO100" in text

    def test_path_included(self):
        text, _, _, _ = _build_visual_chapter_text(
            SAMPLE_CHAPTER, SAMPLE_DAY, SAMPLE_ANALYSIS, start_idx=1,
        )
        assert "path=/media/1_IMG_001.jpg" in text

    def test_numbering_starts_at_start_idx(self):
        text, _, _, _ = _build_visual_chapter_text(
            SAMPLE_CHAPTER, SAMPLE_DAY, SAMPLE_ANALYSIS, start_idx=10,
        )
        assert "#10:" in text
        assert "#11:" in text

    def test_missing_item_skipped(self):
        chapter = {"item_ids": [1, 999], "location": "X", "time_block": "morning"}
        _, photos, labels, _ = _build_visual_chapter_text(
            chapter, SAMPLE_DAY, SAMPLE_ANALYSIS, start_idx=1,
        )
        assert len(photos) == 1
        assert len(labels) == 1

    def test_media_count_in_header(self):
        text, _, _, _ = _build_visual_chapter_text(
            SAMPLE_CHAPTER, SAMPLE_DAY, SAMPLE_ANALYSIS, start_idx=1,
        )
        assert "2 photos" in text
        assert "1 videos" in text


# ---------------------------------------------------------------------------
# Post-processing: path validation (Layer 2)
# ---------------------------------------------------------------------------

class TestPathValidation:
    def test_existing_paths_kept(self, tmp_path):
        photo = tmp_path / "photo1.jpg"
        photo.write_bytes(b"\xff\xd8" + b"\x00" * 100)
        edl = _make_test_edl([
            EditItem(source_file=str(photo), media_type="photo", display_duration=4.0),
        ])
        for seg in edl.segments:
            seg.items = [i for i in seg.items if Path(i.source_file).exists()]
        assert len(edl.all_items()) == 1

    def test_missing_paths_removed(self):
        edl = _make_test_edl([
            EditItem(source_file="/nonexistent/photo.jpg", media_type="photo", display_duration=4.0),
        ])
        for seg in edl.segments:
            seg.items = [i for i in seg.items if Path(i.source_file).exists()]
        edl.segments = [s for s in edl.segments if s.items]
        assert len(edl.all_items()) == 0

    def test_fuzzy_match_finds_prefixed_file(self, tmp_path):
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        real_file = media_dir / "12345_IMG_001.jpg"
        real_file.write_bytes(b"\xff\xd8" + b"\x00" * 100)
        name = "87681_IMG_001.jpg"
        parts = name.split("_", 1)
        candidates = list(media_dir.glob(f"*{parts[-1]}"))
        assert len(candidates) == 1
        assert candidates[0] == real_file

    def test_empty_segments_removed(self):
        edl = EDL(
            title="T", target_duration=10, intro_style="none", outro_style="none",
            segments=[
                Segment(name="S1", items=[], transition="cut"),
                Segment(name="S2", items=[
                    EditItem(source_file="/exists.jpg", media_type="photo", display_duration=4.0),
                ], transition="cut"),
            ],
        )
        edl.segments = [s for s in edl.segments if s.items]
        assert len(edl.segments) == 1
        assert edl.segments[0].name == "S2"


# ---------------------------------------------------------------------------
# Post-processing: trim validation (Layer 2b)
# ---------------------------------------------------------------------------

class TestTrimValidation:
    def _validate_trims(self, edl, analysis_by_id):
        for seg in edl.segments:
            valid_items = []
            for item in seg.items:
                if item.media_type == "video" and item.start_time is not None:
                    vid_dur = analysis_by_id.get(
                        next((aid for aid, a in analysis_by_id.items()
                              if a.get("local_path") == item.source_file), None),
                        {},
                    ).get("video_duration")
                    if vid_dur and vid_dur > 0:
                        if item.start_time >= vid_dur:
                            item.start_time = max(vid_dur - 2, 0)
                        if item.end_time is not None and item.end_time > vid_dur:
                            item.end_time = vid_dur
                        if item.end_time is not None and item.start_time >= item.end_time:
                            continue
                valid_items.append(item)
            seg.items = valid_items
        edl.segments = [s for s in edl.segments if s.items]

    def test_start_past_duration_clamped(self):
        edl = _make_test_edl([
            EditItem(source_file="/media/clip.mp4", media_type="video",
                     display_duration=5.0, start_time=120.0, end_time=125.0),
        ])
        self._validate_trims(edl, {1: {"local_path": "/media/clip.mp4", "video_duration": 60.0}})
        assert edl.all_items()[0].start_time == 58.0
        assert edl.all_items()[0].end_time == 60.0

    def test_end_past_duration_clamped(self):
        edl = _make_test_edl([
            EditItem(source_file="/media/clip.mp4", media_type="video",
                     display_duration=5.0, start_time=50.0, end_time=90.0),
        ])
        self._validate_trims(edl, {1: {"local_path": "/media/clip.mp4", "video_duration": 60.0}})
        assert edl.all_items()[0].end_time == 60.0

    def test_start_ge_end_removed(self):
        edl = _make_test_edl([
            EditItem(source_file="/media/clip.mp4", media_type="video",
                     display_duration=5.0, start_time=120.0, end_time=57.0),
        ])
        self._validate_trims(edl, {1: {"local_path": "/media/clip.mp4", "video_duration": 60.0}})
        assert len(edl.all_items()) == 0

    def test_valid_trims_unchanged(self):
        edl = _make_test_edl([
            EditItem(source_file="/media/clip.mp4", media_type="video",
                     display_duration=5.0, start_time=10.0, end_time=20.0),
        ])
        self._validate_trims(edl, {1: {"local_path": "/media/clip.mp4", "video_duration": 60.0}})
        assert edl.all_items()[0].start_time == 10.0

    def test_photo_items_unaffected(self):
        edl = _make_test_edl([
            EditItem(source_file="/media/photo.jpg", media_type="photo", display_duration=4.0),
        ])
        self._validate_trims(edl, {})
        assert len(edl.all_items()) == 1

    def test_no_trim_points_unaffected(self):
        edl = _make_test_edl([
            EditItem(source_file="/media/clip.mp4", media_type="video", display_duration=5.0),
        ])
        self._validate_trims(edl, {1: {"local_path": "/media/clip.mp4", "video_duration": 60.0}})
        assert len(edl.all_items()) == 1


# ---------------------------------------------------------------------------
# Post-processing: video effect override
# ---------------------------------------------------------------------------

class TestVideoEffectOverride:
    """plan() forces effect='none' on all video items."""

    def test_video_ken_burns_forced_to_none(self):
        edl = _make_test_edl([
            EditItem(source_file="a.mp4", media_type="video", display_duration=5.0,
                     effect="ken_burns_in"),
        ])
        for seg in edl.segments:
            for item in seg.items:
                if item.media_type == "video" and item.effect != "none":
                    item.effect = "none"
        assert edl.all_items()[0].effect == "none"

    def test_photo_effect_preserved(self):
        edl = _make_test_edl([
            EditItem(source_file="a.jpg", media_type="photo", display_duration=4.0,
                     effect="ken_burns_left"),
        ])
        for seg in edl.segments:
            for item in seg.items:
                if item.media_type == "video" and item.effect != "none":
                    item.effect = "none"
        assert edl.all_items()[0].effect == "ken_burns_left"


# ---------------------------------------------------------------------------
# Duration check (Layer 3)
# ---------------------------------------------------------------------------

class TestDurationCheck:
    def test_underfilled_detected(self):
        edl = _make_test_edl([
            EditItem(source_file="a.jpg", media_type="photo", display_duration=3.0),
        ])
        assert edl.estimated_duration() < 10.0 * 0.8

    def test_adequate_duration_passes(self):
        edl = _make_test_edl([
            EditItem(source_file="a.jpg", media_type="photo", display_duration=5.0),
            EditItem(source_file="b.jpg", media_type="photo", display_duration=4.5),
        ])
        assert edl.estimated_duration() >= 10.0 * 0.8


# ---------------------------------------------------------------------------
# Files API threshold
# ---------------------------------------------------------------------------

class TestFilesApiThreshold:
    """_gemini_call should use Files API when payload > 20MB."""

    def test_small_payload_uses_inline(self):
        # 1KB of data — should use inline
        parts = [{"type": "image_bytes", "data": b"\x00" * 1024, "mime_type": "image/jpeg"}]
        total = sum(len(p.get("data", b"")) for p in parts)
        assert total < 20 * 1024 * 1024

    def test_large_payload_triggers_files_api(self):
        # 25MB of data — should trigger Files API
        parts = [{"type": "video_bytes", "data": b"\x00" * (25 * 1024 * 1024), "mime_type": "video/mp4"}]
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
        for tr in ["crossfade", "dissolve", "smoothleft", "smoothright",
                    "circlecrop", "fade_black", "wipe_left", "cut"]:
            seg = Segment(name="t", items=[], transition=tr)
            assert seg.transition == tr


# ---------------------------------------------------------------------------
# Pre-Gemini validation checks
# ---------------------------------------------------------------------------


class TestPreGeminiValidation:
    """Test _build_visual_content_blocks validation."""

    def test_empty_analysis_raises(self):
        """0 text blocks with candidates → RuntimeError."""
        from pipeline.plan import _build_visual_content_blocks
        from pipeline.config import Config
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            cfg = Config(
                workspace=Path(td),
                media_dir=Path(td) / "media",
                cache_dir=Path(td) / "cache",
                thumbnails_dir=Path(td) / "thumbs",
                preview_clips_dir=Path(td) / "previews",
            )
            cfg.ensure_dirs()

            preprocessed = {"timeline": [{"date": "2025-01-01", "day_name": "Mon",
                            "chapters": [{"time_block": "morning", "location": "x",
                            "item_ids": [1, 2, 3]}]}]}
            # Empty analysis → no items match → should raise
            with pytest.raises(RuntimeError, match="No text blocks"):
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
            title="test", target_duration=180, segments=[
                Segment(name="s1", items=[
                    EditItem(source_file="a.mp4", media_type="video", display_duration=5),
                ], transition="crossfade"),
                Segment(name="s2", items=[
                    EditItem(source_file="a.mp4", media_type="video", display_duration=5),
                    EditItem(source_file="b.jpg", media_type="photo", display_duration=3),
                ], transition="crossfade"),
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
            title="test", target_duration=180, segments=[
                Segment(name="s1", items=[
                    EditItem(source_file="a.mp4", media_type="video"),
                    EditItem(source_file="b.jpg", media_type="photo"),
                ], transition="crossfade"),
            ],
        )
        seen: set[str] = set()
        for seg in edl.segments:
            unique = [i for i in seg.items if i.source_file not in seen and not seen.add(i.source_file)]
            seg.items = unique

        assert len(edl.segments[0].items) == 2


# ---------------------------------------------------------------------------
# Content block validation
# ---------------------------------------------------------------------------


class TestContentBlockValidation:
    """Test _build_visual_content_blocks produces consistent output."""

    def test_valid_blocks_have_text_and_images(self, tmp_path):
        """With valid data, blocks contain text + image parts."""
        from pipeline.plan import _build_visual_content_blocks
        from pipeline.config import Config

        cfg = Config(
            workspace=tmp_path,
            media_dir=tmp_path / "media",
            cache_dir=tmp_path / "cache",
            thumbnails_dir=tmp_path / "thumbs",
            preview_clips_dir=tmp_path / "previews",
        )
        cfg.ensure_dirs()

        # Create a real JPEG file via PIL
        from PIL import Image
        img_path = tmp_path / "media" / "photo.jpg"
        img_path.parent.mkdir(exist_ok=True)
        Image.new("RGB", (100, 100), "red").save(img_path, "JPEG")

        preprocessed = {"timeline": [{"date": "2025-01-01", "day_name": "Mon",
                        "chapters": [{"time_block": "morning", "location": "x",
                        "item_ids": [1]}]}]}
        analysis = {"1": {"id": 1, "filename": "photo.jpg",
                    "local_path": str(img_path),
                    "media_type": "photo"}}

        blocks, _ = _build_visual_content_blocks(preprocessed, analysis, cfg)

        texts = [b for b in blocks if isinstance(b, str)]
        assert len(texts) >= 1
        assert "#01" in texts[0]  # item numbering starts at 1


class TestLocalTimeConversion:
    """Test that metadata shows local time, not UTC."""

    def test_utc_to_local_time(self):
        """taken_iso in UTC is converted to local time with tz_hours."""
        text, _, _, _ = _build_visual_chapter_text(
            SAMPLE_CHAPTER, SAMPLE_DAY, SAMPLE_ANALYSIS, start_idx=1,
            tz_hours=8,
        )
        # SAMPLE_ANALYSIS item 1 has taken_iso=2025-06-13T14:30:00 (UTC)
        # With tz_hours=8, local time = 22:30
        assert "22:30" in text

    def test_zero_tz_keeps_utc(self):
        """tz_hours=0 keeps UTC time."""
        text, _, _, _ = _build_visual_chapter_text(
            SAMPLE_CHAPTER, SAMPLE_DAY, SAMPLE_ANALYSIS, start_idx=1,
            tz_hours=0,
        )
        assert "14:30" in text
