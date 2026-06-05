"""Tests for YAML run config save/load behavior."""

from __future__ import annotations

from unittest.mock import patch

import click
import pytest
import yaml
from click.testing import CliRunner

from pipeline.cli import (
    _RESOLUTION_PRESETS,
    _format_resolution,
    cli,
    list_configs,
    save_run_config,
)


@pytest.fixture
def runner():
    return CliRunner()


class TestSaveRunConfig:
    def test_saves_yaml_to_workspace(self, tmp_path):
        params = {
            "stages": ["prepare", "plan", "assemble"],
            "path": "/photos",
            "duration": 180,
            "model": "balanced",
        }
        dest = save_run_config(tmp_path, params)

        assert dest.exists()
        assert dest.name.startswith("run_config_")
        assert list_configs(tmp_path)[-1] == dest
        loaded = yaml.safe_load(dest.read_text())
        assert loaded["pipeline"]["stages"] == ["prepare", "plan", "assemble"]
        assert loaded["source"]["path"] == "/photos"
        assert loaded["plan"]["duration"] == 180
        assert loaded["plan"]["model"] == "balanced"

    @pytest.mark.parametrize(
        "params, assertion",
        [
            ({"path": None, "duration": 60}, lambda d: "source" not in d),
            (
                {
                    "path": "/photos",
                    "stages": ["prepare"],
                    "force": True,
                    "version": 2,
                    "run_name": "test",
                },
                lambda d: (
                    "run_name" not in d
                    and d["pipeline"]["force"] is True
                    and d["pipeline"]["version"] == 2
                    and d["source"]["path"] == "/photos"
                ),
            ),
            ({"path": "/photos"}, lambda d: "plan" not in d and "assemble" not in d),
        ],
        ids=["omits_none", "pipeline_fields", "empty_groups_omitted"],
    )
    def test_field_filtering(self, tmp_path, params, assertion):
        save_run_config(tmp_path, params)
        loaded = yaml.safe_load(list_configs(tmp_path)[-1].read_text())
        assert assertion(loaded)

    def test_multiple_configs_newest_last(self, tmp_path):
        save_run_config(tmp_path, {"path": "/photos", "duration": 60})
        save_run_config(tmp_path, {"path": "/other", "duration": 180})

        loaded = yaml.safe_load(list_configs(tmp_path)[-1].read_text())

        assert loaded["source"]["path"] == "/other"
        assert loaded["plan"]["duration"] == 180

    def test_default_annotations(self, tmp_path):
        params = {
            "path": "/photos",
            "duration": 300,
            "model": "balanced",
            "trip_type": "family",
        }
        dest = save_run_config(tmp_path, params, defaults={"trip_type"})
        text = dest.read_text()

        for line in text.splitlines():
            if "trip_type:" in line:
                assert "# default" in line
            if "duration:" in line:
                assert "# default" not in line

    def test_path_with_backslash_no_yaml_doc_end(self, tmp_path):
        params = {"path": r"C:\Users\guoyu\Projects\vlog\workspace\media"}
        dest = save_run_config(tmp_path, params)

        assert "..." not in dest.read_text()


class TestResolutionFormat:
    @pytest.mark.parametrize("name", list(_RESOLUTION_PRESETS.keys()))
    def test_preset_round_trip(self, name):
        w, h, fps = _RESOLUTION_PRESETS[name]
        assert _format_resolution((w, h, fps)) == name

    def test_custom_resolution(self):
        assert _format_resolution((2560, 1440, 60)) == "2k60"
        assert _format_resolution((2048, 1080, 24)) == "2048x1080x24"


def _capture_run(runner, cfg_file):
    captured = {}

    def mock_pipeline(
        run_name, *, stages, cli_params=None, cli_defaults=None, **kwargs
    ):
        captured["run_name"] = run_name
        captured["stages"] = stages
        captured["cli_params"] = cli_params
        captured["cli_defaults"] = cli_defaults
        captured.update(kwargs)

    with patch("pipeline.cli._commands._run_pipeline", side_effect=mock_pipeline):
        result = runner.invoke(
            cli, ["run", "test", "-c", cfg_file], catch_exceptions=False
        )
    assert result.exit_code == 0, result.output
    return captured


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
        c = _capture_run(runner, cfg_file)

        assert c["stages"] == ["prepare", "plan", "generate_music", "assemble"]
        assert c["prepare"].force is True
        assert c["plan"].target_duration == 180
        assert c["plan"].style == "cinematic"
        assert c["assemble"].w == 3840
        assert c["assemble"].bitrate == 1.5
        assert c["cli_params"]["stages"] == [
            "prepare",
            "plan",
            "generate_music",
            "assemble",
        ]
        assert c["cli_defaults"] == set()

    def test_missing_file(self, runner):
        result = runner.invoke(cli, ["run", "test", "-c", "/nonexistent/config.yaml"])

        assert result.exit_code != 0


class TestConfigValidation:
    def _write_and_load(self, tmp_path, data):
        from pipeline.cli import load_run_config

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
        from pipeline.cli import load_run_config

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
    def test_prints_saved_config(self, runner, tmp_path):
        ws = tmp_path / "workspace" / "runs" / "myrun"
        ws.mkdir(parents=True)
        (ws / "run_config_20260325_120000.yaml").write_text(
            "pipeline:\n  stages: [prepare]\n\nsource:\n  path: /photos\n"
        )

        with patch("pipeline.config.Config.run_workspace", return_value=str(ws)):
            with patch("pipeline.config.Config.load") as mock_load:
                mock_load.return_value.workspace = ws
                result = runner.invoke(cli, ["config", "myrun"], catch_exceptions=False)

        assert result.exit_code == 0
        assert "path: /photos" in result.output
        assert "stages: [prepare]" in result.output

    def test_missing_config_errors(self, runner, tmp_path):
        ws = tmp_path / "workspace" / "runs" / "norun"
        ws.mkdir(parents=True)

        with patch("pipeline.config.Config.run_workspace", return_value=str(ws)):
            with patch("pipeline.config.Config.load") as mock_load:
                mock_load.return_value.workspace = ws
                result = runner.invoke(cli, ["config", "norun"])

        assert result.exit_code != 0
        assert "No config files" in result.output
