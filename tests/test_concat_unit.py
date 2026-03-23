"""Unit tests for pipeline.assemble._concat — demuxer concatenation."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from pipeline.assemble._concat import concatenate


class TestConcatenate:
    def _make_clip(self, tmp_path, name):
        p = tmp_path / name
        p.write_bytes(b"\x00" * 500)
        return {"path": p, "duration": 3.0, "keep_audio": False}

    def test_single_clip_copies(self, tmp_path):
        clip = self._make_clip(tmp_path, "a.mp4")
        out = tmp_path / "out.mp4"
        concatenate([clip], out)
        assert out.exists()
        assert out.read_bytes() == clip["path"].read_bytes()

    def test_multiple_clips_calls_ffmpeg(self, tmp_path):
        clips = [self._make_clip(tmp_path, f"{i}.mp4") for i in range(3)]
        out = tmp_path / "out.mp4"
        with patch("pipeline.assemble._concat.run_subprocess") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            concatenate(clips, out)
        mock_run.assert_called_once()
        cmd = " ".join(str(c) for c in mock_run.call_args[0][0])
        assert "concat" in cmd
        assert "-c:v" in cmd and "copy" in cmd

    def test_creates_list_file(self, tmp_path):
        clips = [self._make_clip(tmp_path, f"{i}.mp4") for i in range(2)]
        out = tmp_path / "out.mp4"
        with patch("pipeline.assemble._concat.run_subprocess", return_value=MagicMock(returncode=0)):
            concatenate(clips, out)
        list_file = out.with_suffix(".txt")
        assert list_file.exists()
        content = list_file.read_text()
        for clip in clips:
            assert str(clip["path"].resolve()).replace("\\", "/") in content

    def test_raises_on_failure(self, tmp_path):
        import pytest
        clips = [self._make_clip(tmp_path, f"{i}.mp4") for i in range(2)]
        out = tmp_path / "out.mp4"
        with patch("pipeline.assemble._concat.run_subprocess") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="error details")
            with pytest.raises(RuntimeError, match="Concat failed"):
                concatenate(clips, out)
