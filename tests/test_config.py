"""Tests for pipeline.config — Config loading and directory management."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from pipeline.config import Config


class TestConfigLoadDefaults:
    @patch.dict(os.environ, {}, clear=True)
    def test_default_values(self):
        """Config.load() without arguments uses sensible defaults."""
        with patch("pipeline.config.load_dotenv"):
            cfg = Config.load()
        assert cfg.workspace == Path("./workspace")

    @patch.dict(os.environ, {}, clear=True)
    def test_workspace_argument_sets_workspace(self, tmp_path: Path):
        ws = str(tmp_path / "my_workspace")
        with patch("pipeline.config.load_dotenv"):
            cfg = Config.load(workspace=ws)
        assert cfg.workspace == Path(ws)

    @patch.dict(os.environ, {}, clear=True)
    def test_runs_parent_detected(self, tmp_path: Path):
        """workspace/runs/myrun -> shared dirs at workspace/ level."""
        ws = str(tmp_path / "workspace" / "runs" / "myrun")
        with patch("pipeline.config.load_dotenv"):
            cfg = Config.load(workspace=ws)
        expected_base = tmp_path / "workspace"
        assert cfg.media_dir == expected_base / "media"
        assert cfg.cache_dir == expected_base / "analysis_cache"
        assert cfg.thumbnails_dir == expected_base / "thumbnails"

    @patch.dict(os.environ, {}, clear=True)
    def test_custom_workspace_uses_self_for_shared(self, tmp_path: Path):
        ws = str(tmp_path / "workspace" / "custom")
        with patch("pipeline.config.load_dotenv"):
            cfg = Config.load(workspace=ws)
        assert cfg.media_dir == Path(ws) / "media"


class TestConfigLoadEnvVars:
    def test_env_var_overrides(self, tmp_path: Path):
        env = {
            "WORKSPACE": str(tmp_path),
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch("pipeline.config.load_dotenv"),
        ):
            cfg = Config.load()
        assert cfg.workspace == tmp_path

    def test_workspace_arg_overrides_env(self, tmp_path: Path):
        env = {"WORKSPACE": "/should/not/use"}
        ws = str(tmp_path / "explicit")
        with (
            patch.dict(os.environ, env, clear=True),
            patch("pipeline.config.load_dotenv"),
        ):
            cfg = Config.load(workspace=ws)
        assert cfg.workspace == Path(ws)


class TestEnsureDirs:
    @patch.dict(os.environ, {}, clear=True)
    def test_creates_all_directories(self, tmp_path: Path):
        ws = str(tmp_path / "workspace")
        with patch("pipeline.config.load_dotenv"):
            cfg = Config.load(workspace=ws)
        cfg.ensure_dirs()
        assert (Path(ws) / "clips").is_dir()
        assert (Path(ws) / "output").is_dir()
        assert cfg.cache_dir.is_dir()
        assert cfg.thumbnails_dir.is_dir()
        assert cfg.preview_clips_dir.is_dir()
        assert cfg.music_dir.is_dir()

    @patch.dict(os.environ, {}, clear=True)
    def test_idempotent(self, tmp_path: Path):
        ws = str(tmp_path / "workspace")
        with patch("pipeline.config.load_dotenv"):
            cfg = Config.load(workspace=ws)
        cfg.ensure_dirs()
        cfg.ensure_dirs()
        assert (Path(ws) / "clips").is_dir()
