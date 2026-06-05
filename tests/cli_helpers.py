"""Shared helpers for CLI tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import yaml
from click.testing import CliRunner

from pipeline.cli import cli

CURRENT_COMMANDS = ("run", "new", "edit", "workspace", "config")
REMOVED_COMMANDS = ("full", "prepare", "plan", "assemble", "init")

FULL_RUN_CONFIG = """\
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


def write_run_config(
    tmp_path: Path,
    text: str = FULL_RUN_CONFIG,
    *,
    filename: str = "run.yaml",
) -> str:
    """Write a YAML config file and return its string path for Click args."""
    cfg = tmp_path / filename
    cfg.write_text(text, encoding="utf-8")
    return str(cfg)


def write_default_run_config(
    tmp_path: Path,
    *,
    run_name: str = "trip",
    text: str = FULL_RUN_CONFIG,
) -> Path:
    """Write workspace/runs/{run_name}/run.yaml under *tmp_path*."""
    run_dir = tmp_path / "workspace" / "runs" / run_name
    run_dir.mkdir(parents=True)
    cfg_path = run_dir / "run.yaml"
    cfg_path.write_text(text, encoding="utf-8")
    return cfg_path


def read_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file with a typed dict result."""
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def capture_pipeline_run(
    runner: CliRunner,
    cfg_file: str | None,
    *,
    run_name: str = "test-run",
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """Invoke `reelsmith run` while capturing the _run_pipeline call."""
    captured: dict[str, Any] = {}

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
        captured.update(
            {
                "run_name": run_name,
                "stages": stages,
                "source_dir": source_dir,
                "prepare": prepare,
                "plan": plan,
                "assemble": assemble,
                "cli_params": cli_params,
                "cli_defaults": cli_defaults,
            }
        )

    args = ["run", run_name]
    if cfg_file is not None:
        args.extend(["-c", cfg_file])
    if extra_args:
        args.extend(extra_args)

    with patch("pipeline.cli._commands._run_pipeline", side_effect=mock_pipeline):
        result = runner.invoke(cli, args, catch_exceptions=False)

    assert result.exit_code == 0, result.output
    return captured
