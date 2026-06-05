"""Tests for YAML run config loading, validation, and CLI wiring."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

import click
import pytest
import yaml

from pipeline.cli import cli
from pipeline.cli._config_io import load_run_config
from tests.cli_helpers import capture_pipeline_run


class TestRunLoadsConfig:
    @pytest.fixture
    def cfg_file(self, tmp_path):
        text = """\
pipeline:
  stages: [prepare, plan, generate_music, assemble]
  force: true

source:
  path: .

plan:
  duration: 180
  model: quality
  lang: cn
  trip_type: solo
  style: cinematic
  focus: temples
  instruct: ''
  music: auto

assemble:
  resolution: 4k60
  bitrate: 1.5
"""
        p = tmp_path / "my_config.yaml"
        p.write_text(text)
        return str(p)

    def test_run_loads_full_config(self, runner, cfg_file):
        c = capture_pipeline_run(runner, cfg_file, run_name="test")

        assert c["stages"] == ["prepare", "plan", "generate_music", "assemble"]
        assert c["prepare"].force is True
        assert c["plan"].target_duration == 180
        assert c["plan"].style == "cinematic"
        assert c["assemble"].w == 3840
        assert c["assemble"].bitrate == 1.5

    def test_missing_file(self, runner):
        result = runner.invoke(cli, ["run", "test", "-c", "/nonexistent/config.yaml"])

        assert result.exit_code != 0


class TestConfigValidation:
    def _write_and_load(self, tmp_path, data):
        p = tmp_path / "cfg.yaml"
        p.write_text(yaml.dump(data))
        return load_run_config(str(p))

    def test_valid_config_passes(self, tmp_path):
        data = {
            "pipeline": {"stages": ["prepare", "plan", "assemble"]},
            "source": {"path": "/photos"},
            "plan": {"duration": 300, "model": "balanced"},
            "assemble": {"resolution": "4k60"},
        }

        assert self._write_and_load(tmp_path, data) == data

    @pytest.mark.parametrize(
        "data, match",
        [
            (
                {"source": {"path": "/photos"}, "bogus": 123},
                "unknown top-level keys.*bogus",
            ),
            (
                {"plan": {"duration": 300, "model": "fast", "foo": "bar"}},
                "'plan'.*unknown keys.*foo",
            ),
            ({"plan": {"model": "fast"}}, "'plan.duration' is required"),
            (
                {"plan": {"duration": "not_an_int", "model": "fast"}},
                "'plan.duration' must be int",
            ),
            (
                {"source": {"path": "/photos", "type": "local"}},
                "'source'.*unknown keys.*type",
            ),
            (
                {"pipeline": {"stages": ["prepare", "review"]}},
                "unknown stages: review",
            ),
            (
                {"pipeline": {"stages": "prepare"}},
                "'pipeline.stages' must be list",
            ),
            ({"plan": "not_a_dict"}, "'plan' must be an object"),
        ],
        ids=[
            "unknown_top_level",
            "unknown_in_group",
            "missing_required",
            "wrong_type",
            "unknown_in_source",
            "unknown_stage",
            "stages_not_list",
            "group_not_object",
        ],
    )
    def test_invalid_config(self, tmp_path, data, match):
        with pytest.raises(click.UsageError, match=match):
            self._write_and_load(tmp_path, data)

    def test_invalid_yaml(self, tmp_path):
        p = tmp_path / "bad.yaml"
        p.write_text(": broken: yaml: [")

        with pytest.raises(click.UsageError, match="invalid YAML"):
            load_run_config(str(p))

    def test_bitrate_accepts_int_or_float(self, tmp_path):
        data = {"assemble": {"resolution": "1080p30", "bitrate": 2}}
        result = self._write_and_load(tmp_path, data)
        assert result["assemble"]["bitrate"] == 2

    def test_multiple_errors_reported(self, tmp_path):
        data = {"plan": {"duration": "bad", "lang": "jp"}}
        with pytest.raises(click.UsageError) as exc_info:
            self._write_and_load(tmp_path, data)
        msg = str(exc_info.value)
        assert "plan.duration" in msg
        assert "plan.model" in msg
        assert "plan.lang" in msg


class TestConfigCommand:
    def _run_with_workspace(self, runner, ws: Path, run_name: str):
        with (
            patch("pipeline.config.Config.run_workspace", return_value=str(ws)),
            patch("pipeline.config.Config.load", return_value=Mock(workspace=ws)),
        ):
            return runner.invoke(cli, ["config", run_name], catch_exceptions=False)

    def test_prints_saved_config(self, runner, tmp_path):
        ws = tmp_path / "workspace" / "runs" / "myrun"
        ws.mkdir(parents=True)
        (ws / "run.yaml").write_text(
            "pipeline:\n  stages: [prepare]\n\nsource:\n  path: /photos\n"
        )

        result = self._run_with_workspace(runner, ws, "myrun")

        assert result.exit_code == 0
        assert "path: /photos" in result.output
        assert "stages: [prepare]" in result.output

    def test_missing_config_errors(self, runner, tmp_path):
        ws = tmp_path / "workspace" / "runs" / "norun"
        ws.mkdir(parents=True)

        result = self._run_with_workspace(runner, ws, "norun")

        assert result.exit_code != 0
        assert "No run.yaml" in result.output
