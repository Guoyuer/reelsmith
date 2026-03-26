"""Extended tests for pipeline.utils.image — convert_heic edge cases."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from pipeline.utils.image import convert_heic, generate_thumbnail


class TestConvertHeic:
    def test_returns_cached(self, tmp_path):
        """If converted file already exists, skip conversion."""
        source = tmp_path / "photo.heic"
        source.write_bytes(b"\x00" * 100)
        cache = tmp_path / "cache"
        cache.mkdir()
        cached = cache / "_converted_photo.jpg"
        cached.write_bytes(b"\xff\xd8" + b"\x00" * 100)
        result = convert_heic(source, cache_dir=cache)
        assert result == cached

    def test_pillow_heif_path(self, tmp_path):
        """If pillow-heif available, use PIL to convert."""
        source = tmp_path / "photo.heic"
        source.write_bytes(b"\x00" * 100)
        cache = tmp_path / "cache"

        mock_img = MagicMock()

        def _save(path, fmt, **kw):
            Path(path).write_bytes(b"\xff\xd8" + b"\x00" * 50)

        mock_img.save.side_effect = _save

        with patch("PIL.Image.open", return_value=mock_img):
            result = convert_heic(source, cache_dir=cache)
        assert result.exists()

    def test_default_cache_dir(self, tmp_path):
        """When cache_dir=None, uses temp directory."""
        source = tmp_path / "photo.heic"
        source.write_bytes(b"\x00" * 100)

        mock_img = MagicMock()

        def _save(path, fmt, **kw):
            Path(path).write_bytes(b"\xff\xd8")

        mock_img.save.side_effect = _save

        with patch("PIL.Image.open", return_value=mock_img):
            result = convert_heic(source)
            assert "vlog_heic_cache" in str(result)


class TestGenerateThumbnailEdgeCases:
    def test_corrupt_image_returns_none(self, tmp_path):
        """Corrupt image should return None, not crash."""
        src = tmp_path / "bad.jpg"
        src.write_bytes(b"\x00" * 100)
        thumb_dir = tmp_path / "thumbs"
        thumb_dir.mkdir()
        result = generate_thumbnail(src, thumb_dir)
        assert result is None

    def test_creates_output_dir(self, tmp_path):
        """Output dir created if it doesn't exist."""
        from PIL import Image

        src = tmp_path / "photo.jpg"
        Image.new("RGB", (100, 100), "red").save(src, "JPEG")
        thumb_dir = tmp_path / "new_thumbs"
        result = generate_thumbnail(src, thumb_dir)
        assert thumb_dir.exists()
        assert result is not None
