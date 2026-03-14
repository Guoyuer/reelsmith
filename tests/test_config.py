"""Tests for pipeline.config — Config loading and directory management."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline.config import Config


# -----------------------------------------------------------------------
# Config.load() defaults and workspace logic
# -----------------------------------------------------------------------


class TestConfigLoadDefaults:
    @patch.dict(os.environ, {}, clear=True)
    def test_default_values(self):
        """Config.load() without arguments uses sensible defaults."""
        with patch("pipeline.config.load_dotenv"):
            cfg = Config.load()
        assert cfg.api_base == "http://localhost:8000"
        assert cfg.ollama_base == "http://localhost:11434"
        assert cfg.vision_model == "llava:7b"
        assert cfg.planning_model == "qwen2.5-coder:7b"
        assert cfg.whisper_model == "medium"
        assert cfg.workspace == Path("./workspace")

    @patch.dict(os.environ, {}, clear=True)
    def test_workspace_argument_sets_workspace(self, tmp_path: Path):
        """Config.load(workspace=...) sets the workspace path."""
        ws = str(tmp_path / "my_workspace")
        with patch("pipeline.config.load_dotenv"):
            cfg = Config.load(workspace=ws)
        assert cfg.workspace == Path(ws)

    @patch.dict(os.environ, {}, clear=True)
    def test_runs_parent_detected(self, tmp_path: Path):
        """workspace/runs/myrun -> shared dirs at runs/ level (ws.parent)."""
        ws = str(tmp_path / "workspace" / "runs" / "myrun")
        with patch("pipeline.config.load_dotenv"):
            cfg = Config.load(workspace=ws)
        assert cfg.workspace == Path(ws)
        # ws.parent is workspace/runs, and ws.parent.name == "runs" is True
        # so base = ws.parent = workspace/runs
        expected_base = tmp_path / "workspace" / "runs"
        assert cfg.media_dir == expected_base / "media"
        assert cfg.cache_dir == expected_base / "analysis_cache"
        assert cfg.keyframes_dir == expected_base / "keyframes"

    @patch.dict(os.environ, {}, clear=True)
    def test_custom_workspace_uses_self_for_shared(self, tmp_path: Path):
        """workspace/custom (not under runs/) uses itself for shared dirs."""
        ws = str(tmp_path / "workspace" / "custom")
        with patch("pipeline.config.load_dotenv"):
            cfg = Config.load(workspace=ws)
        assert cfg.media_dir == Path(ws) / "media"
        assert cfg.cache_dir == Path(ws) / "analysis_cache"
        assert cfg.keyframes_dir == Path(ws) / "keyframes"


class TestConfigLoadEnvVars:
    def test_env_var_overrides(self, tmp_path: Path):
        """Environment variables override default config values."""
        env = {
            "SYNOLOGY_API_BASE": "http://nas:5000",
            "OLLAMA_BASE": "http://gpu-box:11434",
            "VISION_MODEL": "llava:13b",
            "PLANNING_MODEL": "mistral:7b",
            "WHISPER_MODEL": "large",
            "WORKSPACE": str(tmp_path),
        }
        with patch.dict(os.environ, env, clear=True), \
             patch("pipeline.config.load_dotenv"):
            cfg = Config.load()
        assert cfg.api_base == "http://nas:5000"
        assert cfg.ollama_base == "http://gpu-box:11434"
        assert cfg.vision_model == "llava:13b"
        assert cfg.planning_model == "mistral:7b"
        assert cfg.whisper_model == "large"
        assert cfg.workspace == tmp_path

    def test_workspace_arg_overrides_env(self, tmp_path: Path):
        """Explicit workspace argument takes precedence over WORKSPACE env."""
        env = {"WORKSPACE": "/should/not/use"}
        ws = str(tmp_path / "explicit")
        with patch.dict(os.environ, env, clear=True), \
             patch("pipeline.config.load_dotenv"):
            cfg = Config.load(workspace=ws)
        assert cfg.workspace == Path(ws)


# -----------------------------------------------------------------------
# ensure_dirs()
# -----------------------------------------------------------------------


class TestEnsureDirs:
    @patch.dict(os.environ, {}, clear=True)
    def test_creates_all_directories(self, tmp_path: Path):
        """ensure_dirs() should create clips, output, media, cache, keyframes."""
        ws = str(tmp_path / "workspace")
        with patch("pipeline.config.load_dotenv"):
            cfg = Config.load(workspace=ws)
        cfg.ensure_dirs()

        assert (Path(ws) / "clips").is_dir()
        assert (Path(ws) / "output").is_dir()
        assert cfg.media_dir.is_dir()
        assert cfg.cache_dir.is_dir()
        assert cfg.keyframes_dir.is_dir()

    @patch.dict(os.environ, {}, clear=True)
    def test_idempotent(self, tmp_path: Path):
        """Calling ensure_dirs() twice should not raise."""
        ws = str(tmp_path / "workspace")
        with patch("pipeline.config.load_dotenv"):
            cfg = Config.load(workspace=ws)
        cfg.ensure_dirs()
        cfg.ensure_dirs()  # should not raise
        assert (Path(ws) / "clips").is_dir()
