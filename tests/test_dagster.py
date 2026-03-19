"""Tests for Dagster asset definitions and IOManager."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import dagster as dg
import pytest


def _make_workspace(tmp_path: Path) -> str:
    """Create a minimal workspace directory structure."""
    for d in ("media", "keyframes", "clips", "output"):
        (tmp_path / d).mkdir()
    return str(tmp_path)


# ---------------------------------------------------------------------------
# WorkspaceIOManager tests
# ---------------------------------------------------------------------------

class TestWorkspaceIOManager:
    def test_workspace_path(self, tmp_path):
        from pipeline.definitions import WorkspaceIOManager

        mgr = WorkspaceIOManager(base_dir=str(tmp_path), run_name="myrun")
        assert mgr.workspace_path == str(tmp_path / "runs" / "myrun")

    def test_config_property(self, tmp_path):
        from pipeline.definitions import WorkspaceIOManager

        mgr = WorkspaceIOManager(base_dir=str(tmp_path), run_name="myrun")
        cfg = mgr.config
        assert cfg.workspace == Path(tmp_path / "runs" / "myrun")

    def test_load_input_returns_workspace(self, tmp_path):
        from pipeline.definitions import WorkspaceIOManager

        mgr = WorkspaceIOManager(base_dir=str(tmp_path), run_name="myrun")
        ctx = MagicMock()
        result = mgr.load_input(ctx)
        assert result == str(tmp_path / "runs" / "myrun")


# ---------------------------------------------------------------------------
# Asset dependency graph tests
# ---------------------------------------------------------------------------

class TestAssetGraph:
    def test_asset_keys_present(self):
        from pipeline.definitions import defs

        specs = defs.resolve_all_asset_specs()
        keys = {s.key.path[-1] for s in specs}
        assert keys == {"fetch_media", "preprocess", "analyze", "plan", "generate_music", "assemble"}

    def test_plan_depends_on_analyze(self):
        from pipeline.definitions import defs

        specs = {s.key.path[-1]: s for s in defs.resolve_all_asset_specs()}
        plan_spec = specs["plan"]
        dep_keys = {d.asset_key.path[-1] for d in plan_spec.deps}
        assert "analyze" in dep_keys


# ---------------------------------------------------------------------------
# Asset function tests (mocked)
# ---------------------------------------------------------------------------

class TestFetchMediaAsset:
    def test_returns_manifest_path(self, tmp_path):
        from pipeline.definitions import fetch_media, FetchConfig, WorkspaceIOManager

        ws = _make_workspace(tmp_path)
        (tmp_path / "manifest.json").write_text("[]")

        mock_data = [{"id": 1, "filename": "test.jpg"}]

        with patch("pipeline.definitions.do_fetch", return_value=mock_data):
            result = dg.materialize(
                [fetch_media],
                resources={"io_manager": WorkspaceIOManager(base_dir=ws, run_name="test")},
                run_config=dg.RunConfig(
                    ops={"fetch_media": FetchConfig(from_date="2025-01-01", to_date="2025-01-31")},
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
                    "local_path": str(tmp_path / "media" / "test.jpg"),
                    "family_count": 0,
                }
            ],
        }
        (tmp_path / "preprocessed.json").write_text(json.dumps(preprocessed))

        from PIL import Image
        img = Image.new("RGB", (100, 100), "blue")
        img.save(tmp_path / "media" / "test.jpg")

        callback_calls = []

        def on_progress(current, total, filename):
            callback_calls.append((current, total, filename))

        cfg = Config.load(ws)
        with patch("pipeline.analyze._analyze_image", return_value={"quality": 7}):
            analyze(cfg, progress_callback=on_progress)

        assert len(callback_calls) >= 1


@pytest.mark.integration
class TestAssembleProgressCallback:
    def test_callback_invoked(self, tmp_path):
        """assemble() should invoke progress_callback when provided."""
        from pipeline.assemble import assemble
        from pipeline.config import Config
        from pipeline.edl import EDL, EditItem, Segment

        ws = _make_workspace(tmp_path)

        from PIL import Image
        img = Image.new("RGB", (160, 90), "green")
        media_path = tmp_path / "media" / "test.jpg"
        img.save(media_path)

        edl_obj = EDL(
            title="Test", target_duration=5.0, resolution=(320, 180), fps=24,
            segments=[
                Segment(
                    name="test",
                    items=[
                        EditItem(
                            source_file=str(media_path), media_type="photo",
                            display_duration=2.0, effect="static",
                        ),
                    ],
                    transition="cut", transition_duration=0.0,
                ),
            ],
        )
        (tmp_path / "edl_v1.json").write_text(edl_obj.model_dump_json(indent=2))

        callback_calls = []
        cfg = Config.load(ws)
        assemble(cfg, version=99, progress_callback=lambda c, t, n: callback_calls.append((c, t, n)))

        assert len(callback_calls) >= 1
        assert (tmp_path / "output" / "vlog_v99.mp4").exists()
