"""Tests for pipeline.plan — EDL generation via LLM."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline.edl import EDL
from pipeline.plan import SYSTEM_PROMPT, _build_chapters_prompt, plan


def _valid_edl_json() -> str:
    """Return a valid EDL JSON string that matches the EDL schema."""
    edl = {
        "title": "Test Trip",
        "target_duration": 120,
        "resolution": [3840, 2160],
        "fps": 60,
        "segments": [
            {
                "name": "Opening",
                "items": [
                    {
                        "source_file": "/media/1_IMG_001.jpg",
                        "media_type": "photo",
                        "start_time": None,
                        "end_time": None,
                        "display_duration": 4.0,
                        "effect": "ken_burns_in",
                        "text_overlay": None,
                    },
                    {
                        "source_file": "/media/2_IMG_002.jpg",
                        "media_type": "photo",
                        "start_time": None,
                        "end_time": None,
                        "display_duration": 3.0,
                        "effect": "ken_burns_out",
                        "text_overlay": None,
                    },
                ],
                "transition": "crossfade",
                "transition_duration": 0.8,
            }
        ],
        "music": None,
    }
    return json.dumps(edl)


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


def _setup_workspace(cfg: Config) -> None:
    """Write preprocessed.json and analysis.json to workspace."""
    (cfg.workspace / "preprocessed.json").write_text(json.dumps(SAMPLE_PREPROCESSED))
    (cfg.workspace / "analysis.json").write_text(json.dumps(SAMPLE_ANALYSIS))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPlanCallsOllama:
    def test_plan_calls_ollama(self, tmp_path: Path, mock_config):
        """ollama_chat should be called with a system prompt."""
        cfg = mock_config
        _setup_workspace(cfg)

        with patch("pipeline.plan.ollama_chat", return_value=_valid_edl_json()) as mock_chat:
            plan(cfg)

        assert mock_chat.call_count >= 1
        _, kwargs = mock_chat.call_args
        assert kwargs["system"] == SYSTEM_PROMPT


class TestPlanIncludesDuration:
    def test_plan_includes_duration(self, tmp_path: Path, mock_config):
        """target_duration should appear in the user prompt sent to ollama_chat."""
        cfg = mock_config
        _setup_workspace(cfg)

        with patch("pipeline.plan.ollama_chat", return_value=_valid_edl_json()) as mock_chat:
            plan(cfg, target_duration=240)

        _, kwargs = mock_chat.call_args
        assert "240" in kwargs["prompt"]


class TestPlanReturnsEdl:
    def test_plan_returns_edl(self, tmp_path: Path, mock_config):
        """Return value should be an EDL instance."""
        cfg = mock_config
        _setup_workspace(cfg)

        with patch("pipeline.plan.ollama_chat", return_value=_valid_edl_json()):
            result, version = plan(cfg)

        assert isinstance(result, EDL)
        assert result.title == "Test Trip"
        assert len(result.segments) == 1


class TestPlanWritesEdlJson:
    def test_plan_writes_edl_json(self, tmp_path: Path, mock_config):
        """edl.json should be written to workspace."""
        cfg = mock_config
        _setup_workspace(cfg)

        with patch("pipeline.plan.ollama_chat", return_value=_valid_edl_json()):
            plan(cfg)

        edl_path = cfg.workspace / "edl.json"
        assert edl_path.exists()
        data = json.loads(edl_path.read_text())
        assert data["title"] == "Test Trip"


class TestPlanStripsFences:
    def test_plan_strips_fences(self, tmp_path: Path, mock_config):
        """LLM returns ```json...```, plan should still parse correctly."""
        cfg = mock_config
        _setup_workspace(cfg)

        fenced = f"```json\n{_valid_edl_json()}\n```"
        with patch("pipeline.plan.ollama_chat", return_value=fenced):
            result, version = plan(cfg)

        assert isinstance(result, EDL)
        assert result.title == "Test Trip"


class TestBuildChaptersPrompt:
    def test_build_chapters_prompt(self):
        """Prompt structure should include tier A/B items with vision data."""
        analysis_by_id = {a["id"]: a for a in SAMPLE_ANALYSIS}
        result = _build_chapters_prompt(SAMPLE_PREPROCESSED, analysis_by_id)

        # Should contain the day header
        assert "Monday" in result
        assert "2024-01-15" in result

        # Should contain location blocks
        assert "Marina Bay" in result
        assert "Chinatown" in result

        # Should include tier A item with family details
        assert "[A]" in result
        assert "fam=2" in result
        assert "tog=9" in result

        # Should include tier B item
        assert "[B]" in result

        # Should include tier C item with scene type
        assert "[C]" in result
        assert "scene=" in result

        # Should include local paths
        assert "/media/1_IMG_001.jpg" in result
