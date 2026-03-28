"""Tests for pipeline.assemble._assemble — _concat_and_mix, _render_segments internals."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from pipeline.assemble._assemble import (
    _concat_and_mix,
    _parse_loudnorm_stats,
    _validate_output,
)
from pipeline.assemble._encoder import RenderContext
from pipeline.config import Config
from pipeline.edl import MusicTrack

from tests.conftest import minimal_edl as _minimal_edl


def _mock_run_ok(cmd, **kw):
    """Mock run_subprocess returning success."""
    m = MagicMock()
    m.returncode = 0
    m.stderr = ""
    m.stdout = "hevc,video,60.0\naac,audio,60.0\n"
    return m


# ---------------------------------------------------------------------------
# _concat_and_mix — no music
# ---------------------------------------------------------------------------


class TestConcatAndMixNoMusic:
    def test_concat_creates_output(self, tmp_path):
        """Without music, concat copies directly to output_path."""
        cfg = Config(workspace=tmp_path / "ws" / "runs" / "test")
        cfg.ensure_dirs()
        ctx = RenderContext(w=1920, h=1080, fps=30)
        edl = _minimal_edl()
        output = cfg.output_dir / "out.mp4"

        seg0 = cfg.output_dir / "_seg_0_1080p30.ts"
        seg0.write_bytes(b"\x00" * 500)

        with (
            patch(
                "pipeline.assemble._assemble.run_subprocess", side_effect=_mock_run_ok
            ),
            patch.object(ctx, "probe_duration", return_value=60.0),
        ):
            _concat_and_mix(
                [seg0], edl, ctx, cfg, output, version=1, res_label="1080p30"
            )

    def test_concat_failure_raises(self, tmp_path):
        """Non-zero ffmpeg concat raises RuntimeError."""
        cfg = Config(workspace=tmp_path / "ws" / "runs" / "test")
        cfg.ensure_dirs()
        ctx = RenderContext(w=1920, h=1080, fps=30)
        edl = _minimal_edl()
        output = cfg.output_dir / "out.mp4"

        seg0 = cfg.output_dir / "_seg_0_1080p30.ts"
        seg0.write_bytes(b"\x00" * 500)

        fail = MagicMock(returncode=1, stderr="concat error", stdout="")

        with (
            patch("pipeline.assemble._assemble.run_subprocess", return_value=fail),
            patch.object(ctx, "probe_duration", return_value=60.0),
        ):
            with pytest.raises(RuntimeError, match="Concat failed"):
                _concat_and_mix(
                    [seg0], edl, ctx, cfg, output, version=1, res_label="1080p30"
                )


# ---------------------------------------------------------------------------
# _concat_and_mix — with music
# ---------------------------------------------------------------------------


class TestConcatAndMixWithMusic:
    def test_music_mix_filter_chain(self, tmp_path):
        """With music, verify sidechaincompress + loudnorm in filter chain."""
        cfg = Config(workspace=tmp_path / "ws" / "runs" / "test")
        cfg.ensure_dirs()
        ctx = RenderContext(w=1920, h=1080, fps=30)

        music_file = tmp_path / "music.mp3"
        music_file.write_bytes(b"\x00" * 500)

        edl = _minimal_edl(
            music=MusicTrack(
                file=str(music_file), volume=0.4, fade_in=2.0, fade_out=3.0
            )
        )
        output = cfg.output_dir / "out.mp4"
        nomix = cfg.output_dir / "vlog_v1_1080p30_nomix.mp4"

        seg0 = cfg.output_dir / "_seg_0_1080p30.ts"
        seg0.write_bytes(b"\x00" * 500)

        calls = []
        _loudnorm_json = (
            '{\n"input_i": "-24.0", "input_tp": "-2.0",'
            ' "input_lra": "7.0", "input_thresh": "-34.0"\n}'
        )

        def _track_calls(cmd, **kw):
            calls.append(cmd)
            # Measure pass: return loudnorm JSON in stderr
            if "-f" in cmd and "null" in cmd:
                return MagicMock(returncode=0, stderr=_loudnorm_json, stdout="")
            m = MagicMock(returncode=0, stderr="", stdout="")
            # Make nomix and output exist after their respective commands
            if any(str(nomix) in str(c) for c in cmd):
                nomix.write_bytes(b"\x00" * 2000)
            if any(str(output) in str(c) for c in cmd):
                output.write_bytes(b"\x00" * 2000)
            return m

        with (
            patch(
                "pipeline.assemble._assemble.run_subprocess", side_effect=_track_calls
            ),
            patch.object(ctx, "probe_duration", return_value=60.0),
        ):
            _concat_and_mix(
                [seg0], edl, ctx, cfg, output, version=1, res_label="1080p30"
            )

        # Two ffmpeg calls with -filter_complex: measure + apply
        mix_cmds = [c for c in calls if "-filter_complex" in c]
        assert len(mix_cmds) == 2

        # Pass 1: measure (outputs to null)
        measure_fc = mix_cmds[0][mix_cmds[0].index("-filter_complex") + 1]
        assert "print_format=json" in measure_fc

        # Pass 2: apply with measured values
        apply_fc = mix_cmds[1][mix_cmds[1].index("-filter_complex") + 1]
        assert "sidechaincompress" in apply_fc
        assert "measured_I=-24.0" in apply_fc
        assert "linear=true" in apply_fc
        assert "volume=0.400" in apply_fc
        assert "afade=t=in:d=2.0" in apply_fc

    def test_music_loop_when_short(self, tmp_path):
        """When music shorter than video, aloop filter is added."""
        cfg = Config(workspace=tmp_path / "ws" / "runs" / "test")
        cfg.ensure_dirs()
        ctx = RenderContext(w=1920, h=1080, fps=30)

        music_file = tmp_path / "short_music.mp3"
        music_file.write_bytes(b"\x00" * 500)

        edl = _minimal_edl(music=MusicTrack(file=str(music_file)))
        output = cfg.output_dir / "out.mp4"
        nomix = cfg.output_dir / "vlog_v1_1080p30_nomix.mp4"

        seg0 = cfg.output_dir / "_seg_0_1080p30.ts"
        seg0.write_bytes(b"\x00" * 500)

        calls = []
        _loudnorm_json = (
            '{\n"input_i": "-24.0", "input_tp": "-2.0",'
            ' "input_lra": "7.0", "input_thresh": "-34.0"\n}'
        )

        def _track(cmd, **kw):
            calls.append(cmd)
            if "-f" in cmd and "null" in cmd:
                return MagicMock(returncode=0, stderr=_loudnorm_json, stdout="")
            m = MagicMock(returncode=0, stderr="", stdout="")
            if any(str(nomix) in str(c) for c in cmd):
                nomix.write_bytes(b"\x00" * 2000)
            if any(str(output) in str(c) for c in cmd):
                output.write_bytes(b"\x00" * 2000)
            return m

        # Music duration 20s < total 60s → should loop
        def _probe(path):
            if "short_music" in str(path):
                return 20.0
            return 60.0

        with (
            patch("pipeline.assemble._assemble.run_subprocess", side_effect=_track),
            patch.object(ctx, "probe_duration", side_effect=_probe),
        ):
            _concat_and_mix(
                [seg0], edl, ctx, cfg, output, version=1, res_label="1080p30"
            )

        mix_cmds = [c for c in calls if "-filter_complex" in c]
        assert len(mix_cmds) >= 1
        fc = mix_cmds[0][mix_cmds[0].index("-filter_complex") + 1]
        assert "aloop" in fc

    def test_mix_failure_raises(self, tmp_path):
        """Music mix failure raises RuntimeError."""
        cfg = Config(workspace=tmp_path / "ws" / "runs" / "test")
        cfg.ensure_dirs()
        ctx = RenderContext(w=1920, h=1080, fps=30)

        music_file = tmp_path / "music.mp3"
        music_file.write_bytes(b"\x00" * 500)

        edl = _minimal_edl(music=MusicTrack(file=str(music_file)))
        output = cfg.output_dir / "out.mp4"
        nomix = cfg.output_dir / "vlog_v1_1080p30_nomix.mp4"

        seg0 = cfg.output_dir / "_seg_0_1080p30.ts"
        seg0.write_bytes(b"\x00" * 500)

        call_count = [0]
        _loudnorm_json = (
            '{\n"input_i": "-24.0", "input_tp": "-2.0",'
            ' "input_lra": "7.0", "input_thresh": "-34.0"\n}'
        )

        def _mock(cmd, **kw):
            call_count[0] += 1
            m = MagicMock(returncode=0, stderr="", stdout="")
            if call_count[0] == 1:
                # Concat succeeds
                nomix.write_bytes(b"\x00" * 2000)
            elif "-f" in cmd and "null" in cmd:
                # Measure pass
                m.stderr = _loudnorm_json
            else:
                # Mix fails — output not created
                m.returncode = 1
                m.stderr = "mix error"
            return m

        with (
            patch("pipeline.assemble._assemble.run_subprocess", side_effect=_mock),
            patch.object(ctx, "probe_duration", return_value=60.0),
        ):
            with pytest.raises(RuntimeError, match="Music mix failed"):
                _concat_and_mix(
                    [seg0], edl, ctx, cfg, output, version=1, res_label="1080p30"
                )


# ---------------------------------------------------------------------------
# _validate_output — edge cases
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _parse_loudnorm_stats
# ---------------------------------------------------------------------------


class TestParseLoudnormStats:
    def test_empty_string(self):
        assert _parse_loudnorm_stats("") is None


class TestLoudnormFallback:
    """When measurement fails, single-pass loudnorm is used as fallback."""

    def test_fallback_on_bad_measurement(self, tmp_path):
        cfg = Config(workspace=tmp_path / "ws" / "runs" / "test")
        cfg.ensure_dirs()
        ctx = RenderContext(w=1920, h=1080, fps=30)

        music_file = tmp_path / "music.mp3"
        music_file.write_bytes(b"\x00" * 500)

        edl = _minimal_edl(music=MusicTrack(file=str(music_file)))
        output = cfg.output_dir / "out.mp4"
        nomix = cfg.output_dir / "vlog_v1_1080p30_nomix.mp4"
        seg0 = cfg.output_dir / "_seg_0_1080p30.ts"
        seg0.write_bytes(b"\x00" * 500)

        calls = []

        def _mock(cmd, **kw):
            calls.append(cmd)
            if "-f" in cmd and "null" in cmd:
                # Measurement returns garbage
                return MagicMock(returncode=0, stderr="no json here", stdout="")
            m = MagicMock(returncode=0, stderr="", stdout="")
            if any(str(nomix) in str(c) for c in cmd):
                nomix.write_bytes(b"\x00" * 2000)
            if any(str(output) in str(c) for c in cmd):
                output.write_bytes(b"\x00" * 2000)
            return m

        with (
            patch("pipeline.assemble._assemble.run_subprocess", side_effect=_mock),
            patch.object(ctx, "probe_duration", return_value=60.0),
        ):
            _concat_and_mix(
                [seg0], edl, ctx, cfg, output, version=1, res_label="1080p30"
            )

        # Apply pass should use single-pass loudnorm (no measured_ params)
        apply_cmds = [c for c in calls if "-filter_complex" in c and "-f" not in c]
        assert len(apply_cmds) == 1
        fc = apply_cmds[0][apply_cmds[0].index("-filter_complex") + 1]
        assert "loudnorm=I=-16" in fc
        assert "measured_I" not in fc


class TestValidateOutputEdgeCases:
    def test_ctx_none_duration(self, tmp_path):
        """When ctx=None, duration is 0.0 → error."""
        out = tmp_path / "out.mp4"
        out.write_bytes(b"\x00" * 2048)
        edl = _minimal_edl()
        mock = MagicMock(returncode=0, stdout="hevc,video,60.0\naac,audio,60.0\n")
        with patch("pipeline.assemble._assemble.run_subprocess", return_value=mock):
            issues = _validate_output(
                out, edl, has_speech=False, resolution=(1920, 1080), ctx=None
            )
        dur_issues = [i for i in issues if i["check"] == "duration"]
        assert len(dur_issues) == 1
        assert dur_issues[0]["level"] == "error"

    def test_no_streams_with_speech_and_music(self, tmp_path):
        """Missing audio when both speech + music expected."""
        out = tmp_path / "out.mp4"
        out.write_bytes(b"\x00" * 2048)
        music_file = tmp_path / "music.mp3"
        music_file.write_bytes(b"\x00" * 100)
        edl = _minimal_edl(music=MusicTrack(file=str(music_file)))

        def _mock_run(cmd, **kw):
            m = MagicMock()
            m.returncode = 0
            m.stdout = "hevc,video,60.0\n"  # no audio stream
            return m

        ctx = RenderContext(w=1920, h=1080, fps=30)
        with (
            patch("pipeline.assemble._assemble.run_subprocess", side_effect=_mock_run),
            patch.object(ctx, "probe_duration", return_value=60.0),
            patch.object(ctx, "probe_dimensions", return_value=(1920, 1080)),
        ):
            issues = _validate_output(
                out, edl, has_speech=True, resolution=(1920, 1080), ctx=ctx
            )
        stream_issues = [i for i in issues if i["check"] == "streams"]
        assert len(stream_issues) >= 1
