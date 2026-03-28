"""Tests for pipeline.fetch_local — local folder scanning and manifest generation."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline.fetch._local import (
    _extract_date,
    _parse_date_from_filename,
    fetch_local,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_fake_image(path: Path) -> None:
    path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)


def _create_fake_video(path: Path) -> None:
    path.write_bytes(b"\x00\x00\x00\x1c\x66\x74\x79\x70" + b"\x00" * 100)



# ---------------------------------------------------------------------------
# Test: _parse_date_from_filename
# ---------------------------------------------------------------------------


class TestParseDateFromFilename:
    @pytest.mark.parametrize(
        "filename, expected",
        [
            (
                "87462_20250617_191756",
                datetime(2025, 6, 17, 19, 17, 56, tzinfo=timezone.utc),
            ),
            ("20250617_191756", datetime(2025, 6, 17, 19, 17, 56, tzinfo=timezone.utc)),
            (
                "IMG20250613085912",
                datetime(2025, 6, 13, 8, 59, 12, tzinfo=timezone.utc),
            ),
            (
                "DJI_20250613120415_0072_D",
                datetime(2025, 6, 13, 12, 4, 15, tzinfo=timezone.utc),
            ),
            (
                "2025-06-13_12-04-15",
                datetime(2025, 6, 13, 12, 4, 15, tzinfo=timezone.utc),
            ),
            (
                "2025-06-13T12-04-15",
                datetime(2025, 6, 13, 12, 4, 15, tzinfo=timezone.utc),
            ),
            (
                "IMG_20250617_191756",
                datetime(2025, 6, 17, 19, 17, 56, tzinfo=timezone.utc),
            ),
        ],
        ids=[
            "id_prefix_with_date_time",
            "date_time_no_prefix",
            "img_continuous_digits",
            "dji_drone",
            "iso_underscore",
            "iso_t_separator",
            "img_prefix_underscore",
        ],
    )
    def test_parses_date(self, filename, expected):
        assert _parse_date_from_filename(filename) == expected

    @pytest.mark.parametrize(
        "filename",
        [
            "IMG_001",
            "Screenshot_20231114",
            "20259913120415",
        ],
        ids=["no_date", "date_only_no_time", "invalid_month"],
    )
    def test_returns_none(self, filename):
        assert _parse_date_from_filename(filename) is None


# ---------------------------------------------------------------------------
# Test: _extract_date fallback chain
# ---------------------------------------------------------------------------


class TestExtractDateFallback:
    def test_photo_falls_through_to_filename(self, tmp_path):
        """When PIL EXIF fails, photos fall back to filename date parsing."""
        photo = tmp_path / "IMG_20250613_120415.jpg"
        _create_fake_image(photo)
        assert _extract_date(photo) == datetime(
            2025, 6, 13, 12, 4, 15, tzinfo=timezone.utc
        )

    def test_video_falls_through_to_filename(self, tmp_path):
        video = tmp_path / "VID_20250613_120415.mp4"
        _create_fake_video(video)
        with patch(
            "pipeline.utils.media.run_subprocess", side_effect=Exception("no ffprobe")
        ):
            assert _extract_date(video) == datetime(
                2025, 6, 13, 12, 4, 15, tzinfo=timezone.utc
            )

    def test_video_ffprobe_success_parses_date(self, tmp_path):
        """Successful ffprobe should parse ISO datetime from creation_time."""
        from unittest.mock import MagicMock

        video = tmp_path / "clip.mp4"
        _create_fake_video(video)
        mock_result = MagicMock()
        mock_result.stdout = "2025-06-13T12:04:15.000000Z\n"
        with patch("pipeline.utils.media.run_subprocess", return_value=mock_result):
            dt = _extract_date(video)
        assert dt == datetime(2025, 6, 13, 12, 4, 15, tzinfo=timezone.utc)

    def test_no_date_anywhere_returns_none(self, tmp_path):
        photo = tmp_path / "random_photo.jpg"
        _create_fake_image(photo)
        assert _extract_date(photo) is None


# ---------------------------------------------------------------------------
# Test: file filtering
# ---------------------------------------------------------------------------


class TestFileFiltering:
    def test_unsupported_extensions_skipped(self, mock_config, source_dir):
        (source_dir / "notes.txt").write_text("hello")
        (source_dir / "data.json").write_text("{}")
        _create_fake_image(source_dir / "photo.jpg")

        with patch("pipeline.fetch._local._extract_date", return_value=None):
            result = fetch_local(mock_config, str(source_dir))

        assert len(result) == 1


class TestReverseGeocode:
    def test_singapore_coords(self):
        try:
            import reverse_geocode

            loc = reverse_geocode.get((1.2897, 103.8501))
            assert loc["city"] == "Singapore"
            assert loc["country"] == "Singapore"
        except ImportError:
            pytest.skip("reverse_geocode not installed")

    def test_no_gps_no_location(self, mock_config, source_dir):
        _create_fake_image(source_dir / "no_gps.jpg")
        with patch("pipeline.fetch._local._extract_date", return_value=None):
            with patch("pipeline.fetch._local._extract_gps", return_value=(None, None)):
                result = fetch_local(mock_config, str(source_dir))
        assert "city" not in result[0]
