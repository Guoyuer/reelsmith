"""Tests for CLI run config save/load feature.

Verifies that:
- CLI parameters are auto-saved to run_config.yaml on every run
- --use-cfg-file loads parameters from a YAML file
- --use-cfg-file is mutually exclusive with other params (except -n, --force, -v)
- vlog config -n {name} prints the saved config
- Resolution round-trips correctly through save/load
- Default annotations (# default) are present in saved YAML
"""

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


# ---------------------------------------------------------------------------
# save_run_config / config_path_for unit tests
# ---------------------------------------------------------------------------


class TestSaveRunConfig:
    def test_saves_yaml_to_workspace(self, tmp_path):
        params = {"path": "/photos", "duration": 180, "model": "balanced"}
        dest = save_run_config(tmp_path, params)
        assert dest.exists()
        assert dest.name.startswith("run_config_")
        assert list_configs(tmp_path)[-1] == dest
        loaded = yaml.safe_load(dest.read_text())
        assert loaded["source"]["path"] == "/photos"
        assert loaded["plan"]["duration"] == 180
        assert loaded["plan"]["model"] == "balanced"

    @pytest.mark.parametrize(
        "params, assertion",
        [
            ({"path": None, "duration": 60}, lambda d: "source" not in d),
            (
                {"path": "/photos", "force": True, "version": 2, "run_name": "test"},
                lambda d: all(k not in d for k in ("force", "version", "run_name"))
                and d["source"]["path"] == "/photos",
            ),
            ({"path": "/photos"}, lambda d: "plan" not in d and "assemble" not in d),
        ],
        ids=["omits_none", "omits_unsaved_fields", "empty_groups_omitted"],
    )
    def test_field_filtering(self, tmp_path, params, assertion):
        save_run_config(tmp_path, params)
        loaded = yaml.safe_load(list_configs(tmp_path)[-1].read_text())
        assert assertion(loaded)

    def test_overwrites_existing(self, tmp_path):
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

    def test_config_filename_has_timestamp(self, tmp_path):
        dest = save_run_config(tmp_path, {"path": "/photos"})
        assert dest.name.startswith("run_config_")
        assert dest.name.endswith(".yaml")


# ---------------------------------------------------------------------------
# Resolution round-trip tests
# ---------------------------------------------------------------------------


class TestResolutionFormat:
    @pytest.mark.parametrize("name", list(_RESOLUTION_PRESETS.keys()))
    def test_preset_round_trip(self, name):
        w, h, fps = _RESOLUTION_PRESETS[name]
        assert _format_resolution((w, h, fps)) == name

    def test_custom_resolution(self):
        assert _format_resolution((2560, 1440, 60)) == "2k60"
        assert _format_resolution((2048, 1080, 24)) == "2048x1080x24"


# ---------------------------------------------------------------------------
# CLI → _run_pipeline wiring: cli_params saved
# ---------------------------------------------------------------------------


def _capture_cli_params(runner, cli_args):
    """Run CLI and capture cli_params/cli_defaults from _run_pipeline."""
    captured = {}

    def mock_pipeline(
        run_name, *, stages, cli_params=None, cli_defaults=None, **kwargs
    ):
        captured["cli_params"] = cli_params
        captured["cli_defaults"] = cli_defaults
        captured.update(kwargs)

    with patch("pipeline.cli._commands._run_pipeline", side_effect=mock_pipeline):
        result = runner.invoke(cli, cli_args, catch_exceptions=False)
    assert result.exit_code == 0, f"CLI failed: {result.output}"
    return captured


_FULL_BASE = [
    "full", "-n", "test-run", "-p", ".", "--duration", "60",
    "--model", "balanced", "-r", "1080p30",
]


class TestFullSavesConfig:
    def test_cli_params_passed(self, runner):
        c = _capture_cli_params(runner, _FULL_BASE)
        params = c["cli_params"]
        assert params["path"] == "."
        assert params["duration"] == 60
        assert params["model"] == "balanced"
        assert params["resolution"] == "1080p30"

    def test_cli_defaults_tracked(self, runner):
        c = _capture_cli_params(runner, _FULL_BASE)
        defaults = c["cli_defaults"]
        # Explicitly passed → not defaults
        for key in ("path", "duration", "model"):
            assert key not in defaults
        # Not passed → defaults
        for key in ("trip_type", "style", "music"):
            assert key in defaults

    def test_cli_params_bitrate(self, runner):
        c = _capture_cli_params(runner, _FULL_BASE + ["--bitrate", "2.0"])
        assert c["cli_params"]["bitrate"] == 2.0
        assert "quality" not in c["cli_defaults"]

    def test_cli_params_custom_resolution(self, runner):
        c = _capture_cli_params(runner, _FULL_BASE + ["-r", "2560x1440x60"])
        assert c["cli_params"]["resolution"] == "2k60"


class TestPlanSavesConfig:
    def test_cli_params_passed(self, runner):
        c = _capture_cli_params(
            runner,
            ["plan", "-n", "test", "--duration", "120", "--model", "fast"],
        )
        assert c["cli_params"]["duration"] == 120
        assert c["cli_params"]["model"] == "fast"
        assert "trip_type" in c["cli_defaults"]


class TestAssembleSavesConfig:
    def test_cli_params_passed(self, runner):
        c = _capture_cli_params(runner, ["assemble", "-n", "test", "-r", "4k60"])
        assert c["cli_params"]["resolution"] == "4k60"
        assert "quality" in c["cli_defaults"]


# ---------------------------------------------------------------------------
# --use-cfg-file tests
# ---------------------------------------------------------------------------


class TestUseCfgFile:
    @pytest.fixture
    def cfg_file(self, tmp_path):
        text = """\
source:
  path: .

plan:
  duration: 180
  model: quality
  lang: cn
  trip_type: solo
  style: cinematic
  focus: temples
  music: auto

assemble:
  resolution: 4k60
  bitrate: 1.5
"""
        p = tmp_path / "my_config.yaml"
        p.write_text(text)
        return str(p)

    @pytest.mark.parametrize(
        "command, check_fn",
        [
            (
                ["full", "-n", "test", "--use-cfg-file"],
                lambda c: (
                    c["plan"].target_duration == 180
                    and c["plan"].language == "cn"
                    and c["plan"].trip_type == "solo"
                    and c["assemble"].w == 3840
                    and c["assemble"].fps == 60
                ),
            ),
            (
                ["plan", "-n", "test", "--use-cfg-file"],
                lambda c: c["plan"].target_duration == 180 and c["plan"].style == "cinematic",
            ),
            (
                ["assemble", "-n", "test", "--use-cfg-file"],
                lambda c: c["assemble"].w == 3840 and c["assemble"].quality == 1.5,
            ),
        ],
        ids=["full", "plan", "assemble"],
    )
    def test_loads_from_cfg(self, runner, cfg_file, command, check_fn):
        c = _capture_cli_params(runner, command + [cfg_file])
        assert check_fn(c)

    def test_rejects_extra_params(self, runner, cfg_file):
        with patch("pipeline.cli._commands._run_pipeline"):
            result = runner.invoke(
                cli,
                ["plan", "-n", "test", "--use-cfg-file", cfg_file, "--duration", "60"],
            )
        assert result.exit_code != 0
        assert "cannot be combined" in result.output

    def test_allows_force(self, runner, cfg_file):
        c = _capture_cli_params(
            runner,
            ["plan", "-n", "test", "--use-cfg-file", cfg_file, "--force"],
        )
        assert c["plan"].force is True

    def test_allows_version(self, runner, cfg_file):
        c = _capture_cli_params(
            runner,
            ["assemble", "-n", "test", "--use-cfg-file", cfg_file, "-v", "2"],
        )
        assert c["assemble"].version == 2

    def test_missing_file(self, runner):
        result = runner.invoke(
            cli,
            ["plan", "-n", "test", "--use-cfg-file", "/nonexistent/config.yaml"],
        )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Config validation tests
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def _write_and_load(self, tmp_path, data):
        from pipeline.cli import load_run_config

        p = tmp_path / "cfg.yaml"
        p.write_text(yaml.dump(data))
        return load_run_config(str(p))

    def test_valid_config_passes(self, tmp_path):
        data = {
            "source": {"path": "/photos"},
            "plan": {"duration": 300, "model": "balanced"},
            "assemble": {"resolution": "4k60"},
        }
        assert self._write_and_load(tmp_path, data) == data

    @pytest.mark.parametrize(
        "data, match",
        [
            ({"source": {"path": "/photos"}, "bogus": 123}, "unknown top-level keys.*bogus"),
            ({"plan": {"duration": 300, "model": "fast", "foo": "bar"}}, "'plan'.*unknown keys.*foo"),
            ({"plan": {"model": "fast"}}, "'plan.duration' is required"),
            ({"plan": {"duration": "not_an_int", "model": "fast"}}, "'plan.duration' must be int"),
            ({"source": {"path": "/photos", "type": "local"}}, "'source'.*unknown keys.*type"),
            ({"plan": "not_a_dict"}, "'plan' must be an object"),
        ],
        ids=[
            "unknown_top_level",
            "unknown_in_group",
            "missing_required",
            "wrong_type",
            "unknown_in_source",
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


# ---------------------------------------------------------------------------
# vlog config command tests
# ---------------------------------------------------------------------------


class TestConfigCommand:
    def test_prints_saved_config(self, runner, tmp_path):
        ws = tmp_path / "workspace" / "runs" / "myrun"
        ws.mkdir(parents=True)
        (ws / "run_config_20260325_120000.yaml").write_text(
            "source:\n  path: /photos\n\nplan:\n  duration: 120\n"
        )

        with patch("pipeline.config.Config.run_workspace", return_value=str(ws)):
            with patch("pipeline.config.Config.load") as mock_load:
                mock_load.return_value.workspace = ws
                result = runner.invoke(
                    cli, ["config", "-n", "myrun"], catch_exceptions=False
                )

        assert result.exit_code == 0
        assert "path: /photos" in result.output
        assert "duration: 120" in result.output

    def test_missing_config_errors(self, runner, tmp_path):
        ws = tmp_path / "workspace" / "runs" / "norun"
        ws.mkdir(parents=True)

        with patch("pipeline.config.Config.run_workspace", return_value=str(ws)):
            with patch("pipeline.config.Config.load") as mock_load:
                mock_load.return_value.workspace = ws
                result = runner.invoke(cli, ["config", "-n", "norun"])

        assert result.exit_code != 0
        assert "No config files" in result.output
