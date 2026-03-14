"""Tests for Dagster asset definitions and IOManager."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import dagster as dg
import pytest


def _make_workspace(tmp_path: Path) -> str:
    """Create a minimal workspace directory structure."""
    for d in ("raw", "keyframes", "clips", "output"):
        (tmp_path / d).mkdir()
    return str(tmp_path)


# ---------------------------------------------------------------------------
# WorkspaceIOManager tests
# ---------------------------------------------------------------------------

class TestWorkspaceIOManager:
    def test_output_exists_true_when_file_exists(self, tmp_path):
        from pipeline.definitions import _output_exists

        ws = _make_workspace(tmp_path)
        (tmp_path / "manifest.json").write_text("[]")
        assert _output_exists(ws, "manifest") is True

    def test_output_exists_false_when_file_missing(self, tmp_path):
        from pipeline.definitions import _output_exists

        ws = _make_workspace(tmp_path)
        assert _output_exists(ws, "manifest") is False

    def test_output_exists_vlog_video_glob(self, tmp_path):
        from pipeline.definitions import _output_exists

        ws = _make_workspace(tmp_path)
        assert _output_exists(ws, "vlog_video") is False

        (tmp_path / "output" / "vlog_v1.mp4").write_text("fake")
        assert _output_exists(ws, "vlog_video") is True

    def test_load_input_returns_path(self, tmp_path):
        from pipeline.definitions import WorkspaceIOManager

        ws = _make_workspace(tmp_path)
        run_dir = tmp_path / "runs" / "myrun"
        run_dir.mkdir(parents=True)
        (run_dir / "analysis.json").write_text("[]")

        mgr = WorkspaceIOManager(base_dir=str(tmp_path), run_name="myrun")
        ctx = MagicMock()
        ctx.upstream_output.asset_key.path = ["analysis"]
        result = mgr.load_input(ctx)
        assert result == str(run_dir / "analysis.json")

    def test_load_input_raises_when_missing(self, tmp_path):
        from pipeline.definitions import WorkspaceIOManager

        _make_workspace(tmp_path)
        mgr = WorkspaceIOManager(base_dir=str(tmp_path), run_name="myrun")
        ctx = MagicMock()
        ctx.upstream_output.asset_key.path = ["analysis"]

        with pytest.raises(FileNotFoundError):
            mgr.load_input(ctx)


# ---------------------------------------------------------------------------
# Asset dependency graph tests
# ---------------------------------------------------------------------------

class TestAssetGraph:
    def test_asset_keys_present(self):
        from pipeline.definitions import defs

        specs = defs.resolve_all_asset_specs()
        keys = {s.key.path[-1] for s in specs}
        assert keys == {"manifest", "preprocessed", "analysis", "edl", "vlog_video"}

    def test_edl_depends_on_preprocessed_and_analysis(self):
        from pipeline.definitions import defs

        specs = {s.key.path[-1]: s for s in defs.resolve_all_asset_specs()}
        edl_spec = specs["edl"]
        dep_keys = {d.asset_key.path[-1] for d in edl_spec.deps}
        assert "preprocessed" in dep_keys
        assert "analysis" in dep_keys


# ---------------------------------------------------------------------------
# Asset function tests (mocked)
# ---------------------------------------------------------------------------

class TestManifestAsset:
    def test_returns_manifest_path(self, tmp_path):
        from pipeline.definitions import manifest, FetchConfig, WorkspaceIOManager

        ws = _make_workspace(tmp_path)
        (tmp_path / "manifest.json").write_text("[]")

        mock_data = [{"id": 1, "filename": "test.jpg"}]

        with patch("pipeline.fetch.fetch", return_value=mock_data):
            result = dg.materialize(
                [manifest],
                resources={"io_manager": WorkspaceIOManager(workspace=ws)},
                run_config=dg.RunConfig(
                    ops={"manifest": FetchConfig(from_date="2025-01-01", to_date="2025-01-31")},
                ),
            )

        assert result.success


class TestAnalyzeProgressCallback:
    def test_callback_invoked(self, tmp_path):
        """analyze() should invoke progress_callback when provided."""
        from pipeline.analyze import analyze
        from pipeline.config import Config

        ws = _make_workspace(tmp_path)

        preprocessed = {
            "family_names": [],
            "items": [
                {
                    "id": 999, "tier": "C", "filename": "test.jpg",
                    "local_path": str(tmp_path / "raw" / "test.jpg"),
                    "family_count": 0,
                }
            ],
        }
        (tmp_path / "preprocessed.json").write_text(json.dumps(preprocessed))

        from PIL import Image
        img = Image.new("RGB", (100, 100), "blue")
        img.save(tmp_path / "raw" / "test.jpg")

        callback_calls = []

        def on_progress(current, total, filename):
            callback_calls.append((current, total, filename))

        cfg = Config.load(ws)
        with patch("pipeline.analyze._analyze_image", return_value={"quality": 7}):
            analyze(cfg, progress_callback=on_progress)

        assert len(callback_calls) >= 1


class TestAssembleProgressCallback:
    def test_callback_invoked(self, tmp_path):
        """assemble() should invoke progress_callback when provided."""
        from pipeline.assemble import assemble
        from pipeline.config import Config
        from pipeline.edl import EDL, EditItem, Segment

        ws = _make_workspace(tmp_path)

        from PIL import Image
        img = Image.new("RGB", (160, 90), "green")
        raw_path = tmp_path / "raw" / "test.jpg"
        img.save(raw_path)

        edl_obj = EDL(
            title="Test", target_duration=5.0, resolution=(320, 180), fps=24,
            segments=[
                Segment(
                    name="test",
                    items=[
                        EditItem(
                            source_file=str(raw_path), media_type="photo",
                            display_duration=2.0, effect="static",
                        ),
                    ],
                    transition="cut", transition_duration=0.0,
                ),
            ],
        )
        (tmp_path / "edl.json").write_text(edl_obj.model_dump_json(indent=2))

        callback_calls = []
        cfg = Config.load(ws)
        assemble(cfg, version=99, progress_callback=lambda c, t, n: callback_calls.append((c, t, n)))

        assert len(callback_calls) >= 1
        assert (tmp_path / "output" / "vlog_v99.mp4").exists()
