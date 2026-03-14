"""Tests for pipeline.assemble — portrait-aware rendering helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pipeline.assemble import (
    _build_portrait_photo_filter,
    _build_portrait_video_filter,
    _is_portrait,
    _probe_dimensions,
    _render_photo,
    _render_video,
)
from pipeline.edl import EditItem


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
            out_w=3840, out_h=2160, frames=240, fps=60, zoom_rate=0.001,
        )
        assert "split" in fc
        assert "gblur" in fc
        assert "overlay" in fc
        assert "zoompan" in fc

    def test_portrait_photo_filter_output_resolution(self):
        """The zoompan s= parameter must match the requested output dimensions."""
        fc = _build_portrait_photo_filter(
            out_w=1920, out_h=1080, frames=120, fps=30, zoom_rate=0.002,
        )
        assert "s=1920x1080" in fc
        assert "fps=30" in fc


class TestBuildPortraitVideoFilter:
    def test_portrait_video_filter_structure(self):
        """Filter must contain split, gblur, and overlay stages."""
        fc = _build_portrait_video_filter(out_w=3840, out_h=2160)
        assert "split" in fc
        assert "gblur" in fc
        assert "overlay" in fc

    def test_portrait_video_filter_no_pad(self):
        """Portrait video filter must NOT use pad (no black bars)."""
        fc = _build_portrait_video_filter(out_w=3840, out_h=2160)
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

        with patch("pipeline.assemble.subprocess.run", return_value=fake_result):
            w, h = _probe_dimensions(Path("/fake/video.mp4"))
        assert (w, h) == (3840, 2160)

    def test_probe_dimensions_handles_failure(self):
        """On failure (empty output), should return (0, 0)."""
        fake_result = MagicMock()
        fake_result.stdout = ""
        fake_result.returncode = 1

        with patch("pipeline.assemble.subprocess.run", return_value=fake_result):
            w, h = _probe_dimensions(Path("/fake/bad.mp4"))
        assert (w, h) == (0, 0)


class TestHeicConversion:
    def test_heic_conversion_uses_sips(self, tmp_path: Path):
        """HEIC files should be converted via sips, not ffmpeg directly."""
        heic_file = tmp_path / "photo.heic"
        heic_file.write_bytes(b"\x00" * 100)
        out_file = tmp_path / "clip.mp4"

        calls = []

        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = "0x0\n"  # probe will return 0,0
            result.stderr = ""
            # Simulate sips creating the jpeg
            if cmd[0] == "sips":
                jpeg_path = Path(cmd[-1])
                jpeg_path.write_bytes(b"\xff\xd8" + b"\x00" * 98)
            return result

        item = EditItem(
            source_file=str(heic_file),
            media_type="photo",
            display_duration=3.0,
        )

        with patch("pipeline.assemble.subprocess.run", side_effect=mock_run), \
             patch("pipeline.media_utils.subprocess.run", side_effect=mock_run):
            _render_photo(item, out_file, 3840, 2160, 60)

        sips_calls = [c for c in calls if c[0] == "sips"]
        assert len(sips_calls) == 1, "sips should be called exactly once for HEIC conversion"
        assert sips_calls[0][3] == "jpeg"


# -----------------------------------------------------------------------
# Integration tests (require FFmpeg installed)
# -----------------------------------------------------------------------


@pytest.mark.integration
class TestRenderLandscapePhoto:
    def test_render_landscape_photo_correct_dims(
        self, tiny_landscape_image: Path, tmp_path: Path,
    ):
        """Landscape photo should render to the target resolution."""
        out = tmp_path / "landscape_clip.mp4"
        item = EditItem(
            source_file=str(tiny_landscape_image),
            media_type="photo",
            display_duration=2.0,
            effect="static",
        )
        _render_photo(item, out, 320, 180, 10)
        assert out.exists(), "Output clip should be created"

        w, h = _probe_dimensions(out)
        assert (w, h) == (320, 180)


@pytest.mark.integration
class TestRenderPortraitPhoto:
    def test_render_portrait_photo_correct_dims(
        self, tiny_portrait_image: Path, tmp_path: Path,
    ):
        """Portrait photo should render to the target resolution (not cropped)."""
        out = tmp_path / "portrait_clip.mp4"
        item = EditItem(
            source_file=str(tiny_portrait_image),
            media_type="photo",
            display_duration=2.0,
        )
        _render_photo(item, out, 320, 180, 10)
        assert out.exists(), "Output clip should be created"

        w, h = _probe_dimensions(out)
        assert (w, h) == (320, 180)

    def test_render_portrait_photo_no_black_bars(
        self, tiny_portrait_image: Path, tmp_path: Path,
    ):
        """Portrait photo with blurred BG should not have black bars on left/right edges."""
        out = tmp_path / "portrait_noblack.mp4"
        item = EditItem(
            source_file=str(tiny_portrait_image),
            media_type="photo",
            display_duration=2.0,
        )
        _render_photo(item, out, 320, 180, 10)
        assert out.exists()

        # Extract first frame as PNG
        frame_path = tmp_path / "frame.png"
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(out),
                "-vframes", "1", "-f", "image2",
                str(frame_path),
            ],
            capture_output=True, check=True,
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
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", "color=c=red:s=90x160:d=1",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                str(portrait_vid),
            ],
            capture_output=True, check=True,
        )

        out = tmp_path / "portrait_vid_rendered.mp4"
        item = EditItem(
            source_file=str(portrait_vid),
            media_type="video",
            display_duration=1.0,
        )
        _render_video(item, out, 320, 180, 10)
        assert out.exists(), "Rendered video should exist"

        # Extract a frame and check edges
        frame_path = tmp_path / "vid_frame.png"
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(out),
                "-vframes", "1", "-f", "image2",
                str(frame_path),
            ],
            capture_output=True, check=True,
        )
        from PIL import Image

        frame = Image.open(frame_path)
        pixels = frame.load()

        mid_y = frame.height // 2
        left_pixel = pixels[0, mid_y]
        right_pixel = pixels[frame.width - 1, mid_y]

        assert left_pixel != (0, 0, 0), f"Left edge pixel is black: {left_pixel}"
        assert right_pixel != (0, 0, 0), f"Right edge pixel is black: {right_pixel}"
