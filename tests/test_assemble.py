"""Tests for pipeline.assemble — portrait-aware rendering helpers and output validation."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pipeline.assemble._assemble import _validate_output
from pipeline.assemble._encoder import RenderContext
from pipeline.assemble._filters import build_portrait_photo_filter as _build_portrait_photo_filter
from pipeline.assemble._filters import is_portrait as _is_portrait
from pipeline.assemble._filters import portrait_bg_filter
from pipeline.assemble._render import render_photo as _render_photo
from pipeline.assemble._render import render_video as _render_video
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


class TestBuildPortraitPhotoFilter:
    def test_portrait_photo_filter_structure(self):
        """Filter must contain split, gblur, overlay, and zoompan stages."""
        fc = _build_portrait_photo_filter(
            out_w=3840,
            out_h=2160,
            frames=240,
            fps=60,
            zoom_rate=0.001,
        )
        assert "split" in fc
        assert "gblur" in fc
        assert "overlay" in fc
        assert "zoompan" in fc

    def test_portrait_photo_filter_output_resolution(self):
        """The zoompan s= parameter must match the requested output dimensions."""
        fc = _build_portrait_photo_filter(
            out_w=1920,
            out_h=1080,
            frames=120,
            fps=30,
            zoom_rate=0.002,
        )
        assert "s=1920x1080" in fc
        assert "fps=30" in fc


class TestBuildPortraitVideoFilter:
    def test_portrait_video_filter_structure(self):
        """Filter must contain split, gblur, and overlay stages."""
        fc = portrait_bg_filter(3840, 2160)
        assert "split" in fc
        assert "gblur" in fc
        assert "overlay" in fc

    def test_portrait_video_filter_no_pad(self):
        """Portrait video filter must NOT use pad (no black bars)."""
        fc = portrait_bg_filter(3840, 2160)
        assert "pad=" not in fc


# -----------------------------------------------------------------------
# Mocked tests (no real FFmpeg invocation)
# -----------------------------------------------------------------------


class TestProbeDimensions:
    def test_probe_dimensions_parses_output(self):
        """Should parse 'WxH' output from ffprobe into (int, int)."""
        fake_result = MagicMock()
        fake_result.stdout = "3840x2160\n"
        fake_result.returncode = 0

        ctx = RenderContext()
        with patch("pipeline.assemble._encoder.run_subprocess", return_value=fake_result):
            w, h = ctx.probe_dimensions(Path("/fake/video.mp4"))
        assert (w, h) == (3840, 2160)

    def test_probe_dimensions_handles_failure(self):
        """On failure (empty output), should return (0, 0)."""
        fake_result = MagicMock()
        fake_result.stdout = ""
        fake_result.returncode = 1

        ctx = RenderContext()
        with patch("pipeline.assemble._encoder.run_subprocess", return_value=fake_result):
            w, h = ctx.probe_dimensions(Path("/fake/bad.mp4"))
        assert (w, h) == (0, 0)


class TestHeicConversion:
    def test_heic_conversion_called_for_heic(self, tmp_path: Path):
        """HEIC files should be converted to JPEG before rendering."""
        heic_file = tmp_path / "photo.heic"
        heic_file.write_bytes(b"\x00" * 100)
        out_file = tmp_path / "clip.mp4"

        convert_calls = []

        def mock_convert(source, dest_dir=None):
            convert_calls.append(source)
            jpeg = (dest_dir or source.parent) / f"_converted_{source.stem}.jpg"
            jpeg.write_bytes(b"\xff\xd8" + b"\x00" * 98)
            return jpeg

        def mock_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = "0x0\n"
            result.stderr = ""
            return result

        item = EditItem(
            source_file=str(heic_file),
            media_type="photo",
            display_duration=3.0,
        )

        ctx = RenderContext(w=3840, h=2160, fps=60)
        with (
            patch("pipeline.assemble._render.convert_heic", side_effect=mock_convert),
            patch("pipeline.assemble._render.run_subprocess", side_effect=mock_run),
            patch("pipeline.assemble._encoder.run_subprocess", side_effect=mock_run),
        ):
            _render_photo(item, out_file, ctx=ctx)

        assert len(convert_calls) == 1, "convert_heic should be called for HEIC files"


# -----------------------------------------------------------------------
# Integration tests (require FFmpeg installed)
# -----------------------------------------------------------------------


@pytest.mark.integration
class TestRenderLandscapePhoto:
    def test_render_landscape_photo_correct_dims(
        self,
        tiny_landscape_image: Path,
        tmp_path: Path,
    ):
        """Landscape photo should render to the target resolution."""
        out = tmp_path / "landscape_clip.mp4"
        item = EditItem(
            source_file=str(tiny_landscape_image),
            media_type="photo",
            display_duration=2.0,
            effect="static",
        )
        ctx = RenderContext(w=320, h=180, fps=10)
        _render_photo(item, out, ctx=ctx)
        assert out.exists(), "Output clip should be created"

        w, h = ctx.probe_dimensions(out)
        assert (w, h) == (320, 180)


@pytest.mark.integration
class TestRenderPortraitPhoto:
    def test_render_portrait_photo_correct_dims(
        self,
        tiny_portrait_image: Path,
        tmp_path: Path,
    ):
        """Portrait photo should render to the target resolution (not cropped)."""
        out = tmp_path / "portrait_clip.mp4"
        item = EditItem(
            source_file=str(tiny_portrait_image),
            media_type="photo",
            display_duration=2.0,
        )
        ctx = RenderContext(w=320, h=180, fps=10)
        _render_photo(item, out, ctx=ctx)
        assert out.exists(), "Output clip should be created"

        w, h = ctx.probe_dimensions(out)
        assert (w, h) == (320, 180)

    def test_render_portrait_photo_no_black_bars(
        self,
        tiny_portrait_image: Path,
        tmp_path: Path,
    ):
        """Portrait photo with blurred BG should not have black bars on left/right edges."""
        out = tmp_path / "portrait_noblack.mp4"
        item = EditItem(
            source_file=str(tiny_portrait_image),
            media_type="photo",
            display_duration=2.0,
        )
        ctx = RenderContext(w=320, h=180, fps=10)
        _render_photo(item, out, ctx=ctx)
        assert out.exists()

        # Extract first frame as PNG
        frame_path = tmp_path / "frame.png"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(out),
                "-vframes",
                "1",
                "-f",
                "image2",
                str(frame_path),
            ],
            capture_output=True,
            check=True,
        )
        from PIL import Image

        frame = Image.open(frame_path)
        pixels = frame.load()

        # Check left and right edge pixels at the vertical center — they should
        # NOT be black (0, 0, 0) because the blurred background fills them.
        mid_y = frame.height // 2
        left_pixel = pixels[0, mid_y]
        right_pixel = pixels[frame.width - 1, mid_y]

        assert left_pixel != (0, 0, 0), f"Left edge pixel is black: {left_pixel}"
        assert right_pixel != (0, 0, 0), f"Right edge pixel is black: {right_pixel}"


@pytest.mark.integration
class TestRenderPortraitVideo:
    def test_render_portrait_video_no_black_bars(self, tmp_path: Path):
        """Portrait video should use blurred BG, not black-bar padding."""
        # Create a tiny portrait video using ffmpeg lavfi
        portrait_vid = tmp_path / "portrait.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=red:s=90x160:d=1",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                str(portrait_vid),
            ],
            capture_output=True,
            check=True,
        )

        out = tmp_path / "portrait_vid_rendered.mp4"
        item = EditItem(
            source_file=str(portrait_vid),
            media_type="video",
            display_duration=1.0,
        )
        ctx = RenderContext(w=320, h=180, fps=10)
        _render_video(item, out, ctx=ctx)
        assert out.exists(), "Rendered video should exist"

        # Extract a frame and check edges
        frame_path = tmp_path / "vid_frame.png"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(out),
                "-vframes",
                "1",
                "-f",
                "image2",
                str(frame_path),
            ],
            capture_output=True,
            check=True,
        )
        from PIL import Image

        frame = Image.open(frame_path)
        pixels = frame.load()

        mid_y = frame.height // 2
        left_pixel = pixels[0, mid_y]
        right_pixel = pixels[frame.width - 1, mid_y]

        assert left_pixel != (0, 0, 0), f"Left edge pixel is black: {left_pixel}"
        assert right_pixel != (0, 0, 0), f"Right edge pixel is black: {right_pixel}"


# -----------------------------------------------------------------------
# Output validation tests (mocked -- no FFmpeg needed)
# -----------------------------------------------------------------------


def _make_edl(duration: float = 60.0, music_file: str = "") -> EDL:
    """Helper to build a minimal EDL for validation tests."""
    music = MusicTrack(file=music_file) if music_file else None
    return EDL(
        title="Test",
        target_duration=duration,
        segments=[
            Segment(
                name="Seg1",
                items=[
                    EditItem(source_file="a.jpg", media_type="photo", display_duration=duration / 2),
                    EditItem(source_file="b.jpg", media_type="photo", display_duration=duration / 2),
                ],
                transition="cut",
            ),
        ],
        intro_style="none",
        outro_style="none",
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
        if "codec_type,codec_name,duration" in cmd_str:
            result.stdout = streams
        elif "format=duration" in cmd_str:
            result.stdout = duration + "\n"
        elif "stream=width,height" in cmd_str:
            result.stdout = dimensions + "\n"
        else:
            result.stdout = ""
        return result

    return _side_effect


from contextlib import contextmanager


@contextmanager
def _patch_validation(**kwargs):
    """Patch both assemble and encoder run_subprocess for validation tests.

    Yields a fresh RenderContext so validation can probe dimensions/duration.
    """
    mock_fn = _mock_subprocess_for_validation(**kwargs)
    with (
        patch("pipeline.assemble._assemble.run_subprocess", side_effect=mock_fn),
        patch("pipeline.assemble._encoder.run_subprocess", side_effect=mock_fn),
        patch("pipeline.media_utils.run_subprocess", side_effect=mock_fn),
    ):
        yield RenderContext()


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
        assert issues[0]["check"] == "file_exists"

    def test_empty_file_returns_error(self, tmp_path: Path):
        """Output file smaller than 1KB should produce a file_size error."""
        out = tmp_path / "tiny.mp4"
        out.write_bytes(b"\x00" * 500)
        edl = _make_edl()
        issues = _validate_output(out, edl, has_speech=False, resolution=(3840, 2160))
        assert len(issues) == 1
        assert issues[0]["level"] == "error"
        assert issues[0]["check"] == "file_size"

    def test_valid_file_no_early_return(self, tmp_path: Path):
        """A file >1KB should not trigger the early-return error path."""
        out = tmp_path / "ok.mp4"
        out.write_bytes(b"\x00" * 2048)
        edl = _make_edl()
        with _patch_validation() as ctx:
            issues = _validate_output(out, edl, has_speech=False, resolution=(3840, 2160), ctx=ctx)
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
            issues = _validate_output(out, edl, has_speech=False, resolution=(3840, 2160), ctx=ctx)
        dur_issues = [i for i in issues if i["check"] == "duration"]
        assert len(dur_issues) == 1
        assert dur_issues[0]["level"] == "error"
        assert "truncation" in dur_issues[0]["message"]

    def test_duration_between_50_and_80pct_is_warning(self, tmp_path: Path):
        """Duration between 50-80% of expected should produce a warning."""
        out = tmp_path / "medium.mp4"
        out.write_bytes(b"\x00" * 2048)
        edl = _make_edl(duration=100.0)  # expected ~100s
        with _patch_validation(duration="70.0") as ctx:
            issues = _validate_output(out, edl, has_speech=False, resolution=(3840, 2160), ctx=ctx)
        dur_issues = [i for i in issues if i["check"] == "duration"]
        assert len(dur_issues) == 1
        assert dur_issues[0]["level"] == "warning"

    def test_duration_above_80pct_passes(self, tmp_path: Path):
        """Duration >=80% of expected should not produce any duration issue."""
        out = tmp_path / "good.mp4"
        out.write_bytes(b"\x00" * 2048)
        edl = _make_edl(duration=100.0)
        with _patch_validation(duration="95.0") as ctx:
            issues = _validate_output(out, edl, has_speech=False, resolution=(3840, 2160), ctx=ctx)
        dur_issues = [i for i in issues if i["check"] == "duration"]
        assert len(dur_issues) == 0

    def test_zero_probe_duration_is_error(self, tmp_path: Path):
        """ffprobe returning 0 for duration should be a hard error."""
        out = tmp_path / "bad.mp4"
        out.write_bytes(b"\x00" * 2048)
        edl = _make_edl()
        with _patch_validation(duration="0") as ctx:
            issues = _validate_output(out, edl, has_speech=False, resolution=(3840, 2160), ctx=ctx)
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
            issues = _validate_output(out, edl, has_speech=False, resolution=(3840, 2160), ctx=ctx)
        stream_issues = [i for i in issues if i["check"] == "video_stream"]
        assert len(stream_issues) == 1
        assert stream_issues[0]["level"] == "error"

    def test_no_audio_with_speech_is_warning(self, tmp_path: Path):
        """Missing audio when speech was expected should be a warning."""
        out = tmp_path / "noaudio.mp4"
        out.write_bytes(b"\x00" * 2048)
        edl = _make_edl()
        with _patch_validation(streams="hevc,video,60.0\n") as ctx:
            issues = _validate_output(out, edl, has_speech=True, resolution=(3840, 2160), ctx=ctx)
        audio_issues = [i for i in issues if i["check"] == "audio_stream"]
        assert len(audio_issues) == 1
        assert audio_issues[0]["level"] == "warning"

    def test_no_audio_without_speech_no_warning(self, tmp_path: Path):
        """Missing audio when no speech expected should not warn about speech."""
        out = tmp_path / "videoonly.mp4"
        out.write_bytes(b"\x00" * 2048)
        edl = _make_edl()
        with _patch_validation(streams="hevc,video,60.0\n") as ctx:
            issues = _validate_output(out, edl, has_speech=False, resolution=(3840, 2160), ctx=ctx)
        audio_speech = [i for i in issues if i["check"] == "audio_stream"]
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
            issues = _validate_output(out, edl, has_speech=False, resolution=(3840, 2160), ctx=ctx)
        music_issues = [i for i in issues if i["check"] == "audio_stream_music"]
        assert len(music_issues) == 1
        assert music_issues[0]["level"] == "warning"


class TestValidateOutputCodec:
    """Tests for video codec validation."""

    def test_expected_codec_passes(self, tmp_path: Path):
        """hevc codec should not trigger a warning."""
        out = tmp_path / "ok.mp4"
        out.write_bytes(b"\x00" * 2048)
        edl = _make_edl()
        with _patch_validation(streams="hevc,video,60.0\naac,audio,60.0\n") as ctx:
            issues = _validate_output(out, edl, has_speech=False, resolution=(3840, 2160), ctx=ctx)
        codec_issues = [i for i in issues if i["check"] == "video_codec"]
        assert len(codec_issues) == 0

    def test_unexpected_codec_warns(self, tmp_path: Path):
        """An unexpected video codec should produce a warning."""
        out = tmp_path / "weird.mp4"
        out.write_bytes(b"\x00" * 2048)
        edl = _make_edl()
        with _patch_validation(streams="vp9,video,60.0\naac,audio,60.0\n") as ctx:
            issues = _validate_output(out, edl, has_speech=False, resolution=(3840, 2160), ctx=ctx)
        codec_issues = [i for i in issues if i["check"] == "video_codec"]
        assert len(codec_issues) == 1
        assert codec_issues[0]["level"] == "warning"


class TestValidateOutputAVSync:
    """Tests for audio-video sync spot check."""

    def test_large_av_drift_warns(self, tmp_path: Path):
        """Audio longer than video by >5s should warn (actual sync issue)."""
        out = tmp_path / "drifted.mp4"
        out.write_bytes(b"\x00" * 2048)
        edl = _make_edl()
        with _patch_validation(vid_stream_dur="52.0", aud_stream_dur="60.0") as ctx:
            issues = _validate_output(out, edl, has_speech=True, resolution=(3840, 2160), ctx=ctx)
        sync_issues = [i for i in issues if i["check"] == "av_sync"]
        assert len(sync_issues) == 1
        assert "longer" in sync_issues[0]["message"]

    def test_small_av_drift_passes(self, tmp_path: Path):
        """<5s drift should not trigger a sync warning."""
        out = tmp_path / "synced.mp4"
        out.write_bytes(b"\x00" * 2048)
        edl = _make_edl()
        with _patch_validation(vid_stream_dur="60.0", aud_stream_dur="58.0") as ctx:
            issues = _validate_output(out, edl, has_speech=False, resolution=(3840, 2160), ctx=ctx)
        sync_issues = [i for i in issues if i["check"] == "av_sync"]
        assert len(sync_issues) == 0


class TestValidateOutputResolution:
    """Tests for resolution check."""

    def test_matching_resolution_passes(self, tmp_path: Path):
        """Output matching expected resolution should pass."""
        out = tmp_path / "ok.mp4"
        out.write_bytes(b"\x00" * 2048)
        edl = _make_edl()
        with _patch_validation() as ctx:
            issues = _validate_output(out, edl, has_speech=False, resolution=(3840, 2160), ctx=ctx)
        res_issues = [i for i in issues if i["check"] == "resolution"]
        assert len(res_issues) == 0

    def test_mismatched_resolution_warns(self, tmp_path: Path):
        """Output with different resolution should warn."""
        out = tmp_path / "wrongres.mp4"
        out.write_bytes(b"\x00" * 2048)
        edl = _make_edl()
        with _patch_validation(dimensions="1920x1080") as ctx:
            issues = _validate_output(out, edl, has_speech=False, resolution=(3840, 2160), ctx=ctx)
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
            streams="hevc,video,58.0\naac,audio,58.0\n", duration="58.0", dimensions="3840x2160"
        ) as ctx:
            issues = _validate_output(out, edl, has_speech=True, resolution=(3840, 2160), ctx=ctx)
        assert len(issues) == 0


# -----------------------------------------------------------------------
# RenderReport
# -----------------------------------------------------------------------


class TestRenderReport:
    def test_empty_report(self):
        from pipeline.assemble import RenderReport

        r = RenderReport()
        assert r.ok_count == 0
        assert "0/0 OK" in r.summary()

    def test_all_ok(self):
        from pipeline.assemble import ClipStatus, RenderReport

        r = RenderReport(
            clips=[
                ClipStatus("c1", "a.jpg", "ok"),
                ClipStatus("c2", "b.jpg", "ok"),
            ]
        )
        assert r.ok_count == 2
        assert "2/2 OK" in r.summary()

    def test_mixed_status(self):
        from pipeline.assemble import ClipStatus, RenderReport

        r = RenderReport(
            clips=[
                ClipStatus("c1", "a.jpg", "ok"),
                ClipStatus("c2", "b.jpg", "skipped", "source not found"),
                ClipStatus("c3", "c.jpg", "failed", "timeout"),
            ]
        )
        assert r.ok_count == 1
        assert r.skipped_count == 1
        assert r.failed_count == 1
        assert "timeout" in r.summary()
