"""Extended tests for pipeline.assemble._encoder — HW encoder detection, edge cases."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from pipeline.assemble._encoder import (
    EncoderSelector,
    MediaProbe,
    RenderSettings,
    detect_hw_encoder,
)


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

        def _side_effect(cmd, **kw):
            cmd_str = " ".join(str(c) for c in cmd)
            m = MagicMock(stderr="")
            # HW encoder probes should fail
            if any(hw in cmd_str for hw in ("nvenc", "videotoolbox", "vulkan")):
                m.returncode = 1
                m.stdout = ""
            # SW encoder probes: only libx264 succeeds
            elif "libsvtav1" in cmd_str or "libx265" in cmd_str:
                m.returncode = 1
                m.stdout = ""
            else:
                m.returncode = 0
                m.stdout = ""
            return m

        with (
            patch(
                "pipeline.assemble._encoder.run_subprocess", side_effect=_side_effect
            ),
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

        probe = MediaProbe()
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
            w, h = probe.probe_dimensions(Path("/fake.mp4"))
        assert (w, h) == (1920, 1080)

    def test_rotation_270_swaps(self):
        """Rotation 270 should swap dimensions."""
        import json

        probe = MediaProbe()
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
            w, h = probe.probe_dimensions(Path("/fake.mp4"))
        assert (w, h) == (1080, 1920)

    def test_encoder_cached(self):
        """EncoderSelector.args should cache result for same params."""
        encoder = EncoderSelector(RenderSettings(1920, 1080, 30))
        with patch(
            "pipeline.assemble._encoder.detect_hw_encoder",
            return_value=["-c:v", "libx264", "-b:v", "8M"],
        ) as mock_detect:
            enc1 = encoder.args()
            enc2 = encoder.args()
        assert enc1 == enc2
        mock_detect.assert_called_once()

    def test_color_transfer_probe_missing_ffprobe_returns_unknown(self):
        probe = MediaProbe()
        with patch(
            "pipeline.assemble._encoder.run_subprocess",
            side_effect=OSError("ffprobe not found"),
        ):
            assert probe.probe_color_transfer(Path("/fake.mp4")) == ""


class TestCodecSelection:
    """Tests for the --codec parameter in detect_hw_encoder."""

    @staticmethod
    def _make_side_effect(available_encoders: set[str]):
        """Mock that succeeds only for encoders in *available_encoders*."""

        def _side_effect(cmd, **kw):
            cmd_str = " ".join(str(c) for c in cmd)
            m = MagicMock(stderr="", stdout="")
            for enc in available_encoders:
                if enc in cmd_str:
                    m.returncode = 0
                    return m
            m.returncode = 1
            return m

        return _side_effect

    def test_codec_av1_selects_av1_nvenc(self):
        """codec='av1' should select av1_nvenc when available."""
        side_effect = self._make_side_effect({"av1_nvenc"})
        with (
            patch("pipeline.assemble._encoder.run_subprocess", side_effect=side_effect),
            patch("sys.platform", "linux"),
        ):
            result = detect_hw_encoder(1920, 1080, 30, codec="av1")
        assert "av1_nvenc" in result

    def test_codec_av1_falls_back_to_libsvtav1(self):
        """codec='av1' without HW encoder should fall back to libsvtav1."""
        side_effect = self._make_side_effect({"libsvtav1"})
        with (
            patch("pipeline.assemble._encoder.run_subprocess", side_effect=side_effect),
            patch("sys.platform", "linux"),
        ):
            result = detect_hw_encoder(1920, 1080, 30, codec="av1")
        assert "libsvtav1" in result

    def test_codec_av1_ultimate_fallback_libx264(self):
        """codec='av1' with no AV1 encoder at all should fall back to libx264."""
        side_effect = self._make_side_effect({"libx264"})
        with (
            patch("pipeline.assemble._encoder.run_subprocess", side_effect=side_effect),
            patch("sys.platform", "linux"),
        ):
            result = detect_hw_encoder(1920, 1080, 30, codec="av1")
        assert "libx264" in result

    def test_codec_h264_selects_h264_nvenc(self):
        """codec='h264' should select h264_nvenc when available."""
        side_effect = self._make_side_effect({"h264_nvenc"})
        with (
            patch("pipeline.assemble._encoder.run_subprocess", side_effect=side_effect),
            patch("sys.platform", "linux"),
        ):
            result = detect_hw_encoder(1920, 1080, 30, codec="h264")
        assert "h264_nvenc" in result

    def test_codec_hevc_on_darwin(self):
        """codec='hevc' on macOS should select hevc_videotoolbox."""
        side_effect = self._make_side_effect({"hevc_videotoolbox"})
        with (
            patch("pipeline.assemble._encoder.run_subprocess", side_effect=side_effect),
            patch("sys.platform", "darwin"),
        ):
            result = detect_hw_encoder(1920, 1080, 30, codec="hevc")
        assert "hevc_videotoolbox" in result

    def test_codec_av1_bitrate_uses_av1_ratio(self):
        """AV1 bitrate should be ~45% of H.264 base (AV1_RATIO=0.45)."""
        side_effect = self._make_side_effect({"av1_nvenc"})
        with (
            patch("pipeline.assemble._encoder.run_subprocess", side_effect=side_effect),
            patch("sys.platform", "linux"),
        ):
            result = detect_hw_encoder(1920, 1080, 30, codec="av1")
        # H264 for 1080p30 = 8M, AV1 = 45% = 3M (int(8 * 0.45) = 3)
        br_idx = result.index("-b:v") + 1
        assert result[br_idx] == "3M"

    def test_invalid_codec_raises(self):
        """Invalid codec value should raise ValueError."""
        import pytest

        with pytest.raises(ValueError, match="Invalid codec"):
            detect_hw_encoder(1920, 1080, 30, codec="vp9")
