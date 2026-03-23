"""Tests for pipeline stage prerequisite checks (FileNotFoundError messages)."""

from __future__ import annotations

import pytest

from pipeline.config import Config
from pipeline.prepare import PrepareConfig


class TestPreparePrerequisites:
    def test_missing_manifest_raises(self, tmp_path):
        from pipeline.prepare import prepare

        cfg = Config(workspace=tmp_path)
        cfg.ensure_dirs()
        # Don't create manifest.json
        with pytest.raises(FileNotFoundError, match="Manifest not found"):
            prepare(cfg, PrepareConfig())


class TestPlanPrerequisites:
    def test_missing_preprocessed_raises(self, tmp_path):
        from pipeline.plan import PlanConfig, plan

        cfg = Config(workspace=tmp_path)
        cfg.ensure_dirs()
        # Don't create preprocessed.json or analysis.json
        with pytest.raises(FileNotFoundError, match="Preprocessed data not found"):
            plan(cfg, PlanConfig(target_duration=60))

    def test_missing_analysis_raises(self, tmp_path):
        from pipeline.plan import PlanConfig, plan

        cfg = Config(workspace=tmp_path)
        cfg.ensure_dirs()
        # Create preprocessed.json but not analysis.json
        cfg.preprocessed_path.write_text('{"timeline": []}')
        with pytest.raises(FileNotFoundError, match="Analysis data not found"):
            plan(cfg, PlanConfig(target_duration=60))


class TestAssemblePrerequisites:
    def test_missing_edl_raises(self, tmp_path):
        from pipeline.assemble import AssembleConfig, assemble

        cfg = Config(workspace=tmp_path)
        cfg.ensure_dirs()
        ac = AssembleConfig(w=1920, h=1080, fps=30, version=1)
        with pytest.raises(FileNotFoundError, match="EDL not found"):
            assemble(cfg, ac)
