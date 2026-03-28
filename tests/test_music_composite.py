"""Tests for pipeline.music._orchestrate — composite music building edge cases."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from pipeline.music._orchestrate import _DEFAULT_CROSSFADE, _build_composite_music


class TestBuildCompositeMusic:
    def test_single_segment_copies(self, tmp_path):
        track = tmp_path / "track.wav"
        track.write_bytes(b"RIFF" + b"\x00" * 100)
        out = tmp_path / "composite.wav"
        assert _build_composite_music([(10.0, track)], out, crossfade=2.0) is True
        assert out.exists()

    def test_empty_returns_false(self, tmp_path):
        assert (
            _build_composite_music([], tmp_path / "composite.wav", crossfade=2.0)
            is False
        )

    def test_two_segments_crossfade(self, tmp_path):
        """Two segments should produce acrossfade filter chain."""
        t1 = tmp_path / "seg1.wav"
        t2 = tmp_path / "seg2.wav"
        t1.write_bytes(b"RIFF" + b"\x00" * 100)
        t2.write_bytes(b"RIFF" + b"\x00" * 100)
        out = tmp_path / "composite.wav"

        calls = []

        def _mock_run(cmd, **kw):
            calls.append(cmd)
            m = MagicMock(returncode=0, stderr="")
            # Create trimmed files for first N calls (trim phase)
            for c in cmd:
                if str(c).endswith(".wav") and "_seg_music_" in str(c):
                    Path(c).write_bytes(b"RIFF" + b"\x00" * 50)
            # Create output for compose phase — check last arg is the output path
            if Path(cmd[-1]) == out:
                out.write_bytes(b"RIFF" + b"\x00" * 200)
            return m

        with patch("pipeline.utils.media.run_subprocess", side_effect=_mock_run):
            result = _build_composite_music(
                [(10.0, t1), (10.0, t2)], out, crossfade=_DEFAULT_CROSSFADE
            )

        assert result is True
        assert out.exists()
        # Should have acrossfade in one of the commands
        compose_cmds = [c for c in calls if "-filter_complex" in c]
        assert len(compose_cmds) >= 1
        fc = compose_cmds[0][compose_cmds[0].index("-filter_complex") + 1]
        assert "acrossfade" in fc

    def test_trim_failure_skips_segment(self, tmp_path):
        """When one trim fails, that segment is skipped."""
        t1 = tmp_path / "seg1.wav"
        t2 = tmp_path / "seg2.wav"
        t1.write_bytes(b"RIFF" + b"\x00" * 100)
        t2.write_bytes(b"RIFF" + b"\x00" * 100)
        out = tmp_path / "composite.wav"

        call_count = [0]

        def _mock_run(cmd, **kw):
            call_count[0] += 1
            m = MagicMock(stderr="")
            if call_count[0] == 1:
                # First trim fails
                m.returncode = 1
            else:
                # Second trim succeeds
                m.returncode = 0
                for c in cmd:
                    if str(c).endswith(".wav") and "_seg_music_" in str(c):
                        Path(c).write_bytes(b"RIFF" + b"\x00" * 50)
            return m

        with patch("pipeline.utils.media.run_subprocess", side_effect=_mock_run):
            result = _build_composite_music(
                [(10.0, t1), (10.0, t2)], out, crossfade=_DEFAULT_CROSSFADE
            )

        # With only 1 trimmed track, should copy it instead of crossfade
        assert result is True

    def test_all_trims_fail_multi(self, tmp_path):
        """When all trims fail for multi-segment, returns False."""
        t1 = tmp_path / "seg1.wav"
        t2 = tmp_path / "seg2.wav"
        t1.write_bytes(b"RIFF" + b"\x00" * 100)
        t2.write_bytes(b"RIFF" + b"\x00" * 100)
        out = tmp_path / "composite.wav"

        mock = MagicMock(returncode=1, stderr="trim error")
        with patch("pipeline.utils.media.run_subprocess", return_value=mock):
            result = _build_composite_music(
                [(10.0, t1), (10.0, t2)], out, crossfade=_DEFAULT_CROSSFADE
            )

        # All trims fail → trimmed list empty → returns False
        assert result is False

    def test_compose_failure_returns_false(self, tmp_path):
        """When acrossfade compose fails, returns False and cleans up."""
        t1 = tmp_path / "seg1.wav"
        t2 = tmp_path / "seg2.wav"
        t1.write_bytes(b"RIFF" + b"\x00" * 100)
        t2.write_bytes(b"RIFF" + b"\x00" * 100)
        out = tmp_path / "composite.wav"

        call_count = [0]

        def _mock_run(cmd, **kw):
            call_count[0] += 1
            m = MagicMock(stderr="")
            if call_count[0] <= 2:
                # Trim calls succeed
                m.returncode = 0
                for c in cmd:
                    if str(c).endswith(".wav") and "_seg_music_" in str(c):
                        Path(c).write_bytes(b"RIFF" + b"\x00" * 50)
            else:
                # Compose call fails
                m.returncode = 1
                m.stderr = "compose error"
            return m

        with patch("pipeline.utils.media.run_subprocess", side_effect=_mock_run):
            result = _build_composite_music(
                [(10.0, t1), (10.0, t2)], out, crossfade=2.0
            )

        assert result is False
        # Trimmed temp files should be cleaned up
        assert not (tmp_path / "_seg_music_0.wav").exists()
        assert not (tmp_path / "_seg_music_1.wav").exists()

    def test_three_segments_chain(self, tmp_path):
        """Three segments produce chained acrossfade: [0:a][1:a]→[a1], [a1][2:a]→[out]."""
        tracks = []
        for i in range(3):
            t = tmp_path / f"seg{i}.wav"
            t.write_bytes(b"RIFF" + b"\x00" * 100)
            tracks.append((10.0, t))
        out = tmp_path / "composite.wav"

        calls = []

        def _mock_run(cmd, **kw):
            calls.append(cmd)
            m = MagicMock(returncode=0, stderr="")
            for c in cmd:
                sc = str(c)
                if sc.endswith(".wav") and "_seg_music_" in sc:
                    Path(sc).write_bytes(b"RIFF" + b"\x00" * 50)
            if Path(cmd[-1]) == out:
                out.write_bytes(b"RIFF" + b"\x00" * 200)
            return m

        with patch("pipeline.utils.media.run_subprocess", side_effect=_mock_run):
            result = _build_composite_music(tracks, out, crossfade=_DEFAULT_CROSSFADE)

        assert result is True
        compose_cmds = [c for c in calls if "-filter_complex" in c]
        fc = compose_cmds[0][compose_cmds[0].index("-filter_complex") + 1]
        # Should have 2 acrossfade filters chained
        assert fc.count("acrossfade") == 2
