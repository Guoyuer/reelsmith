"""Tests for YAML-first CLI run parsing and Config construction."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from pipeline.cli import _PLANNING_PRESETS, _RESOLUTION_PRESETS, _resolve_planning, cli


class TestResolvePlanning:
    @pytest.mark.parametrize(
        "input_str, expected_model, expected_thinking",
        [
            ("fast", "gemini-3.1-flash-lite", "LOW"),
            ("balanced", "gemini-3.5-flash", "HIGH"),
            ("quality", "gemini-3.1-pro-preview", "HIGH"),
            ("gemini-2.5-flash", "gemini-2.5-flash", "HIGH"),
            ("gemini-2.5-flash:low", "gemini-2.5-flash", "LOW"),
            ("gemini-3-pro-preview:off", "gemini-3-pro-preview", "OFF"),
            ("gemini-2.5-pro:medium", "gemini-2.5-pro", "MEDIUM"),
        ],
    )
    def test_resolve_planning(self, input_str, expected_model, expected_thinking):
        model, thinking = _resolve_planning(input_str)
        assert model == expected_model
        assert thinking == expected_thinking

    @pytest.mark.parametrize("name", list(_PLANNING_PRESETS.keys()))
    def test_all_presets_resolve(self, name):
        model, thinking = _PLANNING_PRESETS[name]
        assert _resolve_planning(name) == (model, thinking)


class TestResolutionPresets:
    @pytest.mark.parametrize("name", list(_RESOLUTION_PRESETS.keys()))
    def test_presets_are_valid(self, name):
        w, h, fps = _RESOLUTION_PRESETS[name]
        assert w % 2 == 0
        assert h % 2 == 0
        assert fps > 0


@pytest.fixture
def runner():
    return CliRunner()


def _write_config(tmp_path, text: str) -> str:
    cfg = tmp_path / "run.yaml"
    cfg.write_text(text, encoding="utf-8")
    return str(cfg)


def _capture_pipeline_call(runner, cfg_file: str | None, run_name: str = "test-run"):
    captured = {}

    def mock_pipeline(
        run_name,
        *,
        stages,
        source_dir=None,
        prepare=None,
        plan=None,
        assemble=None,
        cli_params=None,
        cli_defaults=None,
    ):
        captured["run_name"] = run_name
        captured["stages"] = stages
        captured["source_dir"] = source_dir
        captured["prepare"] = prepare
        captured["plan"] = plan
        captured["assemble"] = assemble
        captured["cli_params"] = cli_params
        captured["cli_defaults"] = cli_defaults

    with patch("pipeline.cli._commands._run_pipeline", side_effect=mock_pipeline):
        args = ["run", run_name]
        if cfg_file is not None:
            args.extend(["-c", cfg_file])
        result = runner.invoke(cli, args, catch_exceptions=False)
    assert result.exit_code == 0, result.output
    return captured


FULL_CONFIG = """\
pipeline:
  stages: [prepare, plan, generate_music, assemble]
  force: true

source:
  path: .

plan:
  duration: 180
  model: fast
  lang: cn
  trip_type: solo
  style: cinematic
  focus: temples
  instruct: no snakes
  music: auto

assemble:
  resolution: 1080p30
  bitrate: 1.5
  codec: h264
"""


class TestRunCommandWiring:
    def test_full_yaml_builds_all_configs(self, runner, tmp_path):
        c = _capture_pipeline_call(runner, _write_config(tmp_path, FULL_CONFIG))

        assert c["run_name"] == "test-run"
        assert c["stages"] == ["prepare", "plan", "generate_music", "assemble"]
        assert c["source_dir"] == "."
        assert c["prepare"].force is True
        assert c["plan"].target_duration == 180
        assert c["plan"].model == "gemini-3.1-flash-lite"
        assert c["plan"].thinking_level == "LOW"
        assert c["plan"].language == "cn"
        assert c["plan"].trip_type == "solo"
        assert c["plan"].style == "cinematic"
        assert c["plan"].focus == "temples"
        assert c["assemble"].w == 1920
        assert c["assemble"].h == 1080
        assert c["assemble"].fps == 30
        assert c["assemble"].bitrate == 1.5
        assert c["assemble"].codec == "h264"
        assert c["cli_defaults"] == set()

    def test_plan_only_yaml(self, runner, tmp_path):
        cfg = _write_config(
            tmp_path,
            """\
pipeline:
  stages: [plan]

plan:
  duration: 60
  model: gemini-2.5-flash:medium
""",
        )

        c = _capture_pipeline_call(runner, cfg)

        assert c["stages"] == ["plan"]
        assert c["source_dir"] is None
        assert c["prepare"] is None
        assert c["assemble"] is None
        assert c["plan"].model == "gemini-2.5-flash"
        assert c["plan"].thinking_level == "MEDIUM"

    def test_assemble_only_yaml_with_version(self, runner, tmp_path):
        cfg = _write_config(
            tmp_path,
            """\
pipeline:
  stages: [assemble]
  version: 3

assemble:
  resolution: 2560x1440x60
  bitrate: 2
""",
        )

        c = _capture_pipeline_call(runner, cfg)

        assert c["stages"] == ["assemble"]
        assert c["assemble"].w == 2560
        assert c["assemble"].h == 1440
        assert c["assemble"].fps == 60
        assert c["assemble"].bitrate == 2.0
        assert c["assemble"].version == 3

    def test_default_config_path(self, runner, tmp_path, monkeypatch):
        run_dir = tmp_path / "workspace" / "runs" / "trip"
        run_dir.mkdir(parents=True)
        (run_dir / "run.yaml").write_text(FULL_CONFIG, encoding="utf-8")
        monkeypatch.chdir(tmp_path)

        c = _capture_pipeline_call(runner, None, run_name="trip")

        assert c["run_name"] == "trip"

    def test_stage_override(self, runner, tmp_path):
        captured = {}

        def mock_pipeline(run_name, *, stages, assemble=None, **kwargs):
            captured["stages"] = stages
            captured["assemble"] = assemble

        cfg = _write_config(tmp_path, FULL_CONFIG)
        with patch("pipeline.cli._commands._run_pipeline", side_effect=mock_pipeline):
            result = runner.invoke(
                cli,
                [
                    "run",
                    "trip",
                    "-c",
                    cfg,
                    "--stages",
                    "assemble",
                    "--version",
                    "2",
                ],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert captured["stages"] == ["assemble"]
        assert captured["assemble"].version == 2

    def test_missing_required_stage_section_fails(self, runner, tmp_path):
        cfg = _write_config(
            tmp_path,
            """\
pipeline:
  stages: [prepare]
""",
        )

        result = runner.invoke(cli, ["run", "test", "-c", cfg])

        assert result.exit_code != 0
        assert "source" in result.output

    def test_old_commands_are_not_registered(self, runner):
        result = runner.invoke(cli, ["full", "--help"])

        assert result.exit_code != 0
        assert "No such command" in result.output


class TestRunPrepareNoAnalysisPath:
    """Verify _run_prepare doesn't use removed Config.analysis_path."""

    def test_cached_prepare_uses_load_analysis(self, tmp_path):
        import json
        from unittest.mock import MagicMock

        from pipeline.config import Config
        from pipeline.prepare import PrepareConfig

        cfg = Config(workspace=tmp_path / "runs" / "test")
        cfg.ensure_dirs()

        manifest = [
            {
                "local_path": str(tmp_path / "p.jpg"),
                "taken_at": "2025-01-01T00:00:00",
                "takentime": 1700000000,
            }
        ]
        cfg.manifest_path.write_text(json.dumps(manifest))
        cfg.analysis_path.write_text(
            json.dumps(
                [
                    {
                        "local_path": str(tmp_path / "p.jpg"),
                        "media_type": "photo",
                        "taken_at": "2025-01-01T00:00:00",
                        "thumbnail_path": "/t.jpg",
                    }
                ]
            )
        )

        from pipeline.cli import _PipelineDisplay
        from pipeline.cli._runner import _run_prepare

        pc = MagicMock()
        pc.cfg = cfg
        pc.prepare = PrepareConfig()
        pc.display = MagicMock(spec=_PipelineDisplay)
        pc.logger = MagicMock()

        _run_prepare(pc)
