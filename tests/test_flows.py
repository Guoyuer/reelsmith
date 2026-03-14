"""Tests for Prefect workflow integration."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from prefect.testing.utilities import prefect_test_harness


@pytest.fixture(autouse=True, scope="module")
def prefect_test_env():
    """Use Prefect's in-memory test harness (no server needed)."""
    with prefect_test_harness():
        yield


def _make_workspace(tmp_path: Path) -> str:
    """Create a minimal workspace directory structure."""
    for d in ("raw", "keyframes", "clips", "output"):
        (tmp_path / d).mkdir()
    return str(tmp_path)


class TestFetchTask:
    def test_returns_manifest_path(self, tmp_path):
        from pipeline.flows import fetch_task

        manifest_data = [{"id": 1, "filename": "test.jpg"}]
        ws = _make_workspace(tmp_path)
        (tmp_path / "manifest.json").write_text(json.dumps(manifest_data))

        with patch("pipeline.fetch.fetch", return_value=manifest_data):
            result = fetch_task.fn(
                ws, from_date="2025-01-01", to_date="2025-01-31",
            )

        assert result == str(tmp_path / "manifest.json")


class TestPreprocessTask:
    def test_returns_preprocessed_path(self, tmp_path):
        from pipeline.flows import preprocess_task

        ws = _make_workspace(tmp_path)
        (tmp_path / "manifest.json").write_text("[]")
        (tmp_path / "preprocessed.json").write_text("{}")

        mock_result = {
            "total_items": 10, "selected_items": 8,
            "tier_counts": {"A": 3, "B": 2, "C": 3},
            "timeline": [{"date": "2025-01-01"}],
        }

        with patch("pipeline.preprocess.preprocess", return_value=mock_result):
            result = preprocess_task.fn(
                ws, str(tmp_path / "manifest.json"),
            )

        assert result == str(tmp_path / "preprocessed.json")


class TestAnalyzeTask:
    def test_returns_analysis_path(self, tmp_path):
        from pipeline.flows import analyze_task

        ws = _make_workspace(tmp_path)
        (tmp_path / "preprocessed.json").write_text("{}")
        (tmp_path / "analysis.json").write_text("[]")

        mock_results = [
            {"id": 1, "vision": {"quality": 8}},
            {"id": 2, "vision": {"quality": 5}},
        ]

        with patch("pipeline.analyze.analyze", return_value=mock_results):
            result = analyze_task.fn(
                ws, str(tmp_path / "preprocessed.json"),
            )

        assert result == str(tmp_path / "analysis.json")

    def test_progress_callback_invoked(self, tmp_path):
        """analyze() should invoke progress_callback when provided."""
        from pipeline.analyze import analyze

        ws = _make_workspace(tmp_path)
        # Create minimal preprocessed.json with one item
        preprocessed = {
            "family_names": [],
            "items": [
                {
                    "id": 999,
                    "tier": "C",
                    "filename": "test.jpg",
                    "local_path": str(tmp_path / "raw" / "test.jpg"),
                    "family_count": 0,
                }
            ],
        }
        (tmp_path / "preprocessed.json").write_text(json.dumps(preprocessed))

        # Create a tiny test image
        from PIL import Image
        img = Image.new("RGB", (100, 100), "blue")
        img.save(tmp_path / "raw" / "test.jpg")

        callback_calls = []

        def on_progress(current, total, filename):
            callback_calls.append((current, total, filename))

        from pipeline.config import Config
        cfg = Config.load(ws)

        # Mock the vision API call
        with patch("pipeline.analyze._analyze_image", return_value={"quality": 7}):
            analyze(cfg, progress_callback=on_progress)

        assert len(callback_calls) >= 1
        assert callback_calls[-1][0] == callback_calls[-1][1]  # final: current == total


class TestPlanTask:
    def test_returns_edl_path(self, tmp_path):
        from pipeline.flows import plan_task

        ws = _make_workspace(tmp_path)
        (tmp_path / "preprocessed.json").write_text("{}")
        (tmp_path / "analysis.json").write_text("[]")

        mock_edl = MagicMock()
        mock_edl.segments = []
        mock_edl.title = "Test"
        mock_edl.all_items.return_value = []
        mock_edl.estimated_duration.return_value = 120.0
        mock_edl.model_dump_json.return_value = "{}"

        with patch("pipeline.plan.plan", return_value=mock_edl):
            result = plan_task.fn(
                ws,
                str(tmp_path / "preprocessed.json"),
                str(tmp_path / "analysis.json"),
            )

        assert result == str(tmp_path / "edl.json")


class TestAssembleTask:
    def test_returns_video_path(self, tmp_path):
        from pipeline.flows import assemble_task

        ws = _make_workspace(tmp_path)
        (tmp_path / "edl.json").write_text("{}")

        output = tmp_path / "output" / "vlog_v1.mp4"

        with patch("pipeline.assemble.assemble", return_value=output):
            result = assemble_task.fn(
                ws, str(tmp_path / "edl.json"), version=1,
            )

        assert result == str(output)


class TestFullPipelineFlow:
    def test_calls_stages_in_order(self, tmp_path):
        from pipeline.flows import vlog_pipeline_flow

        ws = _make_workspace(tmp_path)
        call_order = []

        def make_tracker(name, return_val):
            def fn(*args, **kwargs):
                call_order.append(name)
                return return_val
            return fn

        manifest = str(tmp_path / "manifest.json")
        preprocessed = str(tmp_path / "preprocessed.json")
        analysis = str(tmp_path / "analysis.json")
        edl = str(tmp_path / "edl.json")
        video = str(tmp_path / "output" / "vlog_v1.mp4")

        with (
            patch("pipeline.flows.fetch_task", side_effect=make_tracker("fetch", manifest)),
            patch("pipeline.flows.preprocess_task", side_effect=make_tracker("preprocess", preprocessed)),
            patch("pipeline.flows.analyze_task", side_effect=make_tracker("analyze", analysis)),
            patch("pipeline.flows.plan_task", side_effect=make_tracker("plan", edl)),
            patch("pipeline.flows.assemble_task", side_effect=make_tracker("assemble", video)),
        ):
            vlog_pipeline_flow(
                ws,
                start_from="fetch",
                from_date="2025-01-01", to_date="2025-01-31",
                critique_rounds=0,
            )

        assert call_order == ["fetch", "preprocess", "analyze", "plan", "assemble"]

    def test_start_from_skips_earlier_stages(self, tmp_path):
        from pipeline.flows import vlog_pipeline_flow

        ws = _make_workspace(tmp_path)
        call_order = []

        def make_tracker(name, return_val):
            def fn(*args, **kwargs):
                call_order.append(name)
                return return_val
            return fn

        # Create upstream outputs so start_from="plan" works
        (tmp_path / "manifest.json").write_text("[]")
        (tmp_path / "preprocessed.json").write_text("{}")
        (tmp_path / "analysis.json").write_text("[]")

        edl = str(tmp_path / "edl.json")
        video = str(tmp_path / "output" / "vlog_v1.mp4")

        with (
            patch("pipeline.flows.fetch_task", side_effect=make_tracker("fetch", "")),
            patch("pipeline.flows.preprocess_task", side_effect=make_tracker("preprocess", "")),
            patch("pipeline.flows.analyze_task", side_effect=make_tracker("analyze", "")),
            patch("pipeline.flows.plan_task", side_effect=make_tracker("plan", edl)),
            patch("pipeline.flows.assemble_task", side_effect=make_tracker("assemble", video)),
        ):
            vlog_pipeline_flow(
                ws,
                start_from="plan",
                critique_rounds=0,
            )

        assert call_order == ["plan", "assemble"]


class TestIterateFlow:
    def test_creates_n_critique_rounds(self, tmp_path):
        from pipeline.flows import iterate_flow

        ws = _make_workspace(tmp_path)
        round_calls = []

        def mock_critique(workspace, *, round_num, style="upbeat"):
            round_calls.append(round_num)
            return str(tmp_path / "output" / f"vlog_v{round_num + 1}.mp4")

        with (
            patch("pipeline.flows.critique_round_task", side_effect=mock_critique),
            patch("pipeline.iterate._find_latest_version", return_value=1),
        ):
            iterate_flow(ws, max_rounds=3)

        assert round_calls == [1, 2, 3]


class TestAssembleProgressCallback:
    def test_callback_invoked(self, tmp_path):
        """assemble() should invoke progress_callback when provided."""
        from pipeline.assemble import assemble
        from pipeline.config import Config
        from pipeline.edl import EDL, EditItem, Segment

        ws = _make_workspace(tmp_path)

        # Create a tiny test image
        from PIL import Image
        img = Image.new("RGB", (160, 90), "green")
        raw_path = tmp_path / "raw" / "test.jpg"
        img.save(raw_path)

        # Create a minimal EDL
        edl = EDL(
            title="Test",
            target_duration=5.0,
            resolution=(320, 180),
            fps=24,
            segments=[
                Segment(
                    name="test",
                    items=[
                        EditItem(
                            source_file=str(raw_path),
                            media_type="photo",
                            display_duration=2.0,
                            effect="static",
                        ),
                    ],
                    transition="cut",
                    transition_duration=0.0,
                ),
            ],
        )
        (tmp_path / "edl.json").write_text(edl.model_dump_json(indent=2))

        callback_calls = []

        def on_progress(current, total, clip_name):
            callback_calls.append((current, total, clip_name))

        cfg = Config.load(ws)
        assemble(cfg, version=99, progress_callback=on_progress)

        assert len(callback_calls) >= 1
        assert (tmp_path / "output" / "vlog_v99.mp4").exists()
