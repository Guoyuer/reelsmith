"""Tests for CLI parameter parsing and Config construction.

Verifies that CLI arguments correctly wire through to Config dataclasses
without actually running the pipeline (mocks _run_pipeline).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from pipeline.cli import _PLANNING_PRESETS, _RESOLUTION_PRESETS, _resolve_planning, cli

# ---------------------------------------------------------------------------
# _resolve_planning unit tests
# ---------------------------------------------------------------------------


class TestResolvePlanning:
    @pytest.mark.parametrize(
        "input_str, expected_model, expected_thinking",
        [
            ("fast", "gemini-3.1-flash-lite-preview", "LOW"),
            ("balanced", "gemini-3-flash-preview", "HIGH"),
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


# ---------------------------------------------------------------------------
# Resolution preset tests
# ---------------------------------------------------------------------------


class TestResolutionPresets:
    @pytest.mark.parametrize("name", list(_RESOLUTION_PRESETS.keys()))
    def test_presets_are_valid(self, name):
        """Width and height must be even, fps must be positive."""
        w, h, fps = _RESOLUTION_PRESETS[name]
        assert w % 2 == 0, f"{name}: width {w} is odd"
        assert h % 2 == 0, f"{name}: height {h} is odd"
        assert fps > 0, f"{name}: fps {fps} <= 0"

    @pytest.mark.parametrize(
        "name, expected",
        [
            ("4k60", (3840, 2160, 60)),
            ("1080p30", (1920, 1080, 30)),
            ("720p30", (1280, 720, 30)),
        ],
    )
    def test_specific_presets(self, name, expected):
        assert _RESOLUTION_PRESETS[name] == expected


# ---------------------------------------------------------------------------
# Shared CLI test infrastructure
# ---------------------------------------------------------------------------


@pytest.fixture
def runner():
    return CliRunner()


def _capture_pipeline_call(cli_args, runner):
    """Run a CLI command capturing the _run_pipeline call kwargs."""
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
        cli_defaults=None,
    ):
        captured["run_name"] = run_name
        captured["stages"] = stages
        captured["fetch"] = fetch
        captured["prepare"] = prepare
        captured["plan"] = plan
        captured["assemble"] = assemble
        captured["cli_params"] = cli_params
        captured["cli_defaults"] = cli_defaults

    with patch("pipeline.cli._commands._run_pipeline", side_effect=mock_pipeline):
        result = runner.invoke(cli, cli_args, catch_exceptions=False)
    assert result.exit_code == 0, f"CLI failed: {result.output}"
    return captured


_FULL_BASE_ARGS = [
    "full",
    "-n",
    "test-run",
    "-p",
    ".",
    "--duration",
    "60",
    "--model",
    "balanced",
    "-r",
    "1080p30",
]


# ---------------------------------------------------------------------------
# CLI → Config wiring tests
# ---------------------------------------------------------------------------


class TestFullCommandWiring:
    """Verify 'full' command correctly constructs all Config objects."""

    def _run_full(self, runner, extra_args=None):
        args = list(_FULL_BASE_ARGS)
        if extra_args:
            args.extend(extra_args)
        return _capture_pipeline_call(args, runner)

    @pytest.mark.parametrize(
        "model_flag, expected_model, expected_thinking",
        [
            (None, "gemini-3-flash-preview", "HIGH"),  # default: balanced
            ("quality", "gemini-3.1-pro-preview", "HIGH"),
            ("fast", "gemini-3.1-flash-lite-preview", "LOW"),
            ("gemini-2.5-flash:medium", "gemini-2.5-flash", "MEDIUM"),
        ],
        ids=["balanced_default", "quality", "fast", "custom_model"],
    )
    def test_plan_config_model(
        self, runner, model_flag, expected_model, expected_thinking
    ):
        extra = ["--model", model_flag] if model_flag else []
        c = self._run_full(runner, extra)
        assert c["plan"].model == expected_model
        assert c["plan"].thinking_level == expected_thinking

    def test_plan_config_fields(self, runner):
        c = self._run_full(
            runner,
            [
                "--trip-type",
                "solo",
                "--style",
                "cinematic",
                "--focus",
                "temples",
                "--lang",
                "cn",
                "--duration",
                "180",
            ],
        )
        assert c["plan"].trip_type == "solo"
        assert c["plan"].style == "cinematic"
        assert c["plan"].focus == "temples"
        assert c["plan"].language == "cn"
        assert c["plan"].target_duration == 180

    @pytest.mark.parametrize(
        "res_flag, expected_w, expected_h, expected_fps",
        [
            ("4k60", 3840, 2160, 60),
            ("2560x1440x60", 2560, 1440, 60),
        ],
        ids=["preset", "custom"],
    )
    def test_assemble_config_resolution(
        self, runner, res_flag, expected_w, expected_h, expected_fps
    ):
        c = self._run_full(runner, ["-r", res_flag])
        assert c["assemble"].w == expected_w
        assert c["assemble"].h == expected_h
        assert c["assemble"].fps == expected_fps

    @pytest.mark.parametrize(
        "extra_args, field, expected",
        [
            (["--bitrate", "2.0"], "quality", 2.0),
            ([], "quality", 1.0),
        ],
        ids=["explicit_bitrate", "default_bitrate"],
    )
    def test_assemble_config_bitrate(self, runner, extra_args, field, expected):
        c = self._run_full(runner, extra_args)
        assert getattr(c["assemble"], field) == expected

    @pytest.mark.parametrize(
        "extra_args, expected",
        [
            (["--force"], True),
            ([], False),
        ],
    )
    def test_prepare_config_force(self, runner, extra_args, expected):
        c = self._run_full(runner, extra_args)
        assert c["prepare"].force is expected

    def test_stages_with_music(self, runner):
        c = self._run_full(runner)
        assert "generate_music" in c["stages"]

    def test_stages_without_music(self, runner):
        c = self._run_full(runner, ["--music", "none"])
        assert "generate_music" not in c["stages"]

    def test_all_stages_present(self, runner):
        c = self._run_full(runner)
        assert c["stages"] == ["fetch", "prepare", "plan", "generate_music", "assemble"]


class TestPlanCommandWiring:
    def test_plan_basic(self, runner):
        c = _capture_pipeline_call(
            ["plan", "-n", "test", "--duration", "120", "--model", "fast"],
            runner,
        )
        assert c["plan"].model == "gemini-3.1-flash-lite-preview"
        assert c["plan"].thinking_level == "LOW"
        assert "plan" in c["stages"]

    def test_plan_requires_model(self, runner):
        result = runner.invoke(cli, ["plan", "-n", "test", "--duration", "60"])
        assert result.exit_code != 0
        assert "model" in result.output.lower()


class TestAssembleCommandWiring:
    def test_assemble_basic(self, runner):
        c = _capture_pipeline_call(
            ["assemble", "-n", "test", "-r", "720p30"],
            runner,
        )
        assert c["assemble"].w == 1280
        assert c["assemble"].h == 720
        assert c["assemble"].fps == 30
        assert c["assemble"].quality == 1.0

    def test_assemble_with_bitrate(self, runner):
        c = _capture_pipeline_call(
            ["assemble", "-n", "test", "-r", "4k60", "--bitrate", "1.5"],
            runner,
        )
        assert c["assemble"].quality == 1.5


# ---------------------------------------------------------------------------
# Required parameter tests (missing args should fail)
# ---------------------------------------------------------------------------


class TestRequiredParams:
    @pytest.mark.parametrize(
        "omit_flags",
        [
            (["-n", "t"]),
            (["-p", "."]),
            (["--duration", "60"]),
            (["--model", "fast"]),
            (["-r", "720p30"]),
        ],
        ids=["name", "path", "duration", "model", "resolution"],
    )
    def test_full_missing_required_param(self, runner, omit_flags):
        all_args = [
            "full",
            "-n",
            "t",
            "-p",
            ".",
            "--duration",
            "60",
            "--model",
            "fast",
            "-r",
            "720p30",
        ]
        args = [a for a in all_args if a not in omit_flags]
        result = runner.invoke(cli, args)
        assert result.exit_code != 0

    def test_full_missing_path(self, runner):
        with patch("pipeline.cli._commands._run_pipeline"):
            result = runner.invoke(
                cli,
                [
                    "full",
                    "-n",
                    "t",
                    "--duration",
                    "60",
                    "--model",
                    "fast",
                    "-r",
                    "720p30",
                ],
            )
        assert result.exit_code != 0


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
                "id": 1,
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
                        "id": 1,
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

        _run_prepare(pc)  # should not raise AttributeError
