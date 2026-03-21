"""Tests for pipeline.analyze — thumbnail/keyframe generation and caching."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from pipeline.prepare import prepare as _prepare


def analyze(cfg, **kwargs):
    """Wrapper: calls prepare() and returns analysis results."""
    _prepare(cfg, **kwargs)
    return json.loads((cfg.workspace / "analysis.json").read_text())


def _make_tiny_image(path: Path, size=(160, 90)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, color=(100, 150, 200))
    img.save(path, "JPEG")
    return path


def _write_manifest(cfg, items: list[dict]) -> None:
    """Write items as manifest.json (prepare reads this, not preprocessed.json)."""
    # Add fields that prepare expects from manifest
    for item in items:
        item.setdefault("metadata", {"persons": []})
        item.setdefault("takentime", 1700000000)
        item.setdefault("item_type", 0)
    (cfg.workspace / "manifest.json").write_text(json.dumps(items))


def _make_item(item_id: int, filename: str, local_path: str,
               family_count: int = 0, **extra) -> dict:
    item = {
        "id": item_id,
        "filename": filename,
        "local_path": local_path,
        "family_count": family_count,
        "item_type": 0,
    }
    item.update(extra)
    return item


class TestAnalyzeIncludesAllItems:
    def test_all_items_analyzed(self, mock_config):
        """All items are analyzed regardless of family_count."""
        cfg = mock_config
        img = _make_tiny_image(cfg.media_dir / "100_photo.jpg")
        items = [_make_item(100, "photo.jpg", str(img))]
        _write_manifest(cfg, items)

        analyze(cfg)
        results = json.loads((cfg.workspace / "analysis.json").read_text())
        assert len(results) == 1


class TestAnalyzeResumesFromExisting:
    def test_analyze_resumes_from_existing(self, mock_config):
        cfg = mock_config
        img = _make_tiny_image(cfg.media_dir / "101_photo.jpg")
        items = [_make_item(101, "photo.jpg", str(img), family_count=2)]
        _write_manifest(cfg, items)

        existing = [{"id": 101, "filename": "photo.jpg", "local_path": str(img),
                     "vision": {"description": "already analyzed"}}]
        (cfg.workspace / "analysis.json").write_text(json.dumps(existing))

        analyze(cfg)
        results = json.loads((cfg.workspace / "analysis.json").read_text())
        assert len(results) == 1
        assert results[0]["vision"]["description"] == "already analyzed"


class TestAnalyzeUsesSharedCache:
    def test_analyze_uses_shared_cache(self, mock_config):
        cfg = mock_config
        img = _make_tiny_image(cfg.media_dir / "102_photo.jpg")
        items = [_make_item(102, "photo.jpg", str(img), family_count=1)]
        _write_manifest(cfg, items)

        cache_entry = {"thumbnail_path": "/fake/thumb.jpg"}
        (cfg.cache_dir / "102.json").write_text(json.dumps(cache_entry))

        analyze(cfg)
        results = json.loads((cfg.workspace / "analysis.json").read_text())
        assert len(results) == 1
        assert results[0]["thumbnail_path"] == "/fake/thumb.jpg"


class TestAnalyzeSavesToSharedCache:
    def test_analyze_saves_to_shared_cache(self, mock_config):
        cfg = mock_config
        img = _make_tiny_image(cfg.media_dir / "103_photo.jpg")
        items = [_make_item(103, "photo.jpg", str(img), family_count=2)]
        _write_manifest(cfg, items)

        analyze(cfg)

        cache_file = cfg.cache_dir / "103.json"
        assert cache_file.exists()
        cached = json.loads(cache_file.read_text())
        assert "thumbnail_path" in cached


class TestAnalyzeExifCaching:
    def test_exif_cached_in_analysis(self, mock_config):
        """EXIF data should be cached in the per-file cache and in results."""
        cfg = mock_config
        img_path = cfg.media_dir / "200_photo.jpg"
        from PIL import Image
        img = Image.new("RGB", (100, 100), (200, 100, 50))
        img.save(str(img_path), "JPEG")

        items = [_make_item(200, "photo.jpg", str(img_path), family_count=2)]
        _write_manifest(cfg, items)

        analyze(cfg)
        results = json.loads((cfg.workspace / "analysis.json").read_text())
        assert len(results) == 1

        # Cache should exist with thumbnail
        cache_file = cfg.cache_dir / "200.json"
        assert cache_file.exists()
        cached = json.loads(cache_file.read_text())
        assert "thumbnail_path" in cached

    def test_exif_from_cache_used(self, mock_config):
        """When cache has EXIF data, it should appear in results."""
        cfg = mock_config
        img = _make_tiny_image(cfg.media_dir / "201_photo.jpg")
        items = [_make_item(201, "photo.jpg", str(img), family_count=1)]
        _write_manifest(cfg, items)

        # Pre-populate cache with EXIF
        cache_data = {
            "thumbnail_path": "/fake/thumb.jpg",
            "exif": {"focal_length": 24.0, "aperture": 1.4, "iso": 100},
        }
        (cfg.cache_dir / "201.json").write_text(json.dumps(cache_data))

        analyze(cfg)
        results = json.loads((cfg.workspace / "analysis.json").read_text())
        assert results[0].get("exif") == {"focal_length": 24.0, "aperture": 1.4, "iso": 100}


class TestAnalyzeProgressCallback:
    def test_analyze_progress_callback(self, mock_config):
        cfg = mock_config
        img1 = _make_tiny_image(cfg.media_dir / "109_a.jpg")
        img2 = _make_tiny_image(cfg.media_dir / "110_b.jpg")
        items = [
            _make_item(109, "a.jpg", str(img1), family_count=2),
            _make_item(110, "b.jpg", str(img2), family_count=1),
        ]
        _write_manifest(cfg, items)

        callback_args = []
        analyze(cfg, progress_callback=lambda c, t, n: callback_args.append((c, t, n)))

        assert len(callback_args) == 2
        assert callback_args[0][1] == 2  # total
        assert callback_args[1][1] == 2


