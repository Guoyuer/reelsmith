"""Extended tests for pipeline.fetch._local — GPS extraction, manifest output, progress."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pipeline.fetch._local import _extract_gps, fetch_local


@pytest.fixture
def mock_config(tmp_path):
    from pipeline.config import Config

    cfg = Config(workspace=tmp_path / "workspace")
    cfg.ensure_dirs()
    return cfg


@pytest.fixture
def source_dir(tmp_path):
    src = tmp_path / "media"
    src.mkdir()
    return src


def _create_image(path: Path):
    path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)


class TestExtractGps:
    def test_video_returns_none(self, tmp_path):
        vid = tmp_path / "clip.mp4"
        vid.write_bytes(b"\x00" * 100)
        assert _extract_gps(vid) == (None, None)

    def test_no_exif_returns_none(self, tmp_path):
        photo = tmp_path / "photo.jpg"
        _create_image(photo)
        lat, lon = _extract_gps(photo)
        assert lat is None and lon is None

    def test_with_gps_data(self, tmp_path):
        """Mock PIL to return GPS EXIF data."""
        photo = tmp_path / "photo.jpg"
        _create_image(photo)
        mock_img = MagicMock()
        # GPS tag 34853 with lat/lon data
        mock_img._getexif.return_value = {
            34853: {
                1: "N",
                2: (1, 17, 22.92),  # 1°17'22.92"N
                3: "E",
                4: (103, 51, 0.36),  # 103°51'0.36"E
            }
        }
        with patch("PIL.Image.open", return_value=mock_img):
            lat, lon = _extract_gps(photo)
        assert lat is not None
        assert lat == pytest.approx(1.2897, abs=0.001)
        assert lon == pytest.approx(103.8501, abs=0.001)


class TestFetchLocalManifest:
    @pytest.fixture(autouse=True)
    def _mock_date(self):
        with patch("pipeline.fetch._local._extract_date", return_value=None):
            yield

    def test_manifest_written(self, mock_config, source_dir):
        _create_image(source_dir / "photo.jpg")
        fetch_local(mock_config, str(source_dir))
        assert mock_config.manifest_path.exists()

    def test_manifest_fields(self, mock_config, source_dir):
        _create_image(source_dir / "photo.jpg")
        import json

        result = fetch_local(mock_config, str(source_dir))
        assert "id" in result[0]
        assert "filename" in result[0]
        assert "taken_iso" in result[0]
        assert "local_path" in result[0]
        assert "filesize" in result[0]
        # Verify manifest file matches return value
        saved = json.loads(mock_config.manifest_path.read_text())
        assert len(saved) == 1

    def test_video_item_type(self, mock_config, source_dir):
        vid = source_dir / "clip.mp4"
        vid.write_bytes(b"\x00" * 100)
        result = fetch_local(mock_config, str(source_dir))
        assert result[0]["item_type"] == 1

    def test_photo_item_type(self, mock_config, source_dir):
        _create_image(source_dir / "photo.jpg")
        result = fetch_local(mock_config, str(source_dir))
        assert result[0]["item_type"] == 0

    def test_nonexistent_source_raises(self, mock_config):
        with pytest.raises(FileNotFoundError):
            fetch_local(mock_config, "/nonexistent/path")

    def test_progress_callback(self, mock_config, source_dir):
        _create_image(source_dir / "a.jpg")
        _create_image(source_dir / "b.jpg")
        calls = []
        fetch_local(
            mock_config,
            str(source_dir),
            progress_callback=lambda c, t, n: calls.append((c, t, n)),
        )
        assert len(calls) == 2
        assert calls[0][1] == 2  # total = 2

    def test_recursive_scan(self, mock_config, source_dir):
        sub = source_dir / "subdir"
        sub.mkdir()
        _create_image(source_dir / "a.jpg")
        _create_image(sub / "b.jpg")
        result = fetch_local(mock_config, str(source_dir))
        assert len(result) == 2

    def test_multiple_extensions(self, mock_config, source_dir):
        for ext in (".jpg", ".png", ".heic", ".mp4", ".mov"):
            f = source_dir / f"file{ext}"
            f.write_bytes(b"\xff\xd8" + b"\x00" * 100)
        result = fetch_local(mock_config, str(source_dir))
        assert len(result) == 5
