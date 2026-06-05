"""End-to-end CLI tests using CliRunner with pipeline mocked."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from pipeline.cli import cli


@pytest.fixture
def runner():
    return CliRunner()


def _write_config(tmp_path, text: str) -> str:
    cfg = tmp_path / "run.yaml"
    cfg.write_text(text, encoding="utf-8")
    return str(cfg)


class TestTopLevelHelp:
    def test_help_exits_zero(self, runner):
        result = runner.invoke(cli, ["--help"])

        assert result.exit_code == 0

    def test_help_lists_current_commands_only(self, runner):
        result = runner.invoke(cli, ["--help"])

        for cmd in ("run", "new", "edit", "workspace", "config"):
            assert cmd in result.output
        for old_cmd in ("full", "prepare", "plan", "assemble", "init"):
            assert old_cmd not in result.output


class TestInvalidInputs:
    def test_invalid_resolution_format(self, runner, tmp_path):
        cfg = _write_config(
            tmp_path,
            """\
pipeline:
  stages: [assemble]

assemble:
  resolution: potato
""",
        )

        result = runner.invoke(cli, ["run", "test", "-c", cfg])

        assert result.exit_code != 0
        assert "Unknown resolution" in result.output

    def test_invalid_trip_type(self, runner, tmp_path):
        cfg = _write_config(
            tmp_path,
            """\
pipeline:
  stages: [plan]

plan:
  duration: 60
  model: balanced
  trip_type: spaceflight
""",
        )

        result = runner.invoke(cli, ["run", "test", "-c", cfg])

        assert result.exit_code != 0
        assert "plan.trip_type" in result.output

    def test_invalid_lang(self, runner, tmp_path):
        cfg = _write_config(
            tmp_path,
            """\
pipeline:
  stages: [plan]

plan:
  duration: 60
  model: balanced
  lang: jp
""",
        )

        result = runner.invoke(cli, ["run", "test", "-c", cfg])

        assert result.exit_code != 0
        assert "plan.lang" in result.output

    def test_negative_duration(self, runner, tmp_path):
        cfg = _write_config(
            tmp_path,
            """\
pipeline:
  stages: [plan]

plan:
  duration: -10
  model: balanced
""",
        )

        with patch("pipeline.cli._commands._run_pipeline"):
            result = runner.invoke(cli, ["run", "test", "-c", cfg])

        assert result.exit_code != 0

    @pytest.mark.parametrize("old_cmd", ["full", "prepare", "plan", "assemble", "init"])
    def test_old_commands_removed(self, runner, old_cmd):
        result = runner.invoke(cli, [old_cmd, "--help"])

        assert result.exit_code != 0
        assert "No such command" in result.output


class TestSubcommandHelp:
    @pytest.mark.parametrize("cmd", ["run", "new", "edit", "workspace", "config"])
    def test_help_exits_zero(self, runner, cmd):
        result = runner.invoke(cli, [cmd, "--help"])

        assert result.exit_code == 0
        assert "Usage" in result.output


class TestNewCommand:
    def test_new_writes_config(self, runner, tmp_path, monkeypatch):
        media = tmp_path / "media"
        media.mkdir()
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(cli, ["new", "trip", str(media)])

        assert result.exit_code == 0
        out = tmp_path / "workspace" / "runs" / "trip" / "run.yaml"
        text = out.read_text(encoding="utf-8")
        assert "pipeline:" in text
        assert "stages:" in text
        assert "source:" in text
        assert str(media) in text
        assert "plan:" in text
        assert "assemble:" in text
        assert "Next: reelsmith run trip" in result.output

    def test_new_refuses_overwrite_without_force(self, runner, tmp_path, monkeypatch):
        media = tmp_path / "media"
        media.mkdir()
        out = tmp_path / "workspace" / "runs" / "trip" / "run.yaml"
        out.parent.mkdir(parents=True)
        out.write_text("existing")
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(cli, ["new", "trip", str(media)])

        assert result.exit_code != 0
        assert out.read_text() == "existing"

    def test_new_force_overwrites(self, runner, tmp_path, monkeypatch):
        media = tmp_path / "media"
        media.mkdir()
        out = tmp_path / "workspace" / "runs" / "trip" / "run.yaml"
        out.parent.mkdir(parents=True)
        out.write_text("existing")
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(cli, ["new", "trip", str(media), "--force"])

        assert result.exit_code == 0
        assert "pipeline:" in out.read_text()


class TestEditCommand:
    def test_edit_opens_default_config(self, runner, tmp_path, monkeypatch):
        cfg = tmp_path / "workspace" / "runs" / "trip" / "run.yaml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("pipeline:\n  stages: [assemble]\n")
        monkeypatch.chdir(tmp_path)

        with patch("click.edit") as edit:
            result = runner.invoke(cli, ["edit", "trip"], catch_exceptions=False)

        assert result.exit_code == 0
        edit.assert_called_once_with(filename=str(cfg))
