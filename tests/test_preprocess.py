"""Tests for pipeline.preprocess — tier assignment, clustering, timeline."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline.config import Config
from pipeline.preprocess import _build_timeline, _detect_family, preprocess


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------


def _make_item(
    item_id: int,
    filename: str = "IMG_{:04d}.jpg",
    takentime: int = 1700000000,
    persons: list[str] | None = None,
    district: str | None = None,
    country: str | None = None,
    first_level: str | None = None,
    filesize: int = 5000000,
) -> dict:
    """Create a manifest item with sensible defaults."""
    return {
        "id": item_id,
        "filename": filename.format(item_id) if "{" in filename else filename,
        "item_type": 0,
        "takentime": takentime,
        "filesize": filesize,
        "district": district,
        "country": country,
        "first_level": first_level,
        "metadata": {"persons": persons or []},
    }


# -----------------------------------------------------------------------
# Tier assignment tests
# -----------------------------------------------------------------------


class TestTierAssignment:
    """Test _assign_tier logic (exercised via preprocess())."""

    def _run_preprocess(self, items: list[dict], tmp_path: Path) -> dict:
        """Write manifest and run preprocess, returning the result."""
        ws = tmp_path / "workspace"
        manifest_path = ws / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(items))

        with patch.dict(os.environ, {}, clear=True), \
             patch("pipeline.config.load_dotenv"):
            cfg = Config.load(workspace=str(ws))
        return preprocess(cfg, family_names=["Alice", "Bob"])

    def test_tier_a_two_family(self, tmp_path: Path):
        """Item with 2+ family persons gets tier A."""
        items = [_make_item(1, persons=["Alice", "Bob"])]
        result = self._run_preprocess(items, tmp_path)
        assert result["items"][0]["tier"] == "A"

    def test_tier_b_one_family(self, tmp_path: Path):
        """Item with exactly 1 family person gets tier B."""
        items = [_make_item(1, persons=["Alice"])]
        result = self._run_preprocess(items, tmp_path)
        assert result["items"][0]["tier"] == "B"

    def test_tier_c_no_family_with_location(self, tmp_path: Path):
        """Item with 0 family but has district gets tier C."""
        items = [_make_item(1, persons=[], district="Marina Bay")]
        result = self._run_preprocess(items, tmp_path)
        assert result["items"][0]["tier"] == "C"

    def test_tier_c_country_only(self, tmp_path: Path):
        """Item with 0 family but has country gets tier C."""
        items = [_make_item(1, persons=[], country="Singapore")]
        result = self._run_preprocess(items, tmp_path)
        assert result["items"][0]["tier"] == "C"

    def test_tier_d_screenshot(self, tmp_path: Path):
        """Screenshot filename gets tier D regardless of persons."""
        items = [_make_item(1, filename="Screenshot_20231114.png",
                            persons=["Alice", "Bob"])]
        result = self._run_preprocess(items, tmp_path)
        assert result["items"][0]["tier"] == "D"

    def test_tier_d_no_family_no_location(self, tmp_path: Path):
        """Item with no family and no location gets tier D."""
        items = [_make_item(1, persons=[])]
        result = self._run_preprocess(items, tmp_path)
        assert result["items"][0]["tier"] == "D"

    def test_screen_prefix_is_skip(self, tmp_path: Path):
        """Filename starting with 'screen_' triggers tier D."""
        items = [_make_item(1, filename="screen_recording.mp4")]
        result = self._run_preprocess(items, tmp_path)
        assert result["items"][0]["tier"] == "D"


# -----------------------------------------------------------------------
# Clustering tests
# -----------------------------------------------------------------------


# -----------------------------------------------------------------------
# _detect_family tests
# -----------------------------------------------------------------------


class TestDetectFamily:
    def test_returns_top_persons(self):
        """Most frequent persons above threshold are returned."""
        manifest = [
            _make_item(i, persons=["Alice", "Bob"])
            for i in range(100)
        ] + [
            _make_item(200, persons=["Stranger"]),
        ]
        result = _detect_family(manifest)
        assert "Alice" in result
        assert "Bob" in result
        # Stranger only appears once, well below 3% of 101
        assert "Stranger" not in result

    def test_empty_manifest(self):
        """Empty manifest returns empty list."""
        assert _detect_family([]) == []

    def test_respects_top_n(self):
        """top_n limits the number of returned names."""
        manifest = [
            _make_item(i, persons=["A", "B", "C", "D", "E", "F"])
            for i in range(200)
        ]
        result = _detect_family(manifest, top_n=3)
        assert len(result) <= 3


# -----------------------------------------------------------------------
# _build_timeline tests
# -----------------------------------------------------------------------


class TestBuildTimeline:
    def test_groups_by_day(self):
        """Items on different days produce separate timeline entries."""
        base = 1700000000  # 2023-11-14
        items = [
            {
                "id": 1, "takentime": base, "tier": "A",
                "family_count": 2, "district": "Marina Bay",
            },
            {
                "id": 2, "takentime": base + 86400, "tier": "B",
                "family_count": 1, "district": "Orchard",
            },
        ]
        timeline = _build_timeline(items)
        assert len(timeline) == 2
        dates = [d["date"] for d in timeline]
        assert dates == sorted(dates)

    def test_groups_by_time_block_and_location(self):
        """Items in the same day but different locations get separate chapters."""
        # 2023-11-14 morning SGT: ~06:00 SGT = 1699916400 UTC
        # takentime in UTC; SGT = UTC+8
        # We need hour >= 12 and < 17 in SGT for afternoon
        base = 1700000000  # This is ~22:13 SGT Nov 14 -> evening block
        items = [
            {
                "id": 1, "takentime": base, "tier": "A",
                "family_count": 2, "district": "Marina Bay",
            },
            {
                "id": 2, "takentime": base + 60, "tier": "B",
                "family_count": 1, "district": "Chinatown",
            },
        ]
        timeline = _build_timeline(items)
        assert len(timeline) == 1
        # Two different locations = two chapters
        assert len(timeline[0]["chapters"]) == 2

    def test_items_without_takentime_skipped(self):
        """Items missing takentime are excluded from the timeline."""
        items = [
            {"id": 1, "takentime": None, "tier": "A", "family_count": 2},
            {"id": 2, "tier": "B", "family_count": 1},  # no takentime key
        ]
        timeline = _build_timeline(items)
        assert len(timeline) == 0

    def test_chapter_family_together_count(self):
        """Chapter family_together count reflects tier A items."""
        base = 1700000000
        items = [
            {
                "id": 1, "takentime": base, "tier": "A",
                "family_count": 2, "district": "Marina Bay",
            },
            {
                "id": 2, "takentime": base + 60, "tier": "B",
                "family_count": 1, "district": "Marina Bay",
            },
            {
                "id": 3, "takentime": base + 120, "tier": "A",
                "family_count": 3, "district": "Marina Bay",
            },
        ]
        timeline = _build_timeline(items)
        chapter = timeline[0]["chapters"][0]
        assert chapter["family_together"] == 2  # items 1 and 3 are tier A


# -----------------------------------------------------------------------
# Full preprocess() integration test
# -----------------------------------------------------------------------


class TestPreprocessIntegration:
    def test_writes_preprocessed_json(self, tmp_path: Path, sample_manifest: list[dict]):
        """preprocess() writes preprocessed.json with expected structure."""
        ws = tmp_path / "workspace"
        ws.mkdir(parents=True)
        (ws / "manifest.json").write_text(json.dumps(sample_manifest))

        with patch.dict(os.environ, {}, clear=True), \
             patch("pipeline.config.load_dotenv"):
            cfg = Config.load(workspace=str(ws))

        result = preprocess(cfg, family_names=["Alice", "Bob"])

        # Output file written
        out_path = ws / "preprocessed.json"
        assert out_path.exists()

        # Round-trip through the file
        saved = json.loads(out_path.read_text())
        assert saved["family_names"] == ["Alice", "Bob"]
        assert saved["total_items"] == len(sample_manifest)
        assert saved["selected_items"] <= saved["total_items"]
        assert "timeline" in saved
        assert len(saved["timeline"]) >= 1  # at least one day
        assert all(item["tier"] in ("A", "B", "C", "D") for item in saved["items"])

        # Verify function return matches the saved file
        assert result["total_items"] == saved["total_items"]
        assert result["selected_items"] == saved["selected_items"]
