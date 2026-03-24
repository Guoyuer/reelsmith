"""Tests for pipeline.prepare — video analysis, audio probing, EXIF extraction."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch


class TestReadExif:
    """Test EXIF extraction from photos."""

    def test_returns_dict_for_jpeg(self, tmp_path):
        from pipeline.prepare import _read_exif

        img_path = tmp_path / "photo.jpg"
        from PIL import Image

        img = Image.new("RGB", (100, 100), "red")
        img.save(img_path, "JPEG")

        exif = _read_exif(img_path)
        assert isinstance(exif, dict)
        # PIL-generated images have no EXIF, so dict should be empty
        assert len(exif) == 0

    def test_returns_empty_dict_when_exif_missing(self, tmp_path):
        from pipeline.prepare import _read_exif

        img_path = tmp_path / "photo.png"
        from PIL import Image

        img = Image.new("RGB", (100, 100))
        img.save(img_path, "PNG")

        exif = _read_exif(img_path)
        assert isinstance(exif, dict)
        assert len(exif) == 0

    def test_returns_empty_dict_for_nonexistent_file(self, tmp_path):
        from pipeline.prepare import _read_exif

        exif = _read_exif(tmp_path / "nonexistent.jpg")
        assert exif == {}


class TestPrepareVideo:
    """Test _prepare_video video analysis."""

    def test_probes_video_metadata(self, tmp_path):
        from pipeline.prepare import _prepare_video

        entry = {
            "id": 1,
            "filename": "video.mp4",
            "local_path": str(tmp_path / "video.mp4"),
        }
        (tmp_path / "video.mp4").write_bytes(b"\x00" * 100)

        # Mock ffprobe to return video metadata
        def fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            cmd_str = " ".join(str(c) for c in cmd)
            if "show_streams" in cmd_str or "show_format" in cmd_str:
                result.stdout = json.dumps(
                    {
                        "streams": [
                            {
                                "codec_type": "video",
                                "width": 1920,
                                "height": 1080,
                                "r_frame_rate": "30/1",
                                "duration": "45.5",
                            }
                        ],
                        "format": {"duration": "45.5"},
                    }
                )
            return result

        cache_file = tmp_path / "cache" / "1.json"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        with patch("pipeline.prepare.run_subprocess", side_effect=fake_run):
            with patch("pipeline.prepare._has_dense_keyframes", return_value=True):
                _prepare_video(entry, 1, str(tmp_path / "video.mp4"), cache_file, 1, 1)

        assert (
            entry.get("video_duration") is not None
            or entry.get("video_width") is not None
        )


class TestPrepareFullFlow:
    """Test prepare() end-to-end with minimal data."""

    def test_prepare_with_photos_only(self, tmp_path):
        from pipeline.config import Config
        from pipeline.prepare import PrepareConfig, prepare

        cfg = Config(workspace=tmp_path)
        cfg.ensure_dirs()

        # Create manifest
        from PIL import Image

        photo = cfg.media_dir / "photo.jpg"
        Image.new("RGB", (100, 100), "red").save(photo, "JPEG")

        manifest = [
            {
                "id": 1,
                "filename": "photo.jpg",
                "local_path": str(photo),
                "item_type": 0,
                "takentime": 1700000000,
                "taken_iso": "2023-11-14T14:13:20+00:00",
                "filesize": 5000,
                "metadata": {"persons": []},
            }
        ]
        cfg.manifest_path.write_text(json.dumps(manifest))

        prepare(cfg, PrepareConfig())
        assert cfg.preprocessed_path.exists()
        # Per-item cache should exist
        assert (cfg.cache_dir / "1.json").exists()

    def test_force_regenerates_cache(self, tmp_path):
        """--force should regenerate per-item caches."""
        from pipeline.config import Config
        from pipeline.prepare import PrepareConfig, prepare

        cfg = Config(workspace=tmp_path)
        cfg.ensure_dirs()

        from PIL import Image

        photo = cfg.media_dir / "photo.jpg"
        Image.new("RGB", (100, 100), "red").save(photo, "JPEG")

        manifest = [
            {
                "id": 1,
                "filename": "photo.jpg",
                "local_path": str(photo),
                "item_type": 0,
                "takentime": 1700000000,
                "taken_iso": "2023-11-14T14:13:20+00:00",
                "filesize": 5000,
                "metadata": {"persons": []},
            }
        ]
        cfg.manifest_path.write_text(json.dumps(manifest))

        # First prepare
        prepare(cfg, PrepareConfig())
        cache_file = cfg.cache_dir / "1.json"
        assert cache_file.exists()
        first_mtime = cache_file.stat().st_mtime

        # Second prepare with --force
        import time

        time.sleep(0.05)
        prepare(cfg, PrepareConfig(force=True))
        second_mtime = cache_file.stat().st_mtime
        assert second_mtime > first_mtime


class TestHasDenseKeyframes:
    """Test keyframe interval detection."""

    def test_dense_keyframes_returns_true(self):
        from pipeline.prepare import _has_dense_keyframes

        fake = MagicMock()
        # Keyframes at 0, 1, 2, 3 seconds (1s interval = dense)
        fake.stdout = "0.000000,K_\n1.000000,K_\n2.000000,K_\n3.000000,K_\n"
        with patch("pipeline.prepare.run_subprocess", return_value=fake):
            assert _has_dense_keyframes("/fake/video.mp4") is True

    def test_sparse_keyframes_returns_false(self):
        from pipeline.prepare import _has_dense_keyframes

        fake = MagicMock()
        # Keyframes at 0, 5, 10 seconds (5s interval = sparse)
        fake.stdout = "0.000000,K_\n5.000000,K_\n10.000000,K_\n"
        with patch("pipeline.prepare.run_subprocess", return_value=fake):
            assert _has_dense_keyframes("/fake/video.mp4") is False

    def test_too_few_keyframes_returns_false(self):
        from pipeline.prepare import _has_dense_keyframes

        fake = MagicMock()
        fake.stdout = "0.000000,K_\n"
        with patch("pipeline.prepare.run_subprocess", return_value=fake):
            assert _has_dense_keyframes("/fake/video.mp4") is False

    def test_probe_failure_returns_false(self):
        from pipeline.prepare import _has_dense_keyframes

        with patch(
            "pipeline.prepare.run_subprocess",
            side_effect=RuntimeError("ffprobe failed"),
        ):
            assert _has_dense_keyframes("/fake/video.mp4") is False


class TestGenerateVideoPreview:
    """Test video preview generation."""

    def test_skips_existing_preview(self, tmp_path):
        from pipeline.prepare import _generate_video_previews

        preview_dir = tmp_path / "previews"
        preview_dir.mkdir()

        # Pre-create preview
        preview = preview_dir / "preview_123.mp4"
        preview.write_bytes(b"\x00" * 1000)

        video_items = [
            {"id": 123, "local_path": "/fake/video.mp4", "video_duration": 30}
        ]
        with patch("pipeline.prepare.run_subprocess") as mock_run:
            _generate_video_previews(video_items, preview_dir)
        # Should not call ffmpeg since preview exists
        mock_run.assert_not_called()

    def test_force_deletes_existing(self, tmp_path):
        from pipeline.prepare import _generate_video_previews

        preview_dir = tmp_path / "previews"
        preview_dir.mkdir()

        preview = preview_dir / "preview_123.mp4"
        preview.write_bytes(b"\x00" * 1000)

        video_items = [
            {"id": 123, "local_path": "/fake/video.mp4", "video_duration": 30}
        ]

        def fake_run(cmd, **kwargs):
            # Create a fake output file
            for i, c in enumerate(cmd):
                if str(c).endswith(".mp4") and i > 0:
                    Path(c).write_bytes(b"\x00" * 500)
            result = MagicMock()
            result.returncode = 0
            return result

        with patch("pipeline.prepare.run_subprocess", side_effect=fake_run):
            with patch("pipeline.prepare._has_dense_keyframes", return_value=False):
                _generate_video_previews(video_items, preview_dir, force=True)
