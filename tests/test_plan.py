"""Tests for pipeline.plan — Gemini visual planner helpers."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline.edl import EDL
from pipeline.plan import (
    _build_chapters_prompt,
    _default_focus,
)


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
        "cluster_size": 3,
        "district": "Marina Bay",
        "country": "Singapore",
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
            "scene_type": "landmark",
            "visual_quality": 8,
            "vlog_worthy": True,
        },
    },
]


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
# Prompt builder tests
# ---------------------------------------------------------------------------

class TestBuildChaptersPrompt:
    def test_build_chapters_prompt(self):
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

    def test_includes_quality_score(self):
        analysis_by_id = {a["id"]: a for a in SAMPLE_ANALYSIS}
        result = _build_chapters_prompt(SAMPLE_PREPROCESSED, analysis_by_id)
        assert "qual=9" in result

    def test_includes_cluster_size(self):
        analysis_by_id = {a["id"]: a for a in SAMPLE_ANALYSIS}
        result = _build_chapters_prompt(SAMPLE_PREPROCESSED, analysis_by_id)
        assert "best of 3" in result

    def test_includes_location_detail(self):
        analysis_by_id = {a["id"]: a for a in SAMPLE_ANALYSIS}
        result = _build_chapters_prompt(SAMPLE_PREPROCESSED, analysis_by_id)
        assert "Singapore" in result
