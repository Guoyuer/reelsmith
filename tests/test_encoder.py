"""Tests for pipeline.assemble._encoder — RenderContext, bitrate calculation."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pipeline.assemble._encoder import RenderContext, target_bitrate


class TestRenderContext:
    def test_new_context_has_empty_caches(self):
        ctx1 = RenderContext(w=1920, h=1080, fps=30, quality=0.5)
        ctx1._dim_cache["test"] = (100, 100)
        ctx2 = RenderContext(w=1920, h=1080, fps=30, quality=1.0)
        assert ctx2.quality == 1.0
        assert "test" not in ctx2._dim_cache

    def test_probe_dimensions_caches(self):
        ctx = RenderContext(w=1920, h=1080, fps=30)
        fake = MagicMock()
        fake.stdout = json.dumps({"streams": [{"width": 1920, "height": 1080}]})
        with patch("pipeline.assemble._encoder.run_subprocess", return_value=fake):
            dims = ctx.probe_dimensions(Path("/fake/video.mp4"))
        assert dims == (1920, 1080)
        # Second call should use cache, not subprocess
        with patch(
            "pipeline.assemble._encoder.run_subprocess",
            side_effect=RuntimeError("should not be called"),
        ):
            assert ctx.probe_dimensions(Path("/fake/video.mp4")) == (1920, 1080)

    def test_probe_duration_caches(self):
        ctx = RenderContext(w=1920, h=1080, fps=30)
        fake = MagicMock()
        fake.stdout = "123.45\n"
        with patch("pipeline.utils.media.run_subprocess", return_value=fake):
            dur = ctx.probe_duration(Path("/fake/video.mp4"))
        assert dur == 123.45
        # Second call should use cache
        with patch(
            "pipeline.utils.media.run_subprocess",
            side_effect=RuntimeError("should not be called"),
        ):
            assert ctx.probe_duration(Path("/fake/video.mp4")) == 123.45

    @pytest.mark.parametrize(
        "patch_target, method, stdout, expected",
        [
            (
                "pipeline.assemble._encoder.run_subprocess",
                "probe_dimensions",
                "garbage\n",
                (0, 0),
            ),
            ("pipeline.utils.media.run_subprocess", "probe_duration", "\n", 0.0),
        ],
    )
    def test_handles_bad_output(self, patch_target, method, stdout, expected):
        ctx = RenderContext(w=1920, h=1080, fps=30)
        fake = MagicMock()
        fake.stdout = stdout
        with patch(patch_target, return_value=fake):
            assert getattr(ctx, method)(Path("/bad")) == expected


class TestTargetBitrate:
    @pytest.mark.parametrize(
        "w, h, fps, quality, expected",
        [
            (3840, 2160, 30, 1.0, "45M"),
            (3840, 2160, 60, 1.0, "67M"),
            (1920, 1080, 30, 1.0, "8M"),
            (1920, 1080, 30, 2.0, "16M"),
            (1920, 1080, 30, 0.5, "4M"),
            (320, 180, 24, 1.0, "3M"),
        ],
    )
    def test_bitrate_calculation(self, w, h, fps, quality, expected):
        assert target_bitrate(w, h, fps, quality) == expected
