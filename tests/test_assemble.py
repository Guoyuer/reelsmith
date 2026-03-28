"""Tests for pipeline.assemble — portrait detection, output validation, render report."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from pipeline.assemble._assemble import _validate_output
from pipeline.assemble._encoder import RenderContext
from pipeline.assemble._filters import is_portrait as _is_portrait
from pipeline.edl import EDL, EditItem, MusicTrack, Segment

# -----------------------------------------------------------------------
# Pure function tests (no FFmpeg needed)
# -----------------------------------------------------------------------


class TestIsPortrait:
    def test_is_portrait_tall(self):
        """2608x4624 (phone portrait) should be portrait."""
        assert _is_portrait(2608, 4624) is True

    def test_is_portrait_landscape(self):
        """3840x2160 (16:9 landscape) should not be portrait."""
        assert _is_portrait(3840, 2160) is False

    def test_is_portrait_square(self):
        """3000x3000 (1:1 square) should not be portrait."""
        assert _is_portrait(3000, 3000) is False

    def test_is_portrait_slightly_tall(self):
        """3000x3400 (ratio 1.13, below 1.2 threshold) should not be portrait."""
        assert _is_portrait(3000, 3400) is False


# -----------------------------------------------------------------------
# Mocked tests (no real FFmpeg invocation)
# -----------------------------------------------------------------------


class TestProbeDimensions:
    def test_probe_dimensions_parses_json(self):
        """Should parse JSON ffprobe output into (width, height)."""
        import json

        fake_result = MagicMock()
        fake_result.stdout = json.dumps({"streams": [{"width": 3840, "height": 2160}]})
        fake_result.returncode = 0

        ctx = RenderContext(w=1920, h=1080, fps=30)
        with patch(
            "pipeline.assemble._encoder.run_subprocess", return_value=fake_result
        ):
            w, h = ctx.probe_dimensions(Path("/fake/video.mp4"))
        assert (w, h) == (3840, 2160)

    def test_probe_dimensions_rotation(self):
        """Should swap width/height when rotation is 90 or 270."""
        import json

        fake_result = MagicMock()
        fake_result.stdout = json.dumps(
            {
                "streams": [
                    {
                        "width": 3840,
                        "height": 2160,
                        "side_data_list": [{"rotation": -90}],
                    }
                ]
            }
        )
        fake_result.returncode = 0

        ctx = RenderContext(w=1920, h=1080, fps=30)
        with patch(
            "pipeline.assemble._encoder.run_subprocess", return_value=fake_result
        ):
            w, h = ctx.probe_dimensions(Path("/fake/rotated.mp4"))
        assert (w, h) == (2160, 3840)


# -----------------------------------------------------------------------
# Output validation tests (mocked -- no FFmpeg needed)
# -----------------------------------------------------------------------


def _make_edl(duration: float = 60.0, music_file: str = "") -> EDL:
    """Helper to build a minimal EDL for validation tests."""
    music = MusicTrack(file=music_file) if music_file else None
    return EDL(
        title="Test",
        target_duration=duration,
        trip_type="family",
        style="upbeat",
        segments=[
            Segment(
                name="Seg1",
                items=[
                    EditItem(
                        source_file="a.jpg",
                        media_type="photo",
                        display_duration=duration / 2,
                    ),
                    EditItem(
                        source_file="b.jpg",
                        media_type="photo",
                        display_duration=duration / 2,
                    ),
                ],
                transition="cut",
            ),
        ],
        music=music,
    )


def _mock_subprocess_for_validation(
    *,
    streams: str | None = None,
    duration: str = "60.0",
    dimensions: str = "3840x2160",
    vid_stream_dur: str = "60.0",
    aud_stream_dur: str = "60.0",
):
    """Return a side_effect function for run_subprocess that handles all ffprobe calls."""
    # Build combined stream output: codec_name,codec_type,duration
    if streams is None:
        streams = f"hevc,video,{vid_stream_dur}\naac,audio,{aud_stream_dur}\n"

    def _side_effect(cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        cmd_str = " ".join(str(c) for c in cmd)
        if "codec_type,codec_name" in cmd_str:
            result.stdout = streams
        elif "format=duration" in cmd_str:
            result.stdout = duration + "\n"
        elif "stream=width,height" in cmd_str:
            import json as _json

            w_str, h_str = dimensions.split("x")
            result.stdout = _json.dumps(
                {"streams": [{"width": int(w_str), "height": int(h_str)}]}
            )
        else:
            result.stdout = ""
        return result

    return _side_effect


@contextmanager
def _patch_validation(**kwargs):
    """Patch both assemble and encoder run_subprocess for validation tests.

    Yields a fresh RenderContext so validation can probe dimensions/duration.
    """
    mock_fn = _mock_subprocess_for_validation(**kwargs)
    with (
        patch("pipeline.assemble._assemble.run_subprocess", side_effect=mock_fn),
        patch("pipeline.assemble._encoder.run_subprocess", side_effect=mock_fn),
        patch("pipeline.utils.media.run_subprocess", side_effect=mock_fn),
    ):
        yield RenderContext(w=1920, h=1080, fps=30)


class TestValidateOutputFileChecks:
    """Tests for file existence and size checks."""

    def test_missing_file_returns_error(self, tmp_path: Path):
        """Non-existent output file should produce a file_exists error."""
        edl = _make_edl()
        issues = _validate_output(
            tmp_path / "nonexistent.mp4",
            edl,
            has_speech=False,
            resolution=(3840, 2160),
        )
        assert len(issues) == 1
        assert issues[0]["level"] == "error"
        assert issues[0]["check"] == "file"

    def test_empty_file_returns_error(self, tmp_path: Path):
        """Output file smaller than 1KB should produce a file_size error."""
        out = tmp_path / "tiny.mp4"
        out.write_bytes(b"\x00" * 500)
        edl = _make_edl()
        issues = _validate_output(out, edl, has_speech=False, resolution=(3840, 2160))
        assert len(issues) == 1
        assert issues[0]["level"] == "error"
        assert issues[0]["check"] == "file"

    def test_valid_file_no_early_return(self, tmp_path: Path):
        """A file >1KB should not trigger the early-return error path."""
        out = tmp_path / "ok.mp4"
        out.write_bytes(b"\x00" * 2048)
        edl = _make_edl()
        with _patch_validation() as ctx:
            issues = _validate_output(
                out, edl, has_speech=False, resolution=(3840, 2160), ctx=ctx
            )
        # Should pass all checks (no errors for a well-formed mock)
        errors = [i for i in issues if i["level"] == "error"]
        assert len(errors) == 0


class TestValidateOutputDuration:
    """Tests for duration checks against EDL expected duration."""

    def test_duration_below_50pct_is_error(self, tmp_path: Path):
        """Duration <50% of expected should produce an error."""
        out = tmp_path / "short.mp4"
        out.write_bytes(b"\x00" * 2048)
        edl = _make_edl(duration=120.0)  # expected ~120s
        with _patch_validation(duration="50.0") as ctx:
            issues = _validate_output(
                out, edl, has_speech=False, resolution=(3840, 2160), ctx=ctx
            )
        dur_issues = [i for i in issues if i["check"] == "duration"]
        assert len(dur_issues) == 1
        assert dur_issues[0]["level"] == "error"
        assert "50%" in dur_issues[0]["message"]

    def test_duration_between_50_and_80pct_is_warning(self, tmp_path: Path):
        """Duration between 50-80% of expected should produce a warning."""
        out = tmp_path / "medium.mp4"
        out.write_bytes(b"\x00" * 2048)
        edl = _make_edl(duration=100.0)  # expected ~100s
        with _patch_validation(duration="70.0") as ctx:
            issues = _validate_output(
                out, edl, has_speech=False, resolution=(3840, 2160), ctx=ctx
            )
        dur_issues = [i for i in issues if i["check"] == "duration"]
        assert len(dur_issues) == 1
        assert dur_issues[0]["level"] == "warning"

    def test_duration_above_80pct_passes(self, tmp_path: Path):
        """Duration >=80% of expected should not produce any duration issue."""
        out = tmp_path / "good.mp4"
        out.write_bytes(b"\x00" * 2048)
        edl = _make_edl(duration=100.0)
        with _patch_validation(duration="95.0") as ctx:
            issues = _validate_output(
                out, edl, has_speech=False, resolution=(3840, 2160), ctx=ctx
            )
        dur_issues = [i for i in issues if i["check"] == "duration"]
        assert len(dur_issues) == 0

    def test_zero_probe_duration_is_error(self, tmp_path: Path):
        """ffprobe returning 0 for duration should be a hard error."""
        out = tmp_path / "bad.mp4"
        out.write_bytes(b"\x00" * 2048)
        edl = _make_edl()
        with _patch_validation(duration="0") as ctx:
            issues = _validate_output(
                out, edl, has_speech=False, resolution=(3840, 2160), ctx=ctx
            )
        dur_issues = [i for i in issues if i["check"] == "duration"]
        assert len(dur_issues) == 1
        assert dur_issues[0]["level"] == "error"


class TestValidateOutputStreams:
    """Tests for video/audio stream presence."""

    def test_no_video_stream_is_error(self, tmp_path: Path):
        """Missing video stream should be a critical error."""
        out = tmp_path / "nostream.mp4"
        out.write_bytes(b"\x00" * 2048)
        edl = _make_edl()
        with _patch_validation(streams="aac,audio,60.0\n") as ctx:
            issues = _validate_output(
                out, edl, has_speech=False, resolution=(3840, 2160), ctx=ctx
            )
        stream_issues = [i for i in issues if i["check"] == "streams"]
        assert len(stream_issues) == 1
        assert stream_issues[0]["level"] == "error"

    def test_no_audio_with_speech_is_warning(self, tmp_path: Path):
        """Missing audio when speech was expected should be a warning."""
        out = tmp_path / "noaudio.mp4"
        out.write_bytes(b"\x00" * 2048)
        edl = _make_edl()
        with _patch_validation(streams="hevc,video,60.0\n") as ctx:
            issues = _validate_output(
                out, edl, has_speech=True, resolution=(3840, 2160), ctx=ctx
            )
        audio_issues = [i for i in issues if i["check"] == "streams"]
        assert len(audio_issues) == 1
        assert audio_issues[0]["level"] == "warning"

    def test_no_audio_without_speech_no_warning(self, tmp_path: Path):
        """Missing audio when no speech expected should not warn about speech."""
        out = tmp_path / "videoonly.mp4"
        out.write_bytes(b"\x00" * 2048)
        edl = _make_edl()
        with _patch_validation(streams="hevc,video,60.0\n") as ctx:
            issues = _validate_output(
                out, edl, has_speech=False, resolution=(3840, 2160), ctx=ctx
            )
        audio_speech = [i for i in issues if i["check"] == "streams"]
        assert len(audio_speech) == 0

    def test_no_audio_with_music_is_warning(self, tmp_path: Path):
        """Missing audio when music configured should warn."""
        out = tmp_path / "noaudio_music.mp4"
        out.write_bytes(b"\x00" * 2048)
        # Create a fake music file so the path check passes
        music_file = tmp_path / "music.mp3"
        music_file.write_bytes(b"\x00" * 100)
        edl = _make_edl(music_file=str(music_file))
        with _patch_validation(streams="hevc,video,60.0\n") as ctx:
            issues = _validate_output(
                out, edl, has_speech=False, resolution=(3840, 2160), ctx=ctx
            )
        music_issues = [i for i in issues if i["check"] == "streams"]
        assert len(music_issues) == 1
        assert music_issues[0]["level"] == "warning"


class TestValidateOutputResolution:
    """Tests for resolution check."""

    def test_matching_resolution_passes(self, tmp_path: Path):
        """Output matching expected resolution should pass."""
        out = tmp_path / "ok.mp4"
        out.write_bytes(b"\x00" * 2048)
        edl = _make_edl()
        with _patch_validation() as ctx:
            issues = _validate_output(
                out, edl, has_speech=False, resolution=(3840, 2160), ctx=ctx
            )
        res_issues = [i for i in issues if i["check"] == "resolution"]
        assert len(res_issues) == 0

    def test_mismatched_resolution_warns(self, tmp_path: Path):
        """Output with different resolution should warn."""
        out = tmp_path / "wrongres.mp4"
        out.write_bytes(b"\x00" * 2048)
        edl = _make_edl()
        with _patch_validation(dimensions="1920x1080") as ctx:
            issues = _validate_output(
                out, edl, has_speech=False, resolution=(3840, 2160), ctx=ctx
            )
        res_issues = [i for i in issues if i["check"] == "resolution"]
        assert len(res_issues) == 1
        assert res_issues[0]["level"] == "warning"
        assert "1920x1080" in res_issues[0]["message"]


class TestValidateOutputAllPassing:
    """Test that a well-formed output produces no issues."""

    def test_all_checks_pass(self, tmp_path: Path):
        """Fully valid output should return zero issues."""
        out = tmp_path / "perfect.mp4"
        out.write_bytes(b"\x00" * 2048)
        edl = _make_edl(duration=60.0)
        with _patch_validation(
            streams="hevc,video,58.0\naac,audio,58.0\n",
            duration="58.0",
            dimensions="3840x2160",
        ) as ctx:
            issues = _validate_output(
                out, edl, has_speech=True, resolution=(3840, 2160), ctx=ctx
            )
        assert len(issues) == 0
