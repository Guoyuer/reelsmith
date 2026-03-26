"""Tests for pipeline.fetch_local — local folder scanning and manifest generation."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline.fetch import FetchConfig
from pipeline.fetch._local import (
    _extract_date,
    _parse_date_from_filename,
    fetch_local,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _expected_id(filename: str) -> int:
    """Reproduce the md5-based ID generation from fetch_local."""
    return int(hashlib.md5(filename.encode()).hexdigest()[:8], 16) % (10**8)


@pytest.fixture
def mock_config(tmp_path: Path):
    from pipeline.config import Config

    cfg = Config(workspace=tmp_path / "workspace")
    cfg.ensure_dirs()
    return cfg


@pytest.fixture
def source_dir(tmp_path: Path) -> Path:
    """Create a source directory with a few fake media files."""
    src = tmp_path / "media_source"
    src.mkdir()
    return src


def _create_fake_image(path: Path) -> None:
    """Create a minimal JPEG file (not valid image data, but has correct extension)."""
    path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)


def _create_fake_video(path: Path) -> None:
    """Create a minimal MP4 file stub."""
    path.write_bytes(b"\x00\x00\x00\x1c\x66\x74\x79\x70" + b"\x00" * 100)


# ---------------------------------------------------------------------------
# Test: ID generation is deterministic
# ---------------------------------------------------------------------------


class TestIdDeterminism:
    def test_same_file_same_id(self, mock_config, source_dir):
        """Same filename produces the same ID across separate calls."""
        _create_fake_image(source_dir / "IMG_001.jpg")

        with patch("pipeline.fetch._local._extract_date", return_value=None):
            result1 = fetch_local(mock_config, FetchConfig(source_dir=str(source_dir)))
            result2 = fetch_local(mock_config, FetchConfig(source_dir=str(source_dir)))

        assert result1[0]["id"] == result2[0]["id"]

    def test_id_matches_md5_formula(self, mock_config, source_dir):
        """ID matches the documented md5(filename)[:8] formula."""
        _create_fake_image(source_dir / "IMG_001.jpg")

        with patch("pipeline.fetch._local._extract_date", return_value=None):
            result = fetch_local(mock_config, FetchConfig(source_dir=str(source_dir)))

        assert result[0]["id"] == _expected_id("IMG_001.jpg")

    def test_different_files_different_ids(self, mock_config, source_dir):
        """Different filenames produce different IDs."""
        _create_fake_image(source_dir / "IMG_001.jpg")
        _create_fake_image(source_dir / "IMG_002.jpg")

        with patch("pipeline.fetch._local._extract_date", return_value=None):
            result = fetch_local(mock_config, FetchConfig(source_dir=str(source_dir)))

        ids = [r["id"] for r in result]
        assert len(set(ids)) == 2, "Expected two distinct IDs"

    def test_id_stable_across_directory_changes(self, mock_config, tmp_path):
        """Same filename in different directories gives the same ID (keyed on name, not path)."""
        dir_a = tmp_path / "dir_a"
        dir_b = tmp_path / "dir_b"
        dir_a.mkdir()
        dir_b.mkdir()
        _create_fake_image(dir_a / "IMG_001.jpg")
        _create_fake_image(dir_b / "IMG_001.jpg")

        with patch("pipeline.fetch._local._extract_date", return_value=None):
            result_a = fetch_local(mock_config, FetchConfig(source_dir=str(dir_a)))
            result_b = fetch_local(mock_config, FetchConfig(source_dir=str(dir_b)))

        assert result_a[0]["id"] == result_b[0]["id"]


# ---------------------------------------------------------------------------
# Test: _parse_date_from_filename
# ---------------------------------------------------------------------------


class TestParseDateFromFilename:
    """Test the filename date extraction for all documented patterns."""

    def test_id_prefix_with_date_time(self):
        """Pattern: 87462_20250617_191756"""
        dt = _parse_date_from_filename("87462_20250617_191756")
        assert dt == datetime(2025, 6, 17, 19, 17, 56, tzinfo=timezone.utc)

    def test_date_time_no_prefix(self):
        """Pattern: 20250617_191756 (no ID prefix)"""
        dt = _parse_date_from_filename("20250617_191756")
        assert dt == datetime(2025, 6, 17, 19, 17, 56, tzinfo=timezone.utc)

    def test_img_continuous_digits(self):
        """Pattern: IMG20250613085912"""
        dt = _parse_date_from_filename("IMG20250613085912")
        assert dt == datetime(2025, 6, 13, 8, 59, 12, tzinfo=timezone.utc)

    def test_dji_drone_filename(self):
        """Pattern: DJI_20250613120415_0072_D"""
        dt = _parse_date_from_filename("DJI_20250613120415_0072_D")
        assert dt == datetime(2025, 6, 13, 12, 4, 15, tzinfo=timezone.utc)

    def test_iso_like_underscore(self):
        """Pattern: 2025-06-13_12-04-15"""
        dt = _parse_date_from_filename("2025-06-13_12-04-15")
        assert dt == datetime(2025, 6, 13, 12, 4, 15, tzinfo=timezone.utc)

    def test_iso_like_t_separator(self):
        """Pattern: 2025-06-13T12-04-15"""
        dt = _parse_date_from_filename("2025-06-13T12-04-15")
        assert dt == datetime(2025, 6, 13, 12, 4, 15, tzinfo=timezone.utc)

    def test_no_date_returns_none(self):
        """Filenames with no date pattern return None."""
        assert _parse_date_from_filename("IMG_001") is None

    def test_date_only_no_time_returns_none(self):
        """8-digit date without time component does not match (needs 14 digits)."""
        assert _parse_date_from_filename("Screenshot_20231114") is None

    def test_invalid_month_returns_none(self):
        """A 14-digit run that forms an invalid date should return None."""
        # month=99 is invalid
        assert _parse_date_from_filename("20259913120415") is None

    def test_img_prefix_with_underscore_date(self):
        """Pattern: IMG_20250617_191756 (common Android pattern)."""
        dt = _parse_date_from_filename("IMG_20250617_191756")
        assert dt == datetime(2025, 6, 17, 19, 17, 56, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Test: _extract_date fallback chain
# ---------------------------------------------------------------------------


class TestExtractDateFallback:
    """Test the _extract_date function's fallback logic."""

    def test_photo_falls_through_to_filename_parsing(self, tmp_path):
        """When PIL EXIF fails, photos should fall back to filename date parsing."""
        # Create a fake JPEG with a date-embedded name but no real EXIF
        photo = tmp_path / "IMG_20250613_120415.jpg"
        _create_fake_image(photo)

        # PIL will fail on this fake image, should fall through to filename
        dt = _extract_date(photo)
        assert dt == datetime(2025, 6, 13, 12, 4, 15, tzinfo=timezone.utc)

    def test_video_falls_through_to_filename(self, tmp_path):
        """When ffprobe fails, videos should fall back to filename date parsing."""
        video = tmp_path / "VID_20250613_120415.mp4"
        _create_fake_video(video)

        with patch(
            "pipeline.utils.media.run_subprocess", side_effect=Exception("no ffprobe")
        ):
            dt = _extract_date(video)

        assert dt == datetime(2025, 6, 13, 12, 4, 15, tzinfo=timezone.utc)

    def test_photo_with_no_date_anywhere_returns_none(self, tmp_path):
        """Photo with no EXIF and no date in filename returns None."""
        photo = tmp_path / "random_photo.jpg"
        _create_fake_image(photo)

        dt = _extract_date(photo)
        assert dt is None


# ---------------------------------------------------------------------------
# Test: file filtering
# ---------------------------------------------------------------------------


class TestFileFiltering:
    """Test that temp files and non-media files are skipped."""

    def test_temp_files_skipped(self, mock_config, source_dir):
        """Files with pipeline temp prefixes are skipped."""
        _create_fake_image(source_dir / "_converted_photo.jpg")
        _create_fake_image(source_dir / "_hist_photo.jpg")
        _create_fake_image(source_dir / "_audio_track.mp4")
        _create_fake_image(source_dir / "_resized_photo.jpg")
        _create_fake_image(source_dir / "real_photo.jpg")

        with patch("pipeline.fetch._local._extract_date", return_value=None):
            result = fetch_local(mock_config, FetchConfig(source_dir=str(source_dir)))

        assert len(result) == 1
        assert result[0]["filename"] == "real_photo.jpg"

    def test_unsupported_extensions_skipped(self, mock_config, source_dir):
        """Non-media files are skipped."""
        (source_dir / "notes.txt").write_text("hello")
        (source_dir / "data.json").write_text("{}")
        _create_fake_image(source_dir / "photo.jpg")

        with patch("pipeline.fetch._local._extract_date", return_value=None):
            result = fetch_local(mock_config, FetchConfig(source_dir=str(source_dir)))

        assert len(result) == 1


class TestReverseGeocode:
    """Test GPS reverse geocoding in fetch_local."""

    def test_singapore_coords(self):
        """Known Singapore coords → city=Singapore."""
        try:
            import reverse_geocode

            loc = reverse_geocode.get((1.2897, 103.8501))
            assert loc["city"] == "Singapore"
            assert loc["country"] == "Singapore"
        except ImportError:
            pytest.skip("reverse_geocode not installed")

    def test_no_gps_no_location(self, mock_config, source_dir):
        """Files without GPS don't get city/country fields."""
        from pipeline.fetch._local import fetch_local

        _create_fake_image(source_dir / "no_gps.jpg")
        with patch("pipeline.fetch._local._extract_date", return_value=None):
            with patch("pipeline.fetch._local._extract_gps", return_value=(None, None)):
                result = fetch_local(
                    mock_config, FetchConfig(source_dir=str(source_dir))
                )
        assert "city" not in result[0]
