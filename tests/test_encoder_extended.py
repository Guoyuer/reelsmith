"""Extended tests for pipeline.assemble._encoder — HW encoder detection, edge cases."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from pipeline.assemble._encoder import RenderContext, detect_hw_encoder


class TestDetectHwEncoder:
    def test_fallback_to_libx264_no_ffmpeg(self):
        """When ffmpeg not found on non-macOS, should return libx264 fallback."""
        with (
            patch(
                "pipeline.assemble._encoder.run_subprocess",
                side_effect=OSError("not found"),
            ),
            patch("sys.platform", "linux"),
        ):
            result = detect_hw_encoder(1920, 1080, 30)
        assert "-c:v" in result
        assert "libx264" in result

    def test_fallback_to_libx264_no_encoders(self):
        """When no HW encoders available on non-macOS, should return libx264."""
        mock = MagicMock(returncode=0, stdout="libx264 -- no hw", stderr="")
        with (
            patch("pipeline.assemble._encoder.run_subprocess", return_value=mock),
            patch("sys.platform", "linux"),
        ):
            result = detect_hw_encoder(1920, 1080, 30)
        assert "libx264" in result

    def test_nvenc_hevc_preferred(self):
        """When hevc_nvenc available, prefer it over h264_nvenc."""

        def _side_effect(cmd, **kw):
            m = MagicMock(returncode=0, stderr="")
            cmd_str = " ".join(str(c) for c in cmd)
            if "-encoders" in cmd_str:
                m.stdout = "hevc_nvenc\nh264_nvenc\n"
            elif "hevc_nvenc" in cmd_str:
                m.returncode = 0  # hevc works
            else:
                m.stdout = ""
            return m

        with patch(
            "pipeline.assemble._encoder.run_subprocess", side_effect=_side_effect
        ):
            with patch("sys.platform", "linux"):
                result = detect_hw_encoder(1920, 1080, 30)
        assert "hevc_nvenc" in result

    def test_hevc_bitrate_is_65_pct_of_h264(self):
        """HEVC bitrate should be 65% of H264 bitrate."""

        def _side_effect(cmd, **kw):
            m = MagicMock(returncode=0, stderr="")
            cmd_str = " ".join(str(c) for c in cmd)
            if "-encoders" in cmd_str:
                m.stdout = "hevc_nvenc\n"
            else:
                m.returncode = 0
            return m

        with patch(
            "pipeline.assemble._encoder.run_subprocess", side_effect=_side_effect
        ):
            with patch("sys.platform", "linux"):
                result = detect_hw_encoder(1920, 1080, 30)
        # H264 for 1080p30 = 8M, HEVC = 65% = 5M
        if "hevc_nvenc" in result:
            br_idx = result.index("-b:v") + 1
            assert result[br_idx] == "5M"


class TestRenderContextEdgeCases:
    def test_rotation_180_no_swap(self):
        """Rotation 180 should NOT swap dimensions (upside down, same aspect)."""
        import json

        ctx = RenderContext(w=1920, h=1080, fps=30)
        fake = MagicMock()
        fake.stdout = json.dumps(
            {
                "streams": [
                    {
                        "width": 1920,
                        "height": 1080,
                        "side_data_list": [{"rotation": 180}],
                    }
                ]
            }
        )
        with patch("pipeline.assemble._encoder.run_subprocess", return_value=fake):
            w, h = ctx.probe_dimensions(Path("/fake.mp4"))
        assert (w, h) == (1920, 1080)

    def test_rotation_270_swaps(self):
        """Rotation 270 should swap dimensions."""
        import json

        ctx = RenderContext(w=1920, h=1080, fps=30)
        fake = MagicMock()
        fake.stdout = json.dumps(
            {
                "streams": [
                    {
                        "width": 1920,
                        "height": 1080,
                        "side_data_list": [{"rotation": -270}],
                    }
                ]
            }
        )
        with patch("pipeline.assemble._encoder.run_subprocess", return_value=fake):
            w, h = ctx.probe_dimensions(Path("/fake.mp4"))
        assert (w, h) == (1080, 1920)

    def test_encoder_cached(self):
        """get_encoder should cache result for same params."""
        ctx = RenderContext(w=1920, h=1080, fps=30)
        with patch(
            "pipeline.assemble._encoder.detect_hw_encoder",
            return_value=["-c:v", "libx264", "-b:v", "8M"],
        ) as mock_detect:
            enc1 = ctx.get_encoder()
            enc2 = ctx.get_encoder()
        assert enc1 == enc2
        mock_detect.assert_called_once()
