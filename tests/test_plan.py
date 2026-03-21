"""Tests for pipeline.plan — Gemini visual planner helpers and fault tolerance."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from pipeline.plan import _default_focus, _format_date_range
from pipeline.edl import EDL, EditItem, Segment


# ---------------------------------------------------------------------------
# Default focus tests
# ---------------------------------------------------------------------------

class TestDefaultFocus:
    def test_family_default(self):
        assert "family" in _default_focus("family").lower() or "happiness" in _default_focus("family").lower()

    def test_solo_default(self):
        assert "journey" in _default_focus("solo").lower() or "discovery" in _default_focus("solo").lower()

    def test_unknown_falls_back(self):
        assert _default_focus("nonexistent") == _default_focus("general")


# ---------------------------------------------------------------------------
# Fault tolerance tests (Issue #2)
# ---------------------------------------------------------------------------

def _make_test_edl(items: list[dict] | None = None) -> EDL:
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


class TestPathValidation:
    """Layer 2: Fix hallucinated file paths."""

    def test_existing_paths_kept(self, tmp_path):
        """Items with existing source files should be kept."""
        photo = tmp_path / "photo1.jpg"
        photo.write_bytes(b"\xff\xd8" + b"\x00" * 100)

        edl = _make_test_edl([
            EditItem(source_file=str(photo), media_type="photo", display_duration=4.0),
        ])

        # Simulate Layer 2 logic inline
        for seg in edl.segments:
            valid = [i for i in seg.items if Path(i.source_file).exists()]
            seg.items = valid

        assert len(edl.all_items()) == 1

    def test_missing_paths_removed(self, tmp_path):
        """Items with nonexistent source files should be removed."""
        edl = _make_test_edl([
            EditItem(source_file="/nonexistent/photo.jpg", media_type="photo", display_duration=4.0),
        ])

        for seg in edl.segments:
            seg.items = [i for i in seg.items if Path(i.source_file).exists()]
        edl.segments = [s for s in edl.segments if s.items]

        assert len(edl.all_items()) == 0
        assert len(edl.segments) == 0

    def test_fuzzy_match_finds_prefixed_file(self, tmp_path):
        """If "87681_IMG_001.jpg" doesn't exist but media dir has "12345_IMG_001.jpg", match it."""
        media_dir = tmp_path / "media"
        media_dir.mkdir()
        real_file = media_dir / "12345_IMG_001.jpg"
        real_file.write_bytes(b"\xff\xd8" + b"\x00" * 100)

        item = EditItem(
            source_file=str(tmp_path / "87681_IMG_001.jpg"),  # wrong prefix
            media_type="photo", display_duration=4.0,
        )

        # Simulate fuzzy matching from Layer 2
        source = Path(item.source_file)
        name = source.name
        parts = name.split("_", 1)
        candidates = list(media_dir.glob(f"*{parts[-1]}")) if len(parts) > 1 else []

        assert len(candidates) == 1
        assert candidates[0] == real_file


class TestTrimValidation:
    """Layer 2b: Validate video trim points against actual duration."""

    def _validate_trims(self, edl, analysis_by_id):
        """Replicate the Layer 2b trim validation logic from plan.py."""
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
        """start_time=120 on a 60s video should be clamped to 58."""
        edl = _make_test_edl([
            EditItem(source_file="/media/clip.mp4", media_type="video",
                     display_duration=5.0, start_time=120.0, end_time=125.0),
        ])
        analysis_by_id = {1: {"local_path": "/media/clip.mp4", "video_duration": 60.0}}
        self._validate_trims(edl, analysis_by_id)
        items = edl.all_items()
        assert len(items) == 1
        assert items[0].start_time == 58.0
        assert items[0].end_time == 60.0

    def test_end_past_duration_clamped(self):
        """end_time=90 on a 60s video should be clamped to 60."""
        edl = _make_test_edl([
            EditItem(source_file="/media/clip.mp4", media_type="video",
                     display_duration=5.0, start_time=50.0, end_time=90.0),
        ])
        analysis_by_id = {1: {"local_path": "/media/clip.mp4", "video_duration": 60.0}}
        self._validate_trims(edl, analysis_by_id)
        items = edl.all_items()
        assert len(items) == 1
        assert items[0].start_time == 50.0
        assert items[0].end_time == 60.0

    def test_start_ge_end_after_clamp_removed(self):
        """If clamping makes start >= end, the item should be removed."""
        # vid_dur=60, start=120 -> clamp to 58, end=57 (within duration, not clamped)
        # 58 >= 57 -> item removed
        edl = _make_test_edl([
            EditItem(source_file="/media/clip.mp4", media_type="video",
                     display_duration=5.0, start_time=120.0, end_time=57.0),
        ])
        analysis_by_id = {1: {"local_path": "/media/clip.mp4", "video_duration": 60.0}}
        self._validate_trims(edl, analysis_by_id)
        assert len(edl.all_items()) == 0

    def test_valid_trims_unchanged(self):
        """Trim points within duration should not be modified."""
        edl = _make_test_edl([
            EditItem(source_file="/media/clip.mp4", media_type="video",
                     display_duration=5.0, start_time=10.0, end_time=20.0),
        ])
        analysis_by_id = {1: {"local_path": "/media/clip.mp4", "video_duration": 60.0}}
        self._validate_trims(edl, analysis_by_id)
        items = edl.all_items()
        assert len(items) == 1
        assert items[0].start_time == 10.0
        assert items[0].end_time == 20.0

    def test_photo_items_unaffected(self):
        """Photo items should not be affected by trim validation."""
        edl = _make_test_edl([
            EditItem(source_file="/media/photo.jpg", media_type="photo", display_duration=4.0),
        ])
        analysis_by_id = {}
        self._validate_trims(edl, analysis_by_id)
        assert len(edl.all_items()) == 1

    def test_no_trim_points_unaffected(self):
        """Video items without start_time should not be affected."""
        edl = _make_test_edl([
            EditItem(source_file="/media/clip.mp4", media_type="video", display_duration=5.0),
        ])
        analysis_by_id = {1: {"local_path": "/media/clip.mp4", "video_duration": 60.0}}
        self._validate_trims(edl, analysis_by_id)
        assert len(edl.all_items()) == 1


class TestDurationCheck:
    """Layer 3: Duration check."""

    def test_underfilled_warning(self):
        """EDL <80% of target should be flagged."""
        edl = _make_test_edl([
            EditItem(source_file="a.jpg", media_type="photo", display_duration=3.0),
        ])
        # target_duration=10.0, actual ~3.0 → 30% → severely underfilled
        actual = edl.estimated_duration()
        assert actual < 10.0 * 0.8

    def test_adequate_duration_passes(self):
        """EDL >=80% of target should pass."""
        edl = _make_test_edl([
            EditItem(source_file="a.jpg", media_type="photo", display_duration=5.0),
            EditItem(source_file="b.jpg", media_type="photo", display_duration=4.5),
        ])
        actual = edl.estimated_duration()
        assert actual >= 10.0 * 0.8


# ---------------------------------------------------------------------------
# _format_date_range tests (cross-platform, no %-d)
# ---------------------------------------------------------------------------


class TestFormatDateRange:
    """Verify _format_date_range works on all platforms (no %-d strftime)."""

    def test_same_month(self):
        """Dates within one month: 'June 13-16, 2025'."""
        result = _format_date_range(["2025-06-13", "2025-06-14", "2025-06-16"])
        assert result == "June 13-16, 2025"

    def test_multi_month(self):
        """Dates spanning months: 'June 28 - July 3, 2025'."""
        result = _format_date_range(["2025-06-28", "2025-06-30", "2025-07-01", "2025-07-03"])
        assert result == "June 28 - July 3, 2025"

    def test_single_date(self):
        """Single date: 'March 5-5, 2024'."""
        result = _format_date_range(["2024-03-05"])
        assert "March" in result
        assert "2024" in result

    def test_empty_list(self):
        result = _format_date_range([])
        assert result == ""

    def test_no_zero_padding(self):
        """Day numbers should not have leading zeros (e.g. '3' not '03')."""
        result = _format_date_range(["2025-01-03", "2025-02-05"])
        # Should be "January 3 - February 5, 2025", not "January 03 - February 05"
        assert "January 3" in result
        assert "February 5" in result
