"""Extended tests for pipeline.utils.image — generate_thumbnail edge cases."""

from __future__ import annotations

from pipeline.utils.image import generate_thumbnail
from tests.helpers import make_jpeg


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
        src = tmp_path / "photo.jpg"
        make_jpeg(src)
        thumb_dir = tmp_path / "new_thumbs"
        result = generate_thumbnail(src, thumb_dir)
        assert thumb_dir.exists()
        assert result is not None
