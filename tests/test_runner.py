"""Tests for pipeline.cli._runner — pipeline orchestration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pipeline.cli._display import _PipelineDisplay, _build_headline_from_args, _progress_cb


# ---------------------------------------------------------------------------
# _build_headline_from_args
# ---------------------------------------------------------------------------


class TestBuildHeadline:
    def test_with_plan_config(self):
        plan = MagicMock()
        plan.target_duration = 180
        plan.style = "cinematic"
        plan.trip_type = "family"
        result = _build_headline_from_args(["plan"], plan)
        assert "180s" in result
        assert "cinematic" in result
        assert "family vlog" in result

    def test_without_plan(self):
        result = _build_headline_from_args(["prepare", "plan"], None)
        assert "prepare" in result
        assert "plan" in result

    def test_plan_no_duration(self):
        plan = MagicMock()
        plan.target_duration = 0
        plan.style = "upbeat"
        plan.trip_type = ""
        result = _build_headline_from_args(["plan"], plan)
        assert "upbeat" in result

    def test_empty_stages_no_plan(self):
        result = _build_headline_from_args([], None)
        assert result == ""


# ---------------------------------------------------------------------------
# _progress_cb
# ---------------------------------------------------------------------------


class TestProgressCb:
    def _make_display(self, stages=None):
        if stages is None:
            stages = ["plan"]
        d = _PipelineDisplay.__new__(_PipelineDisplay)
        d._stages = stages
        d._stage_data = {
            s: {"state": "running", "label": "", "current": 0, "total": 0, "subs": {}, "sub_order": []}
            for s in stages
        }
        d._live = None
        d.api_cost = 0.0
        d.output_file = ""
        return d

    def test_status_only(self):
        logger = logging.getLogger("test_cb_status")
        display = self._make_display()
        cb = _progress_cb(logger, display, "plan", 0)
        cb(0, 0, "calling Gemini API...")
        assert display._stage_data["plan"]["label"] == "calling Gemini API..."

    def test_progress_update(self):
        logger = logging.getLogger("test_cb_progress")
        display = self._make_display()
        cb = _progress_cb(logger, display, "plan", 0)
        cb(5, 10, "processing")
        # With <=5 distinct names → sub-stage progress
        assert display._stage_data["plan"]["subs"].get("processing") is not None

    def test_api_cost_accumulated(self):
        logger = logging.getLogger("test_cb_cost")
        display = self._make_display()
        cb = _progress_cb(logger, display, "plan", 0)
        cb(0, 0, "~$0.15 (1000 prompt, 500 content)")
        assert display.api_cost == pytest.approx(0.15)

    def test_api_cost_malformed_ignored(self):
        logger = logging.getLogger("test_cb_cost_bad")
        display = self._make_display()
        cb = _progress_cb(logger, display, "plan", 0)
        cb(0, 0, "~$not_a_number")
        assert display.api_cost == 0.0


# ---------------------------------------------------------------------------
# _PipelineDisplay state transitions
# ---------------------------------------------------------------------------


class TestPipelineDisplayStates:
    def _make_display(self, stages):
        d = _PipelineDisplay.__new__(_PipelineDisplay)
        d._run_name = "test"
        d._headline = "test"
        d._stages = stages
        d._t_start = 0
        d._stage_t_start = {}
        d.output_file = ""
        d.api_cost = 0.0
        d._stage_data = {}
        d._current_stage = None
        d._live = None
        d._tick = 0
        for s in stages:
            d._stage_data[s] = {
                "state": "pending",
                "label": "",
                "current": 0,
                "total": 0,
                "dur": 0,
                "subs": {},
                "sub_order": [],
            }
        return d

    def test_start_sets_running(self):
        d = self._make_display(["fetch"])
        d.start("fetch")
        assert d._stage_data["fetch"]["state"] == "running"
        assert d._current_stage == "fetch"

    def test_done_sets_done(self):
        d = self._make_display(["fetch"])
        d.start("fetch")
        d.done("fetch", "100 items", 2.5)
        assert d._stage_data["fetch"]["state"] == "done"
        assert d._stage_data["fetch"]["dur"] == 2.5
        assert "100 items" in d._stage_data["fetch"]["detail"]

    def test_fail_sets_failed(self):
        d = self._make_display(["plan"])
        d.start("plan")
        d.fail("plan", "API error")
        assert d._stage_data["plan"]["state"] == "failed"
        assert "API error" in d._stage_data["plan"]["detail"]

    def test_update_simple_progress(self):
        d = self._make_display(["prepare"])
        d.start("prepare")
        d.update("prepare", "5/10")
        assert d._stage_data["prepare"]["current"] == 5
        assert d._stage_data["prepare"]["total"] == 10

    def test_update_sub_stage(self):
        d = self._make_display(["assemble"])
        d.start("assemble")
        d.update("assemble", "render segments:3/6")
        assert "render segments" in d._stage_data["assemble"]["subs"]
        sub = d._stage_data["assemble"]["subs"]["render segments"]
        assert sub["current"] == 3
        assert sub["total"] == 6

    def test_update_label(self):
        d = self._make_display(["plan"])
        d.start("plan")
        d.update("plan", "calling Gemini...")
        assert d._stage_data["plan"]["label"] == "calling Gemini..."

    def test_stop_is_safe(self):
        d = self._make_display(["fetch"])
        d.stop()  # should not raise

    def test_update_nonexistent_stage(self):
        d = self._make_display(["fetch"])
        d.update("nonexistent", "5/10")  # should not raise

    def test_done_cached(self):
        d = self._make_display(["fetch"])
        d.start("fetch")
        d.done("fetch", "100 items", 0.1)  # < 0.5 → cached
        assert "(cached)" in d._stage_data["fetch"]["detail"]


# ---------------------------------------------------------------------------
# _run_pipeline integration (mocked stages)
# ---------------------------------------------------------------------------


class TestRunPipeline:
    def test_runs_stages_in_order(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKSPACE", str(tmp_path / "workspace"))
        order = []

        def _mock_fetch(pc):
            order.append("fetch")

        def _mock_prepare(pc):
            order.append("prepare")

        with (
            patch("pipeline.cli._runner._STAGE_RUNNERS", {
                "fetch": _mock_fetch,
                "prepare": _mock_prepare,
            }),
            patch("pipeline.cli._runner._PipelineDisplay") as MockDisplay,
            patch("pipeline.cli._runner._setup_logging") as mock_log,
        ):
            mock_display = MagicMock()
            mock_display._live = None
            mock_display._stage_data = {
                "fetch": {"state": "pending"},
                "prepare": {"state": "pending"},
            }
            MockDisplay.return_value = mock_display
            mock_log.return_value = logging.getLogger("test_pipeline")

            from pipeline.cli._runner import _run_pipeline

            _run_pipeline("test_run", stages=["fetch", "prepare"])

        assert order == ["fetch", "prepare"]
