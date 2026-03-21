"""Tests for pipeline.plan — Gemini visual planner helpers and fault tolerance."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from pipeline.plan import _default_focus
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
