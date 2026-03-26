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

    def test_all_presets_resolve(self):
        """Every preset key must return a valid (model, thinking) tuple."""
        for name, (model, thinking) in _PLANNING_PRESETS.items():
            resolved = _resolve_planning(name)
            assert resolved == (model, thinking), f"Preset '{name}' mismatch"


# ---------------------------------------------------------------------------
# Resolution preset tests
# ---------------------------------------------------------------------------


class TestResolutionPresets:
    def test_all_presets_are_even(self):
        """Width and height must be even for FFmpeg."""
        for name, (w, h, fps) in _RESOLUTION_PRESETS.items():
            assert w % 2 == 0, f"{name}: width {w} is odd"
            assert h % 2 == 0, f"{name}: height {h} is odd"
            assert fps > 0, f"{name}: fps {fps} <= 0"

    def test_4k60(self):
        assert _RESOLUTION_PRESETS["4k60"] == (3840, 2160, 60)

    def test_1080p30(self):
        assert _RESOLUTION_PRESETS["1080p30"] == (1920, 1080, 30)

    def test_720p30(self):
        assert _RESOLUTION_PRESETS["720p30"] == (1280, 720, 30)


# ---------------------------------------------------------------------------
# CLI → Config wiring tests (mock _run_pipeline, verify Config objects)
# ---------------------------------------------------------------------------


class TestFullCommandWiring:
    """Verify 'full' command correctly constructs all Config objects."""

    @pytest.fixture
    def runner(self):
        return CliRunner()

    def _run_full(self, runner, extra_args=None):
        """Run 'full' command with minimal required args, capturing the _run_pipeline call."""
        base_args = [
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
            cli_defaults=None,
        ):
            captured["run_name"] = run_name
            captured["stages"] = stages
            captured["fetch"] = fetch
            captured["prepare"] = prepare
            captured["plan"] = plan
            captured["assemble"] = assemble

        with patch("pipeline.cli._commands._run_pipeline", side_effect=mock_pipeline):
            result = runner.invoke(cli, base_args, catch_exceptions=False)
        assert result.exit_code == 0, f"CLI failed: {result.output}"
        return captured

    def test_plan_config_model_resolved(self, runner):
        c = self._run_full(runner)
        assert c["plan"].model == "gemini-3-flash-preview"
        assert c["plan"].thinking_level == "HIGH"

    def test_plan_config_quality_preset(self, runner):
        c = self._run_full(runner, ["--model", "quality"])
        assert c["plan"].model == "gemini-3.1-pro-preview"
        assert c["plan"].thinking_level == "HIGH"

    def test_plan_config_fast_preset(self, runner):
        c = self._run_full(runner, ["--model", "fast"])
        assert c["plan"].model == "gemini-3.1-flash-lite-preview"
        assert c["plan"].thinking_level == "LOW"

    def test_plan_config_raw_model(self, runner):
        c = self._run_full(runner, ["--model", "gemini-2.5-flash:medium"])
        assert c["plan"].model == "gemini-2.5-flash"
        assert c["plan"].thinking_level == "MEDIUM"

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

    def test_assemble_config_resolution(self, runner):
        c = self._run_full(runner, ["-r", "4k60"])
        assert c["assemble"].w == 3840
        assert c["assemble"].h == 2160
        assert c["assemble"].fps == 60

    def test_assemble_config_bitrate(self, runner):
        c = self._run_full(runner, ["--bitrate", "2.0"])
        assert c["assemble"].quality == 2.0

    def test_assemble_config_default_bitrate(self, runner):
        c = self._run_full(runner)
        assert c["assemble"].quality == 1.0

    def test_assemble_config_custom_resolution(self, runner):
        c = self._run_full(runner, ["-r", "2560x1440x60"])
        assert c["assemble"].w == 2560
        assert c["assemble"].h == 1440
        assert c["assemble"].fps == 60

    def test_prepare_config_force(self, runner):
        c = self._run_full(runner, ["--force"])
        assert c["prepare"].force is True

    def test_prepare_config_no_force(self, runner):
        c = self._run_full(runner)
        assert c["prepare"].force is False

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
    """Verify 'plan' command correctly constructs PlanConfig."""

    @pytest.fixture
    def runner(self):
        return CliRunner()

    def test_plan_basic(self, runner):
        captured = {}

        def mock_pipeline(run_name, *, stages, **kwargs):
            captured.update(kwargs)
            captured["stages"] = stages

        with patch("pipeline.cli._commands._run_pipeline", side_effect=mock_pipeline):
            result = runner.invoke(
                cli,
                [
                    "plan",
                    "-n",
                    "test",
                    "--duration",
                    "120",
                    "--model",
                    "fast",
                ],
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output
        assert captured["plan"].model == "gemini-3.1-flash-lite-preview"
        assert captured["plan"].thinking_level == "LOW"
        assert "plan" in captured["stages"]

    def test_plan_requires_model(self, runner):
        result = runner.invoke(cli, ["plan", "-n", "test", "--duration", "60"])
        assert result.exit_code != 0
        assert "model" in result.output.lower()


class TestAssembleCommandWiring:
    """Verify 'assemble' command correctly constructs AssembleConfig."""

    @pytest.fixture
    def runner(self):
        return CliRunner()

    def test_assemble_basic(self, runner):
        captured = {}

        def mock_pipeline(run_name, *, stages, **kwargs):
            captured.update(kwargs)

        with patch("pipeline.cli._commands._run_pipeline", side_effect=mock_pipeline):
            result = runner.invoke(
                cli,
                [
                    "assemble",
                    "-n",
                    "test",
                    "-r",
                    "720p30",
                ],
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output
        assert captured["assemble"].w == 1280
        assert captured["assemble"].h == 720
        assert captured["assemble"].fps == 30
        assert captured["assemble"].quality == 1.0

    def test_assemble_with_bitrate(self, runner):
        captured = {}

        def mock_pipeline(run_name, *, stages, **kwargs):
            captured.update(kwargs)

        with patch("pipeline.cli._commands._run_pipeline", side_effect=mock_pipeline):
            result = runner.invoke(
                cli,
                [
                    "assemble",
                    "-n",
                    "test",
                    "-r",
                    "4k60",
                    "--bitrate",
                    "1.5",
                ],
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output
        assert captured["assemble"].quality == 1.5


# ---------------------------------------------------------------------------
# Required parameter tests (missing args should fail)
# ---------------------------------------------------------------------------


class TestRequiredParams:
    """Verify that missing required args cause non-zero exit."""

    ALL_ARGS = [
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

    @pytest.fixture
    def runner(self):
        return CliRunner()

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
        args = list(self.ALL_ARGS)
        for flag in omit_flags:
            if flag in args:
                args.remove(flag)
        result = runner.invoke(cli, args)
        assert result.exit_code != 0

    def test_full_missing_path(self, runner):
        """--path is required and omitting it should fail."""
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
        """_run_prepare should use load_analysis(), not cfg.analysis_path."""
        from unittest.mock import MagicMock

        from pipeline.config import Config
        from pipeline.prepare import PrepareConfig

        cfg = Config(workspace=tmp_path)
        cfg.ensure_dirs()

        # Config must NOT have analysis_path
        assert not hasattr(cfg, "analysis_path"), "Config still has analysis_path"

        # Create manifest + per-item cache so prepare can detect cached state
        import json

        manifest = [
            {
                "id": 1,
                "filename": "p.jpg",
                "local_path": str(tmp_path / "p.jpg"),
                "taken_iso": "2025-01-01T00:00:00",
                "takentime": 1700000000,
            }
        ]
        cfg.manifest_path.write_text(json.dumps(manifest))
        cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        (cfg.cache_dir / "1.json").write_text(json.dumps({"thumbnail_path": "/t.jpg"}))

        # Build a minimal _PipelineContext
        from pipeline.cli import _PipelineDisplay
        from pipeline.cli._runner import _run_prepare

        display = MagicMock(spec=_PipelineDisplay)
        pc = MagicMock()
        pc.cfg = cfg
        pc.prepare = PrepareConfig()
        pc.display = display
        pc.logger = MagicMock()

        # Should not raise AttributeError
        _run_prepare(pc)
