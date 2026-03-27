"""Tests for pipeline.config — Config loading and directory management."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline.config import Config


@pytest.fixture
def _clean_env():
    """Clear environment and mock dotenv for Config.load() tests."""
    with patch.dict(os.environ, {}, clear=True), patch("pipeline.config.load_dotenv"):
        yield


@pytest.mark.usefixtures("_clean_env")
class TestConfigLoad:
    def test_default_workspace(self):
        assert Config.load().workspace == Path("./workspace")

    def test_workspace_argument(self, tmp_path: Path):
        ws = str(tmp_path / "my_workspace")
        assert Config.load(workspace=ws).workspace == Path(ws)

    def test_runs_parent_detected(self, tmp_path: Path):
        """workspace/runs/myrun -> shared dirs at workspace/ level."""
        ws = str(tmp_path / "workspace" / "runs" / "myrun")
        cfg = Config.load(workspace=ws)
        expected_base = tmp_path / "workspace"
        assert cfg.media_dir == expected_base / "media"
        assert cfg.cache_dir == expected_base / "analysis_cache"
        assert cfg.thumbnails_dir == expected_base / "thumbnails"

    def test_workspace_arg_used(self, tmp_path: Path):
        ws = str(tmp_path / "explicit")
        assert Config.load(workspace=ws).workspace == Path(ws)

    def test_no_workspace_falls_to_default(self):
        assert Config.load().workspace == Path("./workspace")


@pytest.mark.usefixtures("_clean_env")
class TestEnsureDirs:
    def test_creates_all_directories(self, tmp_path: Path):
        cfg = Config.load(workspace=str(tmp_path / "workspace" / "runs" / "test"))
        cfg.ensure_dirs()
        assert (cfg.workspace / "clips").is_dir()
        assert (cfg.workspace / "output").is_dir()
        assert cfg.cache_dir.is_dir()
        assert cfg.thumbnails_dir.is_dir()
        assert cfg.preview_clips_dir.is_dir()
        assert cfg.music_dir.is_dir()

    def test_idempotent(self, tmp_path: Path):
        cfg = Config.load(workspace=str(tmp_path / "workspace" / "runs" / "test"))
        cfg.ensure_dirs()
        cfg.ensure_dirs()
        assert (cfg.workspace / "clips").is_dir()
