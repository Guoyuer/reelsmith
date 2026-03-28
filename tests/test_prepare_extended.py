"""Extended tests for pipeline.prepare — keyframe detection, video probe edge cases."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from pipeline.prepare._prepare import (
    _has_dense_keyframes,
    _prepare_video,
)

# ---------------------------------------------------------------------------
# _has_dense_keyframes
# ---------------------------------------------------------------------------


class TestHasDenseKeyframes:
    def test_dense_keyframes_true(self, tmp_path):
        """Keyframe interval <= 2.0s should return True."""
        source = tmp_path / "video.mp4"
        source.write_bytes(b"\x00" * 100)
        # Simulate 5 keyframes at 0, 0.5, 1.0, 1.5, 2.0 (interval = 0.5s)
        output = "0.0,K__\n0.5,K__\n1.0,K__\n1.5,K__\n2.0,K__\n"
        mock = MagicMock(returncode=0, stdout=output, stderr="")
        with patch("pipeline.prepare._prepare.run_subprocess", return_value=mock):
            assert _has_dense_keyframes(source) is True

    def test_sparse_keyframes_false(self, tmp_path):
        """Keyframe interval > 2.0s should return False."""
        source = tmp_path / "video.mp4"
        source.write_bytes(b"\x00" * 100)
        output = "0.0,K__\n5.0,K__\n10.0,K__\n"  # interval = 5s
        mock = MagicMock(returncode=0, stdout=output, stderr="")
        with patch("pipeline.prepare._prepare.run_subprocess", return_value=mock):
            assert _has_dense_keyframes(source) is False

    def test_single_keyframe_false(self, tmp_path):
        """Only one keyframe should return False."""
        source = tmp_path / "video.mp4"
        source.write_bytes(b"\x00" * 100)
        mock = MagicMock(returncode=0, stdout="0.0,K__\n", stderr="")
        with patch("pipeline.prepare._prepare.run_subprocess", return_value=mock):
            assert _has_dense_keyframes(source) is False

    def test_ffprobe_fails_false(self, tmp_path):
        """If ffprobe raises, should return False gracefully."""
        source = tmp_path / "video.mp4"
        source.write_bytes(b"\x00" * 100)
        with patch(
            "pipeline.prepare._prepare.run_subprocess", side_effect=Exception("fail")
        ):
            assert _has_dense_keyframes(source) is False

    def test_non_keyframe_lines_ignored(self, tmp_path):
        """Lines without K flag should be ignored."""
        source = tmp_path / "video.mp4"
        source.write_bytes(b"\x00" * 100)
        output = "0.0,K__\n0.033,___\n0.066,___\n1.0,K__\n"
        mock = MagicMock(returncode=0, stdout=output, stderr="")
        with patch("pipeline.prepare._prepare.run_subprocess", return_value=mock):
            assert _has_dense_keyframes(source) is True  # 1.0s interval


# ---------------------------------------------------------------------------
# _prepare_video — edge cases
# ---------------------------------------------------------------------------


class TestPrepareVideoEdgeCases:
    def test_malformed_ffprobe_defaults(self, tmp_path):
        """Malformed ffprobe output should produce safe defaults."""
        entry = {"local_path": str(tmp_path / "bad.mp4")}
        mock = MagicMock(returncode=0, stdout="not json at all", stderr="")
        with patch("pipeline.prepare._prepare.run_subprocess", return_value=mock):
            _prepare_video(entry, tmp_path / "bad.mp4", 1, 1)
        assert entry["video_duration"] == 10.0  # default
        assert entry["video_width"] == 0
        assert entry["video_height"] == 0
        assert entry["video_fps"] == 0.0
        assert entry["video_orientation"] == "landscape"

    def test_entry_fields_populated(self, tmp_path):
        """Entry dict should be populated with video metadata."""
        entry = {"local_path": str(tmp_path / "vid.mp4")}
        probe_data = {
            "format": {"duration": "45.5"},
            "streams": [{"width": 3840, "height": 2160, "r_frame_rate": "60/1"}],
        }
        mock = MagicMock(returncode=0, stdout=json.dumps(probe_data), stderr="")
        with patch("pipeline.prepare._prepare.run_subprocess", return_value=mock):
            _prepare_video(entry, tmp_path / "vid.mp4", 1, 1)
        assert entry["video_duration"] == 45.5
        assert entry["video_fps"] == 60.0

    def test_fps_parsing_fractional(self, tmp_path):
        """24000/1001 fps should parse correctly."""
        entry = {"local_path": str(tmp_path / "vid.mp4")}
        probe_data = {
            "format": {"duration": "10.0"},
            "streams": [{"width": 1920, "height": 1080, "r_frame_rate": "24000/1001"}],
        }
        mock = MagicMock(returncode=0, stdout=json.dumps(probe_data), stderr="")
        with patch("pipeline.prepare._prepare.run_subprocess", return_value=mock):
            _prepare_video(entry, tmp_path / "vid.mp4", 1, 1)
        assert entry["video_fps"] == 24.0  # 24000/1001 ≈ 23.976 → rounded to 24.0
