"""Tests for pipeline stage prerequisite checks (FileNotFoundError messages)."""

from __future__ import annotations

import pytest

from pipeline.config import Config


class TestPreparePrerequisites:
    def test_missing_manifest_raises(self, tmp_path):
        from pipeline.prepare import PrepareConfig, prepare

        cfg = Config(workspace=tmp_path / "runs" / "test")
        cfg.ensure_dirs()
        with pytest.raises(FileNotFoundError, match="Manifest not found"):
            prepare(cfg, PrepareConfig())


class TestPlanPrerequisites:
    def test_missing_manifest_raises(self, tmp_path):
        from pipeline.plan import PlanConfig, plan

        cfg = Config(workspace=tmp_path / "runs" / "test")
        cfg.ensure_dirs()
        with pytest.raises(FileNotFoundError, match="Manifest not found"):
            plan(cfg, PlanConfig(target_duration=60))


class TestAssemblePrerequisites:
    def test_missing_edl_raises(self, tmp_path):
        from pipeline.assemble import AssembleConfig, assemble

        cfg = Config(workspace=tmp_path / "runs" / "test")
        cfg.ensure_dirs()
        ac = AssembleConfig(w=1920, h=1080, fps=30, version=1)
        with pytest.raises(FileNotFoundError):
            assemble(cfg, ac)
