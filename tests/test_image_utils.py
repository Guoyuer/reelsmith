"""Tests for pipeline.utils.image — thumbnail generation and HEIC support."""

from __future__ import annotations

from PIL import Image

from tests.helpers import make_jpeg


class TestGenerateThumbnail:
    def test_creates_thumbnail(self, tmp_path):
        from pipeline.utils.image import generate_thumbnail

        src = tmp_path / "photo.jpg"
        make_jpeg(src, size=(4000, 3000), color="blue")

        thumb_dir = tmp_path / "thumbs"
        thumb_dir.mkdir()

        result = generate_thumbnail(src, thumb_dir, size=400)
        assert result.exists()
        assert result.suffix == ".jpg"
        assert "_thumb" in result.name

        img = Image.open(result)
        assert max(img.size) <= 400

    def test_skips_existing_thumbnail(self, tmp_path):
        from pipeline.utils.image import generate_thumbnail

        src = tmp_path / "photo.jpg"
        make_jpeg(src)

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

    def test_same_stem_different_paths_do_not_collide(self, tmp_path):
        from pipeline.utils.image import generate_thumbnail

        src_a = tmp_path / "a" / "photo.jpg"
        src_b = tmp_path / "b" / "photo.jpg"
        make_jpeg(src_a)
        make_jpeg(src_b, color="blue")

        thumb_dir = tmp_path / "thumbs"
        result_a = generate_thumbnail(src_a, thumb_dir, size=400)
        result_b = generate_thumbnail(src_b, thumb_dir, size=400)

        assert result_a != result_b
        assert result_a.exists()
        assert result_b.exists()

    def test_handles_heic(self, tmp_path):
        from pipeline.utils.image import generate_thumbnail

        # Create a fake HEIC (just test the path logic — actual conversion tested elsewhere)
        src = tmp_path / "photo.heic"
        # Write a valid JPEG with .heic extension (PIL can't create real HEIC)
        make_jpeg(src, color="green")

        thumb_dir = tmp_path / "thumbs"
        thumb_dir.mkdir()

        # This will try to open as HEIC, fall through to PIL, and succeed
        # since it's actually JPEG data
        result = generate_thumbnail(src, thumb_dir, size=400)
        assert result.exists()
