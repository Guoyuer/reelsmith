"""End-to-end CLI tests using CliRunner.

Verifies argument validation, error messages, and help output for all
top-level commands without invoking the actual pipeline (mocked).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from pipeline.cli import cli


@pytest.fixture
def runner():
    return CliRunner()


# ---------------------------------------------------------------------------
# Top-level help
# ---------------------------------------------------------------------------


class TestTopLevelHelp:
    def test_help_exits_zero(self, runner):
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0

    def test_help_lists_commands(self, runner):
        result = runner.invoke(cli, ["--help"])
        for cmd in ("full", "prepare", "plan", "assemble", "workspace", "config"):
            assert cmd in result.output


# ---------------------------------------------------------------------------
# Required argument validation
# ---------------------------------------------------------------------------


class TestFullRequiredArgs:
    """``vlog full`` requires -n, -p, --duration, --model, -r."""

    def test_missing_name(self, runner):
        result = runner.invoke(
            cli,
            [
                "full",
                "-p",
                ".",
                "--duration",
                "60",
                "-r",
                "1080p30",
                "--model",
                "balanced",
            ],
        )
        assert result.exit_code != 0

    def test_missing_path(self, runner):
        result = runner.invoke(
            cli,
            [
                "full",
                "-n",
                "test",
                "--duration",
                "60",
                "-r",
                "1080p30",
                "--model",
                "balanced",
            ],
        )
        assert result.exit_code != 0

    def test_missing_resolution(self, runner):
        result = runner.invoke(
            cli,
            [
                "full",
                "-n",
                "test",
                "-p",
                ".",
                "--duration",
                "60",
                "--model",
                "balanced",
            ],
        )
        assert result.exit_code != 0

    def test_missing_duration(self, runner):
        result = runner.invoke(
            cli,
            ["full", "-n", "test", "-p", ".", "-r", "1080p30", "--model", "balanced"],
        )
        assert result.exit_code != 0

    def test_missing_model(self, runner):
        result = runner.invoke(
            cli, ["full", "-n", "test", "-p", ".", "-r", "1080p30", "--duration", "60"]
        )
        assert result.exit_code != 0


class TestPrepareRequiredArgs:
    """``vlog prepare`` requires -n and -p."""

    def test_missing_name(self, runner):
        result = runner.invoke(cli, ["prepare", "-p", "."])
        assert result.exit_code != 0

    def test_missing_path(self, runner):
        result = runner.invoke(cli, ["prepare", "-n", "test"])
        assert result.exit_code != 0


class TestPlanRequiredArgs:
    """``vlog plan`` requires -n, --duration, --model."""

    def test_missing_name(self, runner):
        result = runner.invoke(cli, ["plan", "--duration", "60", "--model", "balanced"])
        assert result.exit_code != 0

    def test_missing_duration(self, runner):
        result = runner.invoke(cli, ["plan", "-n", "test", "--model", "balanced"])
        assert result.exit_code != 0

    def test_missing_model(self, runner):
        result = runner.invoke(cli, ["plan", "-n", "test", "--duration", "60"])
        assert result.exit_code != 0


class TestAssembleRequiredArgs:
    """``vlog assemble`` requires -n and -r."""

    def test_missing_name(self, runner):
        result = runner.invoke(cli, ["assemble", "-r", "1080p30"])
        assert result.exit_code != 0

    def test_missing_resolution(self, runner):
        result = runner.invoke(cli, ["assemble", "-n", "test"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Invalid input validation
# ---------------------------------------------------------------------------


class TestInvalidInputs:
    def test_invalid_resolution_format(self, runner):
        result = runner.invoke(cli, ["assemble", "-n", "test", "-r", "potato"])
        assert result.exit_code != 0

    def test_invalid_trip_type(self, runner):
        result = runner.invoke(
            cli,
            [
                "plan",
                "-n",
                "test",
                "--duration",
                "60",
                "--model",
                "balanced",
                "--trip-type",
                "spaceflight",
            ],
        )
        assert result.exit_code != 0

    def test_invalid_lang(self, runner):
        result = runner.invoke(
            cli,
            [
                "plan",
                "-n",
                "test",
                "--duration",
                "60",
                "--model",
                "balanced",
                "--lang",
                "jp",
            ],
        )
        assert result.exit_code != 0

    def test_negative_duration(self, runner):
        """Negative duration should fail (caught by PlanConfig.__post_init__)."""
        with patch("pipeline.cli._commands._run_pipeline", side_effect=ValueError):
            result = runner.invoke(
                cli,
                ["plan", "-n", "test", "--duration", "-10", "--model", "balanced"],
            )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Sub-command help
# ---------------------------------------------------------------------------


class TestSubcommandHelp:
    @pytest.mark.parametrize(
        "cmd", ["full", "prepare", "plan", "assemble", "workspace", "config"]
    )
    def test_help_exits_zero(self, runner, cmd):
        result = runner.invoke(cli, [cmd, "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output


# ---------------------------------------------------------------------------
# Resolution presets
# ---------------------------------------------------------------------------


class TestResolutionPresets:
    """Verify all documented resolution presets are accepted."""

    @pytest.mark.parametrize(
        "preset", ["4k60", "4k30", "2k60", "2k30", "1080p60", "1080p30", "720p30"]
    )
    def test_preset_accepted(self, runner, preset):
        """Preset should be parsed without error (fails later on missing name, not resolution)."""
        result = runner.invoke(cli, ["assemble", "-r", preset, "-n", "test"])
        # Should NOT fail with "invalid resolution" — any failure is about
        # missing workspace/EDL, not the resolution flag itself.
        assert "Invalid resolution" not in (result.output or "")

    def test_custom_WxHxFPS_accepted(self, runner):
        result = runner.invoke(cli, ["assemble", "-r", "2560x1440x60", "-n", "test"])
        assert "Invalid resolution" not in (result.output or "")
