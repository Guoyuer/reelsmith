"""Tests for pipeline.iterate — self-critique, feedback, and variations."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pipeline.edl import EDL, EditItem, Segment
from pipeline.iterate import (
    _find_latest_version,
    _save_edl,
    apply_feedback,
    generate_variations,
    self_critique,
)


def _make_edl() -> EDL:
    """Create a minimal valid EDL."""
    return EDL(
        title="Test Trip",
        target_duration=120.0,
        segments=[
            Segment(
                name="Opening",
                items=[
                    EditItem(
                        source_file="/media/1_IMG_001.jpg",
                        media_type="photo",
                        display_duration=4.0,
                    ),
                    EditItem(
                        source_file="/media/2_IMG_002.jpg",
                        media_type="photo",
                        display_duration=3.0,
                    ),
                ],
                transition="crossfade",
                transition_duration=0.8,
            ),
        ],
    )


def _valid_edl_json_str() -> str:
    """Return a valid EDL JSON string for LLM mock responses."""
    edl = {
        "title": "Revised Trip",
        "target_duration": 120,
        "resolution": [3840, 2160],
        "fps": 60,
        "segments": [
            {
                "name": "Revised Opening",
                "items": [
                    {
                        "source_file": "/media/1_IMG_001.jpg",
                        "media_type": "photo",
                        "start_time": None,
                        "end_time": None,
                        "display_duration": 5.0,
                        "effect": "ken_burns_in",
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


def _setup_workspace(cfg: Config, edl: EDL | None = None, version: int = 1) -> None:
    """Write edl_v{version}.json and create output/vlog_v{version}.mp4."""
    edl = edl or _make_edl()
    edl_path = cfg.workspace / f"edl_v{version}.json"
    edl_path.write_text(edl.model_dump_json(indent=2))

    output_dir = cfg.workspace / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    video = output_dir / f"vlog_v{version}.mp4"
    video.write_bytes(b"\x00" * 100)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFindLatestVersionEmpty:
    def test_find_latest_version_empty(self, tmp_path: Path, mock_config):
        """With no output directory, should return 0."""
        cfg = mock_config
        assert _find_latest_version(cfg) == 0


class TestFindLatestVersionMax:
    def test_find_latest_version_max(self, tmp_path: Path, mock_config):
        """Should return the highest version number found."""
        cfg = mock_config

        (cfg.workspace / "edl_v1.json").write_text("{}")
        (cfg.workspace / "edl_v3.json").write_text("{}")
        (cfg.workspace / "edl_v2.json").write_text("{}")

        assert _find_latest_version(cfg) == 3


class TestSaveEdl:
    def test_save_edl(self, tmp_path: Path, mock_config):
        """Should create edl_v{N}.json in workspace."""
        cfg = mock_config
        edl = _make_edl()

        _save_edl(cfg, edl, 5)

        path = cfg.workspace / "edl_v5.json"
        assert path.exists()
        saved = EDL.model_validate_json(path.read_text())
        assert saved.title == edl.title


class TestSelfCritiqueIncrementsVersion:
    def test_self_critique_increments_version(self, tmp_path: Path, mock_config):
        """Version should go from 1 to 2 after one round of self-critique."""
        cfg = mock_config
        _setup_workspace(cfg, version=1)

        with patch("pipeline.iterate.ollama_chat", return_value=_valid_edl_json_str()), \
             patch("pipeline.iterate.extract_frames", return_value=[]), \
             patch("pipeline.iterate.assemble", return_value=Path("/fake/v2.mp4")):
            result = self_critique(cfg, max_rounds=1)

        # Version 2 EDL should exist
        v2_path = cfg.workspace / "edl_v2.json"
        assert v2_path.exists()

        # The returned EDL should be the revised one
        assert result.title == "Revised Trip"


class TestSelfCritiqueStopsOnParseFailure:
    def test_self_critique_stops_on_parse_failure(self, tmp_path: Path, mock_config):
        """If LLM returns garbage, the loop should break early."""
        cfg = mock_config
        _setup_workspace(cfg, version=1)

        with patch("pipeline.iterate.ollama_chat", return_value="not valid json at all"), \
             patch("pipeline.iterate.extract_frames", return_value=[]), \
             patch("pipeline.iterate.assemble", return_value=Path("/fake/v2.mp4")) as mock_assemble:
            result = self_critique(cfg, max_rounds=3)

        # assemble should NOT have been called for a new version because parsing failed
        # The initial assemble for v1 may be called, but no additional versions
        # Since vlog_v1.mp4 already exists, assemble should not be called at all
        mock_assemble.assert_not_called()

        # Result should be the original EDL (not modified)
        assert result.title == "Test Trip"


class TestApplyFeedbackClearsClips:
    def test_apply_feedback_clears_clips(self, tmp_path: Path, mock_config):
        """clips/ should be emptied after applying feedback."""
        cfg = mock_config
        _setup_workspace(cfg, version=1)

        # Create some clip files
        clips_dir = cfg.workspace / "clips"
        clips_dir.mkdir(parents=True, exist_ok=True)
        (clips_dir / "clip_001.mp4").write_bytes(b"\x00" * 50)
        (clips_dir / "clip_002.mp4").write_bytes(b"\x00" * 50)

        with patch("pipeline.iterate.ollama_chat", return_value=_valid_edl_json_str()), \
             patch("pipeline.iterate.assemble", return_value=Path("/fake/v2.mp4")):
            apply_feedback(cfg, "Make it more upbeat")

        # clips directory should be empty
        remaining = list(clips_dir.iterdir())
        assert len(remaining) == 0


class TestApplyFeedbackSavesEdl:
    def test_apply_feedback_saves_edl(self, tmp_path: Path, mock_config):
        """New EDL should be written as edl_v{N}.json."""
        cfg = mock_config
        _setup_workspace(cfg, version=1)

        with patch("pipeline.iterate.ollama_chat", return_value=_valid_edl_json_str()), \
             patch("pipeline.iterate.assemble", return_value=Path("/fake/v2.mp4")):
            result = apply_feedback(cfg, "Add more family shots")

        # edl_v2.json should exist
        v2_path = cfg.workspace / "edl_v2.json"
        assert v2_path.exists()
        saved = EDL.model_validate_json(v2_path.read_text())
        assert saved.title == "Revised Trip"


class TestGenerateVariationsRestoresOriginal:
    def test_generate_variations_creates_versions(self, tmp_path: Path, mock_config):
        """Variations should create new versioned EDLs."""
        cfg = mock_config
        original_edl = _make_edl()
        _setup_workspace(cfg, edl=original_edl, version=1)

        # Write analysis.json (needed by generate_variations)
        analysis = [
            {
                "id": 1,
                "local_path": "/media/1_IMG_001.jpg",
                "media_type": "photo",
                "duration_ms": None,
                "vision": {"description": "Family photo", "happiness_score": 9},
            },
        ]
        (cfg.workspace / "analysis.json").write_text(json.dumps(analysis))

        variation_edl_json = _valid_edl_json_str()

        with patch("pipeline.iterate.ollama_chat", return_value=variation_edl_json), \
             patch("pipeline.iterate.assemble", return_value=Path("/fake/var.mp4")):
            outputs = generate_variations(cfg, styles=["energetic"])

        # Original v1 should still exist, variation should be v2
        assert (cfg.workspace / "edl_v1.json").exists()
        assert (cfg.workspace / "edl_v2.json").exists()
