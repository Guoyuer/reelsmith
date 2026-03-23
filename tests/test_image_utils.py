"""Tests for pipeline.image_utils — thumbnail generation and HEIC support."""

from __future__ import annotations

from PIL import Image


class TestGenerateThumbnail:
    def test_creates_thumbnail(self, tmp_path):
        from pipeline.image_utils import generate_thumbnail

        src = tmp_path / "photo.jpg"
        Image.new("RGB", (4000, 3000), "blue").save(src, "JPEG")

        thumb_dir = tmp_path / "thumbs"
        thumb_dir.mkdir()

        result = generate_thumbnail(src, thumb_dir, size=400)
        assert result.exists()
        assert result.suffix == ".jpg"
        assert "_thumb" in result.name

        img = Image.open(result)
        assert max(img.size) <= 400

    def test_skips_existing_thumbnail(self, tmp_path):
        from pipeline.image_utils import generate_thumbnail

        src = tmp_path / "photo.jpg"
        Image.new("RGB", (100, 100), "red").save(src, "JPEG")

        thumb_dir = tmp_path / "thumbs"
        thumb_dir.mkdir()

        # Create thumbnail first
        result1 = generate_thumbnail(src, thumb_dir, size=400)
        mtime1 = result1.stat().st_mtime

        # Call again — should skip
        import time

        time.sleep(0.01)
        result2 = generate_thumbnail(src, thumb_dir, size=400)
        assert result2.stat().st_mtime == mtime1

    def test_handles_heic(self, tmp_path):
        from pipeline.image_utils import generate_thumbnail

        # Create a fake HEIC (just test the path logic — actual conversion tested elsewhere)
        src = tmp_path / "photo.heic"
        # Write a valid JPEG with .heic extension (PIL can't create real HEIC)
        Image.new("RGB", (100, 100), "green").save(src, "JPEG")

        thumb_dir = tmp_path / "thumbs"
        thumb_dir.mkdir()

        # This will try to open as HEIC, fall through to PIL, and succeed
        # since it's actually JPEG data
        result = generate_thumbnail(src, thumb_dir, size=400)
        assert result.exists()
