"""Unit tests for pipeline.assemble._audio — mix_final_audio, add_music, write_chapters."""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pipeline.edl import EDL, EditItem, MusicTrack, Segment


class TestMixFinalAudio:
    """Test mix_final_audio routing logic."""

    def test_no_music_no_speech_copies(self, tmp_path):
        from pipeline.assemble._audio import mix_final_audio

        video = tmp_path / "video.mp4"
        video.write_bytes(b"\x00" * 1000)
        out = tmp_path / "out.mp4"

        mix_final_audio(video, out)
        assert out.exists()
        assert not video.exists()  # original moved

    def test_speech_only_muxes(self, tmp_path):
        from pipeline.assemble._audio import mix_final_audio

        video = tmp_path / "video.mp4"
        video.write_bytes(b"\x00" * 1000)
        speech = tmp_path / "speech.wav"
        speech.write_bytes(b"\x00" * 500)
        out = tmp_path / "out.mp4"

        with patch("pipeline.assemble._audio.run_subprocess") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_run.return_value = mock_result
            # Create output to simulate ffmpeg
            def side_effect(cmd, **kw):
                out.write_bytes(b"\x00" * 1500)
                return mock_result
            mock_run.side_effect = side_effect

            mix_final_audio(video, out, speech_audio_path=speech)

        mock_run.assert_called_once()

    def test_with_music_calls_add_music(self, tmp_path):
        from pipeline.assemble._audio import mix_final_audio

        video = tmp_path / "video.mp4"
        video.write_bytes(b"\x00" * 1000)
        music_file = tmp_path / "music.wav"
        music_file.write_bytes(b"\x00" * 500)
        out = tmp_path / "out.mp4"
        music = MusicTrack(file=str(music_file))

        with patch("pipeline.assemble._audio.add_music") as mock_add:
            mix_final_audio(video, out, music_track=music)
        mock_add.assert_called_once()


class TestAddMusic:
    """Test add_music ducking and looping."""

    def test_calls_ffmpeg_with_ducking(self, tmp_path):
        from pipeline.assemble._audio import add_music

        video = tmp_path / "video.mp4"
        video.write_bytes(b"\x00" * 1000)
        out = tmp_path / "out.mp4"
        music = MusicTrack(file=str(tmp_path / "music.wav"))
        (tmp_path / "music.wav").write_bytes(b"\x00" * 500)

        ctx = MagicMock()
        ctx.probe_duration.side_effect = lambda p: 60.0 if "video" in str(p) else 120.0

        with patch("pipeline.assemble._audio.run_subprocess") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            add_music(video, music, out, speech_ranges=[(5.0, 10.0)], ctx=ctx)

        cmd = " ".join(str(c) for c in mock_run.call_args[0][0])
        assert "volume=" in cmd  # ducking filter applied

    def test_loops_short_music(self, tmp_path):
        from pipeline.assemble._audio import add_music

        video = tmp_path / "video.mp4"
        video.write_bytes(b"\x00" * 1000)
        out = tmp_path / "out.mp4"
        music = MusicTrack(file=str(tmp_path / "music.wav"))
        (tmp_path / "music.wav").write_bytes(b"\x00" * 500)

        ctx = MagicMock()
        ctx.probe_duration.side_effect = lambda p: 120.0 if "video" in str(p) else 30.0  # music shorter

        with patch("pipeline.assemble._audio.run_subprocess") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            add_music(video, music, out, ctx=ctx)

        cmd = " ".join(str(c) for c in mock_run.call_args[0][0])
        assert "aloop" in cmd  # looping applied


class TestWriteChapters:
    """Test YouTube chapter marker generation."""

    def test_requires_timeline(self, tmp_path):
        from pipeline.assemble._audio import write_chapters

        edl = EDL(
            title="T",
            target_duration=60,
            segments=[
                Segment(name="S1", items=[EditItem(source_file="a.jpg", media_type="photo", display_duration=4.0)], transition="cut"),
            ],
        )
        out = tmp_path / "chapters.txt"
        with pytest.raises(ValueError, match="Timeline"):
            write_chapters(edl, [], out)
