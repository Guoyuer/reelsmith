"""Unit tests for pipeline.assemble._assemble — AssembleJob properties and assemble() routing."""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.assemble._assemble import AssembleConfig, AssembleJob
from pipeline.assemble._encoder import RenderContext
from pipeline.config import Config
from pipeline.edl import EDL, EditItem, Segment


class TestAssembleJobProperties:
    """Test that AssembleJob derived properties work correctly."""

    def _make_job(self, tmp_path) -> AssembleJob:
        cfg = Config(workspace=tmp_path)
        cfg.ensure_dirs()
        edl = EDL(
            title="T",
            target_duration=60,
            segments=[
                Segment(name="S", items=[EditItem(source_file="a.jpg", media_type="photo", display_duration=4.0)], transition="cut")
            ],
        )
        ctx = RenderContext(w=1920, h=1080, fps=30)
        return AssembleJob(cfg=cfg, edl=edl, version=1, ctx=ctx)

    def test_w_h_fps_from_ctx(self, tmp_path):
        job = self._make_job(tmp_path)
        assert job.w == 1920
        assert job.h == 1080
        assert job.fps == 30

    def test_lang_from_edl(self, tmp_path):
        job = self._make_job(tmp_path)
        assert job.lang == "en"  # EDL default

    def test_res_label(self, tmp_path):
        job = self._make_job(tmp_path)
        assert job.res_label == "1080p30"

    def test_clips_dir(self, tmp_path):
        job = self._make_job(tmp_path)
        assert job.clips_dir == tmp_path / "clips"

    def test_output_dir(self, tmp_path):
        job = self._make_job(tmp_path)
        assert job.output_dir == tmp_path / "output"

    def test_output_path(self, tmp_path):
        job = self._make_job(tmp_path)
        assert job.output_path == tmp_path / "output" / "vlog_v1_1080p30.mp4"

    def test_different_resolution(self, tmp_path):
        cfg = Config(workspace=tmp_path)
        cfg.ensure_dirs()
        edl = EDL(title="T", target_duration=60, segments=[
            Segment(name="S", items=[EditItem(source_file="a.jpg", media_type="photo", display_duration=4.0)], transition="cut"),
        ])
        ctx = RenderContext(w=3840, h=2160, fps=60)
        job = AssembleJob(cfg=cfg, edl=edl, version=2, ctx=ctx)
        assert job.res_label == "2160p60"
        assert "vlog_v2_2160p60.mp4" in str(job.output_path)


class TestAssembleConfigToEdlLoading:
    """Test that assemble() loads the correct EDL version."""

    def test_loads_specified_version(self, tmp_path):
        cfg = Config(workspace=tmp_path)
        cfg.ensure_dirs()
        # Create edl_v1.json
        edl = EDL(
            title="V1",
            target_duration=60,
            segments=[Segment(name="S", items=[
                EditItem(source_file=str(tmp_path / "photo.jpg"), media_type="photo", display_duration=4.0),
            ], transition="cut")],
        )
        cfg.edl_path(1).write_text(edl.model_dump_json(indent=2))
        (tmp_path / "photo.jpg").write_bytes(b"\xff\xd8" + b"\x00" * 100)

        ac = AssembleConfig(w=320, h=180, fps=15, version=1)
        # We can't run the full assemble without FFmpeg, but we can test the loading:
        from pipeline.edl import EDL as EDLModel
        edl_loaded = EDLModel.model_validate_json(cfg.edl_path(1).read_text())
        assert edl_loaded.title == "V1"

    def test_missing_version_raises(self, tmp_path):
        from pipeline.assemble import assemble
        cfg = Config(workspace=tmp_path)
        cfg.ensure_dirs()
        ac = AssembleConfig(w=320, h=180, fps=15, version=99)
        with pytest.raises(FileNotFoundError, match="EDL not found"):
            assemble(cfg, ac)
