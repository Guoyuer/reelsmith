"""Unit tests for pipeline.assemble._concat — branching logic and demuxer."""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pipeline.assemble._encoder import RenderContext


class TestConcatenateBranching:
    """Test the high-level concatenate() routing logic."""

    def _make_clip(self, tmp_path, name, transition="cut", td=0.0):
        p = tmp_path / name
        p.write_bytes(b"\x00" * 500)
        return {"path": p, "duration": 3.0, "transition": transition, "transition_duration": td, "keep_audio": False}

    def test_single_clip_copies(self, tmp_path):
        from pipeline.assemble._concat import concatenate
        clip = self._make_clip(tmp_path, "a.mp4")
        out = tmp_path / "out.mp4"
        concatenate([clip], out)
        assert out.exists()
        assert out.read_bytes() == clip["path"].read_bytes()

    def test_all_cuts_uses_demuxer(self, tmp_path):
        from pipeline.assemble._concat import concatenate
        clips = [self._make_clip(tmp_path, f"{i}.mp4") for i in range(3)]
        out = tmp_path / "out.mp4"
        with patch("pipeline.assemble._concat.concat_demuxer") as mock_demux:
            concatenate(clips, out)
        mock_demux.assert_called_once()

    def test_xfade_clips_uses_xfade(self, tmp_path):
        from pipeline.assemble._concat import concatenate
        clips = [
            self._make_clip(tmp_path, "0.mp4", "cut", 0),
            self._make_clip(tmp_path, "1.mp4", "crossfade", 0.5),
            self._make_clip(tmp_path, "2.mp4", "crossfade", 0.5),
        ]
        out = tmp_path / "out.mp4"
        ctx = RenderContext(w=320, h=180, fps=15)
        with patch("pipeline.assemble._concat.concat_xfade") as mock_xfade:
            concatenate(clips, out, ctx=ctx)
        mock_xfade.assert_called_once()


class TestConcatDemuxer:
    """Test the concat demuxer (file list based concat)."""

    def test_writes_concat_list(self, tmp_path):
        from pipeline.assemble._concat import concat_demuxer

        clips = []
        for i in range(2):
            p = tmp_path / f"{i}.mp4"
            p.write_bytes(b"\x00" * 100)
            clips.append({"path": p, "duration": 3.0})

        out = tmp_path / "out.mp4"
        with patch("pipeline.assemble._concat.run_subprocess") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            concat_demuxer(clips, out)
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "concat" in " ".join(str(c) for c in cmd)

    def test_creates_list_file(self, tmp_path):
        from pipeline.assemble._concat import concat_demuxer

        p = tmp_path / "clip.mp4"
        p.write_bytes(b"\x00" * 100)
        clips = [{"path": p, "duration": 3.0}]
        out = tmp_path / "out.mp4"
        with patch("pipeline.assemble._concat.run_subprocess", return_value=MagicMock(returncode=0)):
            concat_demuxer(clips, out)
        list_file = out.with_suffix(".txt")
        assert list_file.exists()
        assert str(p.resolve()) in list_file.read_text()


class TestConcatFilter:
    """Test the concat filter (re-encode based join)."""

    def test_calls_ffmpeg_with_encoder(self, tmp_path):
        from pipeline.assemble._concat import _concat_filter

        ctx = RenderContext(w=320, h=180, fps=15)
        clips = []
        for i in range(2):
            p = tmp_path / f"{i}.mp4"
            p.write_bytes(b"\x00" * 100)
            clips.append({"path": p, "duration": 3.0})
        out = tmp_path / "out.mp4"

        with patch("pipeline.assemble._concat.run_subprocess") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            with patch.object(ctx, "probe_duration", return_value=6.0):
                _concat_filter(clips, out, ctx=ctx)
        mock_run.assert_called_once()

    def test_falls_back_to_demuxer_on_failure(self, tmp_path):
        from pipeline.assemble._concat import _concat_filter

        ctx = RenderContext(w=320, h=180, fps=15)
        p = tmp_path / "clip.mp4"
        p.write_bytes(b"\x00" * 100)
        clips = [{"path": p, "duration": 3.0}]
        out = tmp_path / "out.mp4"

        with patch("pipeline.assemble._concat.run_subprocess") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="error")
            _concat_filter(clips, out, ctx=ctx)
        # Should have been called twice: concat filter + demuxer fallback
        assert mock_run.call_count == 2


class TestConcatXfade:
    """Test xfade concatenation."""

    def test_builds_xfade_filter(self, tmp_path):
        from pipeline.assemble._concat import concat_xfade

        ctx = RenderContext(w=320, h=180, fps=15)
        clips = [
            {"path": tmp_path / "0.mp4", "duration": 5.0, "transition": "cut", "transition_duration": 0.0, "keep_audio": False},
            {"path": tmp_path / "1.mp4", "duration": 4.0, "transition": "crossfade", "transition_duration": 0.5, "keep_audio": False},
        ]
        for c in clips:
            c["path"].write_bytes(b"\x00" * 100)
        out = tmp_path / "out.mp4"

        with patch("pipeline.assemble._concat.run_subprocess") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            with patch.object(ctx, "probe_duration", return_value=8.5):
                concat_xfade(clips, out, ctx=ctx)
        cmd = " ".join(str(c) for c in mock_run.call_args[0][0])
        assert "xfade" in cmd
