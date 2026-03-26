"""Tests for CLI run config save/load feature.

Verifies that:
- CLI parameters are auto-saved to run_config.json on every run
- --use-cfg-file loads parameters from a JSON file
- --use-cfg-file is mutually exclusive with other params (except -n, --force, -v)
- vlog config -n {name} prints the saved config
- Resolution round-trips correctly through save/load
"""

from __future__ import annotations

import json
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from pipeline.cli import (
    _RESOLUTION_PRESETS,
    _format_resolution,
    cli,
    config_path_for,
    save_run_config,
)


@pytest.fixture
def runner():
    return CliRunner()


# ---------------------------------------------------------------------------
# save_run_config / config_path_for unit tests
# ---------------------------------------------------------------------------


class TestSaveRunConfig:
    def test_saves_grouped_json(self, tmp_path):
        params = {
            "source": "local",
            "path": "/photos",
            "duration": 180,
            "model": "balanced",
        }
        dest = save_run_config(tmp_path, params)
        assert dest == config_path_for(tmp_path)
        assert dest.exists()
        loaded = json.loads(dest.read_text())
        assert loaded["source"]["type"] == "local"
        assert loaded["source"]["path"] == "/photos"
        assert loaded["plan"]["duration"] == 180
        assert loaded["plan"]["model"] == "balanced"

    def test_omits_none_values(self, tmp_path):
        params = {"source": "local", "path": None, "duration": 60}
        save_run_config(tmp_path, params)
        loaded = json.loads(config_path_for(tmp_path).read_text())
        assert "path" not in loaded["source"]

    def test_omits_unsaved_fields(self, tmp_path):
        params = {"source": "local", "force": True, "version": 2, "run_name": "test"}
        save_run_config(tmp_path, params)
        loaded = json.loads(config_path_for(tmp_path).read_text())
        assert "force" not in loaded
        assert "version" not in loaded
        assert "run_name" not in loaded
        assert loaded["source"]["type"] == "local"

    def test_overwrites_existing(self, tmp_path):
        save_run_config(tmp_path, {"source": "local", "duration": 60})
        save_run_config(tmp_path, {"source": "nas", "duration": 180})
        loaded = json.loads(config_path_for(tmp_path).read_text())
        assert loaded["source"]["type"] == "nas"
        assert loaded["plan"]["duration"] == 180

    def test_empty_groups_omitted(self, tmp_path):
        params = {"source": "local"}
        save_run_config(tmp_path, params)
        loaded = json.loads(config_path_for(tmp_path).read_text())
        assert "plan" not in loaded
        assert "assemble" not in loaded


# ---------------------------------------------------------------------------
# Resolution round-trip tests
# ---------------------------------------------------------------------------


class TestResolutionFormat:
    def test_preset_round_trip(self):
        for name, (w, h, fps) in _RESOLUTION_PRESETS.items():
            assert _format_resolution((w, h, fps)) == name

    def test_custom_resolution(self):
        assert _format_resolution((2560, 1440, 60)) == "2k60"  # this is a preset
        assert _format_resolution((2048, 1080, 24)) == "2048x1080x24"


# ---------------------------------------------------------------------------
# CLI → _run_pipeline wiring: cli_params saved
# ---------------------------------------------------------------------------


class TestFullSavesConfig:
    """Verify 'full' passes cli_params to _run_pipeline."""

    def _run_full(self, runner, extra_args=None):
        base_args = [
            "full",
            "-n",
            "test-run",
            "-s",
            "local",
            "-p",
            ".",
            "--duration",
            "60",
            "--model",
            "balanced",
            "-r",
            "1080p30",
        ]
        if extra_args:
            base_args.extend(extra_args)

        captured = {}

        def mock_pipeline(
            run_name,
            *,
            stages,
            fetch=None,
            prepare=None,
            plan=None,
            assemble=None,
            cli_params=None,
        ):
            captured["cli_params"] = cli_params

        with patch("pipeline.cli._commands._run_pipeline", side_effect=mock_pipeline):
            result = runner.invoke(cli, base_args, catch_exceptions=False)
        assert result.exit_code == 0, f"CLI failed: {result.output}"
        return captured

    def test_cli_params_passed(self, runner):
        c = self._run_full(runner)
        params = c["cli_params"]
        assert params is not None
        assert params["source"] == "local"
        assert params["duration"] == 60
        assert params["model"] == "balanced"
        assert params["resolution"] == "1080p30"

    def test_cli_params_bitrate(self, runner):
        c = self._run_full(runner, ["--bitrate", "2.0"])
        assert c["cli_params"]["bitrate"] == 2.0

    def test_cli_params_custom_resolution(self, runner):
        c = self._run_full(runner, ["-r", "2560x1440x60"])
        assert c["cli_params"]["resolution"] == "2k60"


class TestPlanSavesConfig:
    def test_cli_params_passed(self, runner):
        captured = {}

        def mock_pipeline(run_name, *, stages, cli_params=None, **kwargs):
            captured["cli_params"] = cli_params

        with patch("pipeline.cli._commands._run_pipeline", side_effect=mock_pipeline):
            result = runner.invoke(
                cli,
                ["plan", "-n", "test", "--duration", "120", "--model", "fast"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output
        params = captured["cli_params"]
        assert params["duration"] == 120
        assert params["model"] == "fast"


class TestAssembleSavesConfig:
    def test_cli_params_passed(self, runner):
        captured = {}

        def mock_pipeline(run_name, *, stages, cli_params=None, **kwargs):
            captured["cli_params"] = cli_params

        with patch("pipeline.cli._commands._run_pipeline", side_effect=mock_pipeline):
            result = runner.invoke(
                cli,
                ["assemble", "-n", "test", "-r", "4k60"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output
        assert captured["cli_params"]["resolution"] == "4k60"


# ---------------------------------------------------------------------------
# --use-cfg-file tests
# ---------------------------------------------------------------------------


class TestUseCfgFile:
    """Test loading config from a JSON file via --use-cfg-file."""

    @pytest.fixture
    def cfg_file(self, tmp_path):
        """Create a grouped config file and return its path."""
        cfg = {
            "source": {
                "type": "local",
                "path": ".",
            },
            "plan": {
                "duration": 180,
                "model": "quality",
                "lang": "cn",
                "trip_type": "solo",
                "style": "cinematic",
                "focus": "temples",
                "music": "auto",
            },
            "assemble": {
                "resolution": "4k60",
                "bitrate": 1.5,
            },
        }
        p = tmp_path / "my_config.json"
        p.write_text(json.dumps(cfg))
        return str(p)

    def test_full_loads_from_cfg(self, runner, cfg_file):
        captured = {}

        def mock_pipeline(run_name, *, stages, cli_params=None, **kwargs):
            captured["plan"] = kwargs.get("plan")
            captured["assemble"] = kwargs.get("assemble")
            captured["cli_params"] = cli_params

        with patch("pipeline.cli._commands._run_pipeline", side_effect=mock_pipeline):
            result = runner.invoke(
                cli,
                ["full", "-n", "test", "--use-cfg-file", cfg_file],
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output
        assert captured["plan"].target_duration == 180
        assert captured["plan"].language == "cn"
        assert captured["plan"].trip_type == "solo"
        assert captured["assemble"].w == 3840
        assert captured["assemble"].fps == 60

    def test_plan_loads_from_cfg(self, runner, cfg_file):
        captured = {}

        def mock_pipeline(run_name, *, stages, cli_params=None, **kwargs):
            captured["plan"] = kwargs.get("plan")

        with patch("pipeline.cli._commands._run_pipeline", side_effect=mock_pipeline):
            result = runner.invoke(
                cli,
                ["plan", "-n", "test", "--use-cfg-file", cfg_file],
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output
        assert captured["plan"].target_duration == 180
        assert captured["plan"].style == "cinematic"

    def test_assemble_loads_from_cfg(self, runner, cfg_file):
        captured = {}

        def mock_pipeline(run_name, *, stages, cli_params=None, **kwargs):
            captured["assemble"] = kwargs.get("assemble")

        with patch("pipeline.cli._commands._run_pipeline", side_effect=mock_pipeline):
            result = runner.invoke(
                cli,
                ["assemble", "-n", "test", "--use-cfg-file", cfg_file],
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output
        assert captured["assemble"].w == 3840
        assert captured["assemble"].quality == 1.5

    def test_use_cfg_rejects_extra_params(self, runner, cfg_file):
        """--use-cfg-file combined with --duration should fail."""
        with patch("pipeline.cli._commands._run_pipeline"):
            result = runner.invoke(
                cli,
                ["plan", "-n", "test", "--use-cfg-file", cfg_file, "--duration", "60"],
            )
        assert result.exit_code != 0
        assert "cannot be combined" in result.output

    def test_use_cfg_allows_force(self, runner, cfg_file):
        """--force is allowed alongside --use-cfg-file."""
        captured = {}

        def mock_pipeline(run_name, *, stages, cli_params=None, **kwargs):
            captured["plan"] = kwargs.get("plan")

        with patch("pipeline.cli._commands._run_pipeline", side_effect=mock_pipeline):
            result = runner.invoke(
                cli,
                ["plan", "-n", "test", "--use-cfg-file", cfg_file, "--force"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output
        assert captured["plan"].force is True

    def test_use_cfg_allows_version(self, runner, cfg_file):
        """-v is allowed alongside --use-cfg-file."""
        captured = {}

        def mock_pipeline(run_name, *, stages, cli_params=None, **kwargs):
            captured["assemble"] = kwargs.get("assemble")

        with patch("pipeline.cli._commands._run_pipeline", side_effect=mock_pipeline):
            result = runner.invoke(
                cli,
                ["assemble", "-n", "test", "--use-cfg-file", cfg_file, "-v", "2"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output
        assert captured["assemble"].version == 2

    def test_use_cfg_missing_file(self, runner):
        """Non-existent config file should fail."""
        result = runner.invoke(
            cli,
            ["plan", "-n", "test", "--use-cfg-file", "/nonexistent/config.json"],
        )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Config validation tests
# ---------------------------------------------------------------------------


class TestConfigValidation:
    """Verify that load_run_config rejects invalid configs."""

    def _write_and_load(self, tmp_path, data):
        from pipeline.cli import load_run_config

        p = tmp_path / "cfg.json"
        p.write_text(json.dumps(data))
        return load_run_config(str(p))

    def test_valid_config_passes(self, tmp_path):
        data = {
            "source": {"type": "local", "path": "/photos"},
            "plan": {"duration": 300, "model": "balanced"},
            "assemble": {"resolution": "4k60"},
        }
        result = self._write_and_load(tmp_path, data)
        assert result == data

    def test_unknown_top_level_key(self, tmp_path):
        data = {"source": {"type": "local"}, "bogus": 123}
        with pytest.raises(click.UsageError, match="unknown top-level keys.*bogus"):
            self._write_and_load(tmp_path, data)

    def test_unknown_key_in_group(self, tmp_path):
        data = {"plan": {"duration": 300, "model": "fast", "foo": "bar"}}
        with pytest.raises(click.UsageError, match="'plan'.*unknown keys.*foo"):
            self._write_and_load(tmp_path, data)

    def test_missing_required_field(self, tmp_path):
        data = {"plan": {"model": "fast"}}  # missing duration
        with pytest.raises(click.UsageError, match="'plan.duration' is required"):
            self._write_and_load(tmp_path, data)

    def test_wrong_type(self, tmp_path):
        data = {"plan": {"duration": "not_an_int", "model": "fast"}}
        with pytest.raises(click.UsageError, match="'plan.duration' must be int"):
            self._write_and_load(tmp_path, data)

    def test_invalid_choice(self, tmp_path):
        data = {"source": {"type": "ftp"}}
        with pytest.raises(click.UsageError, match="'source.type'.*must be one of"):
            self._write_and_load(tmp_path, data)

    def test_group_not_object(self, tmp_path):
        data = {"plan": "not_a_dict"}
        with pytest.raises(click.UsageError, match="'plan' must be an object"):
            self._write_and_load(tmp_path, data)

    def test_invalid_json(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{broken")
        from pipeline.cli import load_run_config

        with pytest.raises(click.UsageError, match="invalid JSON"):
            load_run_config(str(p))

    def test_bitrate_accepts_int_or_float(self, tmp_path):
        data = {"assemble": {"resolution": "1080p30", "bitrate": 2}}
        result = self._write_and_load(tmp_path, data)
        assert result["assemble"]["bitrate"] == 2

    def test_multiple_errors_reported(self, tmp_path):
        data = {
            "plan": {"duration": "bad", "lang": "jp"}
        }  # missing model, wrong type, bad choice
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
        # Write a grouped config file to the expected location
        ws = tmp_path / "workspace" / "runs" / "myrun"
        ws.mkdir(parents=True)
        cfg_data = {
            "source": {"type": "local", "path": "/photos"},
            "plan": {"duration": 120},
        }
        (ws / "run_config.json").write_text(json.dumps(cfg_data))

        with patch("pipeline.config.Config.run_workspace", return_value=str(ws)):
            with patch("pipeline.config.Config.load") as mock_load:
                mock_cfg = mock_load.return_value
                mock_cfg.workspace = ws
                result = runner.invoke(
                    cli, ["config", "-n", "myrun"], catch_exceptions=False
                )

        assert result.exit_code == 0, result.output
        assert '"type": "local"' in result.output
        assert '"duration": 120' in result.output

    def test_missing_config_errors(self, runner, tmp_path):
        ws = tmp_path / "workspace" / "runs" / "norun"
        ws.mkdir(parents=True)

        with patch("pipeline.config.Config.run_workspace", return_value=str(ws)):
            with patch("pipeline.config.Config.load") as mock_load:
                mock_cfg = mock_load.return_value
                mock_cfg.workspace = ws
                result = runner.invoke(cli, ["config", "-n", "norun"])

        assert result.exit_code != 0
        assert "No saved config" in result.output
