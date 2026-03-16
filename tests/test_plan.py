"""Tests for pipeline.plan — algorithmic and API-based EDL generation."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline.edl import EDL
from pipeline.plan import _build_chapters_prompt, _plan_auto, plan


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_PREPROCESSED = {
    "family_names": ["Alice", "Bob"],
    "total_items": 4,
    "selected_items": 3,
    "tier_counts": {"A": 1, "B": 1, "C": 1, "D": 0},
    "items": [
        {"id": 1, "tier": "A", "family_count": 2, "filename": "IMG_001.jpg",
         "local_path": "/media/1_IMG_001.jpg"},
        {"id": 2, "tier": "B", "family_count": 1, "filename": "IMG_002.jpg",
         "local_path": "/media/2_IMG_002.jpg"},
        {"id": 3, "tier": "C", "family_count": 0, "filename": "IMG_003.jpg",
         "local_path": "/media/3_IMG_003.jpg"},
    ],
    "timeline": [
        {
            "date": "2024-01-15",
            "day_name": "Monday",
            "chapters": [
                {
                    "time_block": "morning",
                    "location": "Marina Bay",
                    "item_ids": [1, 2],
                    "count": 2,
                    "family_together": 1,
                },
                {
                    "time_block": "afternoon",
                    "location": "Chinatown",
                    "item_ids": [3],
                    "count": 1,
                    "family_together": 0,
                },
            ],
            "total_items": 3,
        }
    ],
}

SAMPLE_ANALYSIS = [
    {
        "id": 1,
        "filename": "IMG_001.jpg",
        "local_path": "/media/1_IMG_001.jpg",
        "media_type": "photo",
        "tier": "A",
        "family_count": 2,
        "vision": {
            "description": "Family at Marina Bay Sands",
            "togetherness": 9,
            "genuine_emotion": 8,
            "story_beat": "landmark",
            "visual_quality": 9,
            "vlog_worthy": True,
        },
    },
    {
        "id": 2,
        "filename": "IMG_002.jpg",
        "local_path": "/media/2_IMG_002.jpg",
        "media_type": "photo",
        "tier": "B",
        "family_count": 1,
        "vision": {
            "description": "Alice smiling at camera",
            "togetherness": 4,
            "genuine_emotion": 7,
            "story_beat": "posed",
            "visual_quality": 7,
            "vlog_worthy": True,
        },
    },
    {
        "id": 3,
        "filename": "IMG_003.jpg",
        "local_path": "/media/3_IMG_003.jpg",
        "media_type": "photo",
        "tier": "C",
        "family_count": 0,
        "vision": {
            "description": "Chinatown street view",
            "scene_type": "street",
            "visual_quality": 6,
            "vlog_worthy": True,
        },
    },
]


def _setup_workspace(cfg) -> None:
    (cfg.workspace / "preprocessed.json").write_text(json.dumps(SAMPLE_PREPROCESSED))
    (cfg.workspace / "analysis.json").write_text(json.dumps(SAMPLE_ANALYSIS))


# ---------------------------------------------------------------------------
# Algorithmic planner tests
# ---------------------------------------------------------------------------

class TestPlanAutoReturnsEdl:
    def test_returns_valid_edl(self):
        """Algorithmic planner should return a valid EDL."""
        analysis_by_id = {a["id"]: a for a in SAMPLE_ANALYSIS}
        edl = _plan_auto(SAMPLE_PREPROCESSED, analysis_by_id,
                         style="upbeat", target_duration=30)
        assert isinstance(edl, EDL)
        assert len(edl.segments) >= 1
        assert len(edl.all_items()) >= 1

    def test_uses_real_paths(self):
        """All source_file values should match analysis local_path."""
        analysis_by_id = {a["id"]: a for a in SAMPLE_ANALYSIS}
        edl = _plan_auto(SAMPLE_PREPROCESSED, analysis_by_id,
                         style="upbeat", target_duration=60)
        valid_paths = {a["local_path"] for a in SAMPLE_ANALYSIS}
        for item in edl.all_items():
            assert item.source_file in valid_paths

    def test_respects_target_duration(self):
        """Estimated duration should not vastly exceed target."""
        analysis_by_id = {a["id"]: a for a in SAMPLE_ANALYSIS}
        edl = _plan_auto(SAMPLE_PREPROCESSED, analysis_by_id,
                         style="upbeat", target_duration=30)
        assert edl.estimated_duration() <= 60  # some overshoot ok

    def test_has_text_overlays(self):
        """At least one item should have a text overlay (date/location)."""
        analysis_by_id = {a["id"]: a for a in SAMPLE_ANALYSIS}
        edl = _plan_auto(SAMPLE_PREPROCESSED, analysis_by_id,
                         style="upbeat", target_duration=60)
        overlays = [it for it in edl.all_items() if it.text_overlay]
        assert len(overlays) >= 1


class TestPlanAutoStyles:
    def test_cinematic_uses_fade_black(self):
        analysis_by_id = {a["id"]: a for a in SAMPLE_ANALYSIS}
        edl = _plan_auto(SAMPLE_PREPROCESSED, analysis_by_id,
                         style="cinematic", target_duration=30)
        assert edl.segments[0].transition == "fade_black"

    def test_upbeat_uses_crossfade(self):
        analysis_by_id = {a["id"]: a for a in SAMPLE_ANALYSIS}
        edl = _plan_auto(SAMPLE_PREPROCESSED, analysis_by_id,
                         style="upbeat", target_duration=30)
        assert edl.segments[0].transition == "crossfade"


class TestPlanIntegration:
    def test_plan_writes_versioned_edl(self, tmp_path: Path, mock_config):
        """plan() should write edl_v{N}.json to workspace."""
        cfg = mock_config
        _setup_workspace(cfg)

        result, version = plan(cfg, target_duration=30)

        assert isinstance(result, EDL)
        assert version >= 1
        assert (cfg.workspace / f"edl_v{version}.json").exists()


# ---------------------------------------------------------------------------
# Prompt builder tests
# ---------------------------------------------------------------------------

class TestBuildChaptersPrompt:
    def test_build_chapters_prompt(self):
        """Prompt structure should include tier A/B items with vision data."""
        analysis_by_id = {a["id"]: a for a in SAMPLE_ANALYSIS}
        result = _build_chapters_prompt(SAMPLE_PREPROCESSED, analysis_by_id)

        assert "Monday" in result
        assert "2024-01-15" in result
        assert "Marina Bay" in result
        assert "Chinatown" in result
        assert "[A]" in result
        assert "fam=2" in result
        assert "tog=9" in result
        assert "[B]" in result
        assert "[C]" in result
        assert "scene=" in result
        assert "/media/1_IMG_001.jpg" in result
