"""Tests for pipeline.encoder — RenderContext, bitrate calculation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from pipeline.assemble._encoder import RenderContext, target_bitrate


class TestRenderContext:
    def test_new_context_has_empty_caches(self):
        ctx1 = RenderContext(quality=0.5)
        ctx1._dim_cache["test"] = (100, 100)
        ctx2 = RenderContext(quality=1.0)
        assert ctx2.quality == 1.0
        assert "test" not in ctx2._dim_cache

    def test_probe_dimensions_caches(self):
        ctx = RenderContext()
        fake = MagicMock()
        fake.stdout = "1920x1080\n"
        with patch("pipeline.assemble._encoder.run_subprocess", return_value=fake):
            dims = ctx.probe_dimensions(Path("/fake/video.mp4"))
        assert dims == (1920, 1080)
        with patch("pipeline.assemble._encoder.run_subprocess", side_effect=RuntimeError("should not be called")):
            dims2 = ctx.probe_dimensions(Path("/fake/video.mp4"))
        assert dims2 == (1920, 1080)

    def test_probe_duration_caches(self):
        ctx = RenderContext()
        fake = MagicMock()
        fake.stdout = "123.45\n"
        with patch("pipeline.assemble._encoder.run_subprocess", return_value=fake):
            dur = ctx.probe_duration(Path("/fake/video.mp4"))
        assert dur == 123.45
        with patch("pipeline.assemble._encoder.run_subprocess", side_effect=RuntimeError("should not be called")):
            dur2 = ctx.probe_duration(Path("/fake/video.mp4"))
        assert dur2 == 123.45

    def test_probe_dimensions_handles_bad_output(self):
        ctx = RenderContext()
        fake = MagicMock()
        fake.stdout = "garbage\n"
        with patch("pipeline.assemble._encoder.run_subprocess", return_value=fake):
            assert ctx.probe_dimensions(Path("/bad")) == (0, 0)

    def test_probe_duration_handles_bad_output(self):
        ctx = RenderContext()
        fake = MagicMock()
        fake.stdout = "\n"
        with patch("pipeline.assemble._encoder.run_subprocess", return_value=fake):
            assert ctx.probe_duration(Path("/bad")) == 0.0


class TestTargetBitrate:
    def test_4k_30fps(self):
        assert target_bitrate(3840, 2160, 30) == "45M"

    def test_4k_60fps(self):
        assert target_bitrate(3840, 2160, 60) == "67M"

    def test_1080p_30fps(self):
        assert target_bitrate(1920, 1080, 30) == "8M"

    def test_quality_multiplier(self):
        assert target_bitrate(1920, 1080, 30, quality=2.0) == "16M"

    def test_quality_half(self):
        assert target_bitrate(1920, 1080, 30, quality=0.5) == "4M"

    def test_small_resolution(self):
        assert target_bitrate(320, 180, 24) == "3M"
