"""Tests for pipeline.fetch_local — local folder scanning and manifest generation."""

from __future__ import annotations

import hashlib
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


def _expected_id(filename: str) -> int:
    """Reproduce the md5-based ID generation from fetch_local."""
    return int(hashlib.md5(filename.encode()).hexdigest()[:8], 16) % (10**8)


def _create_fake_image(path: Path) -> None:
    path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)


def _create_fake_video(path: Path) -> None:
    path.write_bytes(b"\x00\x00\x00\x1c\x66\x74\x79\x70" + b"\x00" * 100)


@pytest.fixture
def mock_config(tmp_path: Path):
    from pipeline.config import Config

    cfg = Config(workspace=tmp_path / "workspace")
    cfg.ensure_dirs()
    return cfg


@pytest.fixture
def source_dir(tmp_path: Path) -> Path:
    src = tmp_path / "media_source"
    src.mkdir()
    return src


# ---------------------------------------------------------------------------
# Test: ID generation is deterministic
# ---------------------------------------------------------------------------


class TestIdDeterminism:
    @pytest.fixture(autouse=True)
    def _mock_date(self):
        with patch("pipeline.fetch._local._extract_date", return_value=None):
            yield

    def test_same_file_same_id(self, mock_config, source_dir):
        _create_fake_image(source_dir / "IMG_001.jpg")
        result1 = fetch_local(mock_config, str(source_dir))
        result2 = fetch_local(mock_config, str(source_dir))
        assert result1[0]["id"] == result2[0]["id"]

    def test_id_matches_md5_formula(self, mock_config, source_dir):
        _create_fake_image(source_dir / "IMG_001.jpg")
        result = fetch_local(mock_config, str(source_dir))
        assert result[0]["id"] == _expected_id("IMG_001.jpg")

    def test_different_files_different_ids(self, mock_config, source_dir):
        _create_fake_image(source_dir / "IMG_001.jpg")
        _create_fake_image(source_dir / "IMG_002.jpg")
        result = fetch_local(mock_config, str(source_dir))
        ids = [r["id"] for r in result]
        assert len(set(ids)) == 2

    def test_id_stable_across_directory_changes(self, mock_config, tmp_path):
        """Same filename in different directories gives the same ID."""
        dir_a = tmp_path / "dir_a"
        dir_b = tmp_path / "dir_b"
        dir_a.mkdir()
        dir_b.mkdir()
        _create_fake_image(dir_a / "IMG_001.jpg")
        _create_fake_image(dir_b / "IMG_001.jpg")
        result_a = fetch_local(mock_config, str(dir_a))
        result_b = fetch_local(mock_config, str(dir_b))
        assert result_a[0]["id"] == result_b[0]["id"]


# ---------------------------------------------------------------------------
# Test: _parse_date_from_filename
# ---------------------------------------------------------------------------


class TestParseDateFromFilename:
    @pytest.mark.parametrize(
        "filename, expected",
        [
            ("87462_20250617_191756", datetime(2025, 6, 17, 19, 17, 56, tzinfo=timezone.utc)),
            ("20250617_191756", datetime(2025, 6, 17, 19, 17, 56, tzinfo=timezone.utc)),
            ("IMG20250613085912", datetime(2025, 6, 13, 8, 59, 12, tzinfo=timezone.utc)),
            ("DJI_20250613120415_0072_D", datetime(2025, 6, 13, 12, 4, 15, tzinfo=timezone.utc)),
            ("2025-06-13_12-04-15", datetime(2025, 6, 13, 12, 4, 15, tzinfo=timezone.utc)),
            ("2025-06-13T12-04-15", datetime(2025, 6, 13, 12, 4, 15, tzinfo=timezone.utc)),
            ("IMG_20250617_191756", datetime(2025, 6, 17, 19, 17, 56, tzinfo=timezone.utc)),
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
        assert _extract_date(photo) == datetime(2025, 6, 13, 12, 4, 15, tzinfo=timezone.utc)

    def test_video_falls_through_to_filename(self, tmp_path):
        video = tmp_path / "VID_20250613_120415.mp4"
        _create_fake_video(video)
        with patch(
            "pipeline.utils.media.run_subprocess", side_effect=Exception("no ffprobe")
        ):
            assert _extract_date(video) == datetime(
                2025, 6, 13, 12, 4, 15, tzinfo=timezone.utc
            )

    def test_no_date_anywhere_returns_none(self, tmp_path):
        photo = tmp_path / "random_photo.jpg"
        _create_fake_image(photo)
        assert _extract_date(photo) is None


# ---------------------------------------------------------------------------
# Test: file filtering
# ---------------------------------------------------------------------------


class TestFileFiltering:
    def test_temp_files_skipped(self, mock_config, source_dir):
        for name in ("_converted_photo.jpg", "_hist_photo.jpg", "_audio_track.mp4", "_resized_photo.jpg"):
            _create_fake_image(source_dir / name)
        _create_fake_image(source_dir / "real_photo.jpg")

        with patch("pipeline.fetch._local._extract_date", return_value=None):
            result = fetch_local(mock_config, str(source_dir))

        assert len(result) == 1
        assert result[0]["filename"] == "real_photo.jpg"

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
