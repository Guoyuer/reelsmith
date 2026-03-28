"""Tests for pipeline.prepare — EXIF extraction, rerun behavior, preview generation."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch


class TestReadExif:
    """Test EXIF extraction from photos."""

    def test_returns_dict_for_jpeg(self, tmp_path):
        from pipeline.prepare._prepare import _read_exif

        img_path = tmp_path / "photo.jpg"
        from PIL import Image

        img = Image.new("RGB", (100, 100), "red")
        img.save(img_path, "JPEG")

        exif = _read_exif(img_path)
        assert isinstance(exif, dict)
        # PIL-generated images have no EXIF, so dict should be empty
        assert len(exif) == 0

    def test_returns_empty_dict_when_exif_missing(self, tmp_path):
        from pipeline.prepare._prepare import _read_exif

        img_path = tmp_path / "photo.png"
        from PIL import Image

        img = Image.new("RGB", (100, 100))
        img.save(img_path, "PNG")

        exif = _read_exif(img_path)
        assert isinstance(exif, dict)
        assert len(exif) == 0

    def test_returns_empty_dict_for_nonexistent_file(self, tmp_path):
        from pipeline.prepare._prepare import _read_exif

        exif = _read_exif(tmp_path / "nonexistent.jpg")
        assert exif == {}


class TestPrepareFullFlow:
    """Test prepare() end-to-end with minimal data."""

    def test_rerun_updates_analysis(self, tmp_path):
        """Re-running prepare should overwrite analysis.json."""
        from pipeline.config import Config
        from pipeline.prepare import PrepareConfig, prepare

        cfg = Config(workspace=tmp_path / "runs" / "test")
        cfg.ensure_dirs()
        cfg.media_dir.mkdir(parents=True, exist_ok=True)

        from PIL import Image

        photo = cfg.media_dir / "photo.jpg"
        Image.new("RGB", (100, 100), "red").save(photo, "JPEG")

        manifest = [
            {
                "local_path": str(photo),
                "item_type": 0,
                "takentime": 1700000000,
                "taken_at": "2023-11-14T14:13:20+00:00",
                "filesize": 5000,
            }
        ]
        cfg.manifest_path.write_text(json.dumps(manifest))

        prepare(cfg, PrepareConfig())
        assert cfg.analysis_path.exists()
        first_mtime = cfg.analysis_path.stat().st_mtime

        import time

        time.sleep(0.05)
        prepare(cfg, PrepareConfig())
        assert cfg.analysis_path.stat().st_mtime > first_mtime


class TestGenerateVideoPreview:
    """Test video preview generation."""

    def test_skips_existing_preview(self, tmp_path):
        from pipeline._types import cache_id
        from pipeline.prepare._prepare import _generate_video_previews

        preview_dir = tmp_path / "previews"
        preview_dir.mkdir()

        local_path = "/fake/video.mp4"
        # Pre-create preview
        preview = preview_dir / f"preview_{cache_id(local_path)}.mp4"
        preview.write_bytes(b"\x00" * 1000)

        video_items = [{"local_path": local_path, "video_duration": 30}]
        with patch("pipeline.prepare._prepare.run_subprocess") as mock_run:
            _generate_video_previews(video_items, preview_dir)
        # Should not call ffmpeg since preview exists
        mock_run.assert_not_called()

    def test_force_deletes_existing(self, tmp_path):
        from pipeline._types import cache_id
        from pipeline.prepare._prepare import _generate_video_previews

        preview_dir = tmp_path / "previews"
        preview_dir.mkdir()

        local_path = "/fake/video.mp4"
        preview = preview_dir / f"preview_{cache_id(local_path)}.mp4"
        preview.write_bytes(b"\x00" * 1000)

        video_items = [{"local_path": local_path, "video_duration": 30}]

        def fake_run(cmd, **kwargs):
            # Create a fake output file
            for i, c in enumerate(cmd):
                if str(c).endswith(".mp4") and i > 0:
                    Path(c).write_bytes(b"\x00" * 500)
            result = MagicMock()
            result.returncode = 0
            return result

        with patch("pipeline.prepare._prepare.run_subprocess", side_effect=fake_run):
            with patch(
                "pipeline.prepare._prepare._has_dense_keyframes", return_value=False
            ):
                _generate_video_previews(video_items, preview_dir, force=True)
