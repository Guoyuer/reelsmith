"""Tests for pipeline.plan._preview — content block building and preview concat."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from pipeline.config import Config
from pipeline.plan._preview import _build_visual_content_blocks, _concat_previews


class TestConcatPreviews:
    """Test video preview concatenation."""

    def test_builds_offset_table(self, tmp_path):
        # Create fake preview files
        entries = []
        clip_durs = [10.0, 11.0, 12.0]
        for i in range(3):
            p = tmp_path / f"preview_{i}.mp4"
            p.write_bytes(b"\x00" * 500)
            entries.append((i + 1, clip_durs[i], p))

        out = tmp_path / "mega.mp4"
        total = sum(clip_durs)

        # probe_duration called per clip (3x) then for mega output (1x)
        probe_returns = clip_durs + [total]
        with patch("pipeline.plan._preview.run_subprocess") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            with patch(
                "pipeline.plan._preview.probe_duration", side_effect=probe_returns
            ):
                offset_table, result_path = _concat_previews(entries, out)

        assert len(offset_table) == 3
        assert offset_table[0][2] == 0.0
        assert offset_table[1][2] == 10.0
        assert offset_table[2][2] == 21.0

    def test_duration_mismatch_raises(self, tmp_path):
        p = tmp_path / "preview.mp4"
        p.write_bytes(b"\x00" * 500)
        entries = [(1, 10.0, p)]
        out = tmp_path / "mega.mp4"

        with patch("pipeline.plan._preview.run_subprocess") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            out.write_bytes(b"\x00" * 100)
            # probe per clip returns 10.0, probe mega returns 1.0 (<50%)
            with patch(
                "pipeline.plan._preview.probe_duration", side_effect=[10.0, 1.0]
            ):
                with pytest.raises(RuntimeError, match="mismatch"):
                    _concat_previews(entries, out)


class TestBuildVisualContentBlocks:
    """Test the full content block builder."""

    def test_builds_blocks_with_photos(self, tmp_path):
        cfg = Config(workspace=tmp_path / "runs" / "test")
        cfg.ensure_dirs()
        cfg.media_dir.mkdir(parents=True, exist_ok=True)

        # Create photo + thumbnail
        photo = cfg.media_dir / "photo.jpg"
        Image.new("RGB", (100, 100), "red").save(photo, "JPEG")
        thumb = cfg.thumbnails_dir / "photo_thumb.jpg"
        Image.new("RGB", (50, 50), "red").save(thumb, "JPEG")

        analysis = {
            str(photo): {
                "local_path": str(photo),
                "media_type": "photo",
            }
        }

        blocks, offset_table, _, _ = _build_visual_content_blocks(analysis, cfg)

        texts = [b for b in blocks if isinstance(b, str)]
        images = [
            b for b in blocks if isinstance(b, dict) and b.get("type") == "image_bytes"
        ]

        assert len(texts) >= 1
        assert len(images) == 1
        assert "#01" in texts[0]

    def test_missing_thumbnail_raises(self, tmp_path):
        cfg = Config(workspace=tmp_path / "runs" / "test")
        cfg.ensure_dirs()
        cfg.media_dir.mkdir(parents=True, exist_ok=True)

        photo = cfg.media_dir / "photo.jpg"
        Image.new("RGB", (100, 100), "red").save(photo, "JPEG")
        # Don't create thumbnail

        analysis = {
            str(photo): {
                "local_path": str(photo),
                "media_type": "photo",
            }
        }

        with pytest.raises(FileNotFoundError, match="Thumbnail missing"):
            _build_visual_content_blocks(analysis, cfg)

    def test_empty_chapter_skipped(self, tmp_path):
        cfg = Config(workspace=tmp_path / "runs" / "test")
        cfg.ensure_dirs()

        with pytest.raises(RuntimeError, match="No photos"):
            _build_visual_content_blocks({}, cfg)

    def test_video_entries_collected(self, tmp_path):
        cfg = Config(workspace=tmp_path / "runs" / "test")
        cfg.ensure_dirs()
        cfg.media_dir.mkdir(parents=True, exist_ok=True)

        # Create photo + thumb + video preview
        photo = cfg.media_dir / "photo.jpg"
        Image.new("RGB", (100, 100), "red").save(photo, "JPEG")
        thumb = cfg.thumbnails_dir / "photo_thumb.jpg"
        Image.new("RGB", (50, 50), "red").save(thumb, "JPEG")

        # Create video file + preview
        from pipeline._types import cache_id

        video = cfg.media_dir / "vid.mp4"
        video.write_bytes(b"\x00" * 500)
        preview = cfg.previews_dir / f"preview_{cache_id(str(video))}.mp4"
        preview.write_bytes(b"\x00" * 1000)

        analysis = {
            str(photo): {
                "local_path": str(photo),
                "media_type": "photo",
            },
            str(video): {
                "local_path": str(video),
                "media_type": "video",
                "video_duration": 30.0,
            },
        }

        with patch("pipeline.plan._preview._concat_previews") as mock_concat:
            mock_concat.return_value = ([(2, 30.0, 0.0)], preview)
            blocks, offset_table, _, _ = _build_visual_content_blocks(analysis, cfg)

        # Should have text + image + video preview instruction + video bytes
        videos = [
            b for b in blocks if isinstance(b, dict) and b.get("type") == "video_bytes"
        ]
        assert len(videos) == 1
        assert len(offset_table) == 1
