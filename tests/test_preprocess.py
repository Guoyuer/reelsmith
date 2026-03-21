"""Tests for pipeline.prepare — family detection, timeline, integration."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline.config import Config
from pipeline.prepare import _build_timeline, _detect_family, prepare as preprocess


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
# Family count tests
# -----------------------------------------------------------------------


class TestFamilyCount:
    """Test family_count assignment in prepare()."""

    def _run_preprocess(self, items: list[dict], tmp_path: Path) -> dict:
        """Write manifest and run preprocess, returning the result."""
        ws = tmp_path / "workspace"
        manifest_path = ws / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(items))

        with patch.dict(os.environ, {}, clear=True), \
             patch("pipeline.config.load_dotenv"):
            cfg = Config.load(workspace=str(ws))
        preprocess(cfg, family_names=["Alice", "Bob"])
        # Read the analysis.json to check items
        analysis = json.loads((ws / "analysis.json").read_text())
        return analysis

    def test_two_family_members(self, tmp_path: Path):
        """Item with 2 family persons gets family_count=2."""
        items = [_make_item(1, persons=["Alice", "Bob"])]
        analysis = self._run_preprocess(items, tmp_path)
        assert analysis[0]["family_count"] == 2

    def test_one_family_member(self, tmp_path: Path):
        """Item with 1 family person gets family_count=1."""
        items = [_make_item(1, persons=["Alice"])]
        analysis = self._run_preprocess(items, tmp_path)
        assert analysis[0]["family_count"] == 1

    def test_no_family(self, tmp_path: Path):
        """Item with no family persons gets family_count=0."""
        items = [_make_item(1, persons=["Stranger"])]
        analysis = self._run_preprocess(items, tmp_path)
        assert analysis[0]["family_count"] == 0

    def test_no_persons(self, tmp_path: Path):
        """Item with empty persons gets family_count=0."""
        items = [_make_item(1, persons=[])]
        analysis = self._run_preprocess(items, tmp_path)
        assert analysis[0]["family_count"] == 0


# -----------------------------------------------------------------------
# Clustering tests
# -----------------------------------------------------------------------


# -----------------------------------------------------------------------
# _detect_family tests
# -----------------------------------------------------------------------


class TestDetectFamily:
    def test_returns_top_persons(self):
        """Most frequently appearing persons should be detected as family."""
        items = [
            _make_item(i, persons=["Alice", "Bob"]) for i in range(20)
        ] + [_make_item(99, persons=["Stranger"])]
        result = _detect_family(items)
        assert "Alice" in result
        assert "Bob" in result
        assert "Stranger" not in result

    def test_empty_manifest(self):
        assert _detect_family([]) == []

    def test_respects_top_n(self):
        items = [_make_item(i, persons=[f"P{i % 6}"]) for i in range(60)]
        result = _detect_family(items, top_n=3)
        assert len(result) <= 3


# -----------------------------------------------------------------------
# _build_timeline tests
# -----------------------------------------------------------------------


class TestBuildTimeline:
    def test_groups_by_day(self):
        """Items on different days produce separate timeline entries."""
        base = 1700000000
        items = [
            {"id": 1, "takentime": base, "family_count": 2, "district": "Marina Bay"},
            {"id": 2, "takentime": base + 86400, "family_count": 1, "district": "Orchard"},
        ]
        timeline = _build_timeline(items)
        assert len(timeline) == 2
        dates = [d["date"] for d in timeline]
        assert dates == sorted(dates)

    def test_groups_by_time_block_and_location(self):
        """Items in the same day but different locations get separate chapters."""
        base = 1700000000
        items = [
            {"id": 1, "takentime": base, "family_count": 2, "district": "Marina Bay"},
            {"id": 2, "takentime": base + 60, "family_count": 1, "district": "Chinatown"},
        ]
        timeline = _build_timeline(items)
        assert len(timeline) == 1
        assert len(timeline[0]["chapters"]) == 2

    def test_items_without_takentime_skipped(self):
        """Items missing takentime are excluded from the timeline."""
        items = [
            {"id": 1, "takentime": None, "family_count": 2},
            {"id": 2, "family_count": 1},  # no takentime key
        ]
        timeline = _build_timeline(items)
        assert len(timeline) == 0

    def test_chapter_has_item_ids(self):
        """Chapters should contain item_ids list."""
        base = 1700000000
        items = [
            {"id": 1, "takentime": base, "family_count": 2, "district": "Marina Bay"},
            {"id": 2, "takentime": base + 60, "family_count": 1, "district": "Marina Bay"},
        ]
        timeline = _build_timeline(items)
        chapter = timeline[0]["chapters"][0]
        assert "item_ids" in chapter
        assert set(chapter["item_ids"]) == {1, 2}


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

        out_path = ws / "preprocessed.json"
        assert out_path.exists()

        saved = json.loads(out_path.read_text())
        assert saved["family_names"] == ["Alice", "Bob"]
        assert "timeline" in saved
        assert len(saved["timeline"]) >= 1
