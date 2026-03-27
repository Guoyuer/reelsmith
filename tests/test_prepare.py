"""Tests for pipeline.prepare — analysis caching, integration."""

from __future__ import annotations

import json
from pathlib import Path

from pipeline.prepare import load_analysis, prepare

# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------


def _make_item(
    item_id: int,
    filename: str = "IMG_{:04d}.jpg",
    takentime: int = 1700000000,
    district: str | None = None,
    country: str | None = None,
    first_level: str | None = None,
    filesize: int = 5000000,
) -> dict:
    """Create a manifest item with sensible defaults."""
    fname = filename.format(item_id) if "{" in filename else filename
    return {
        "id": item_id,
        "item_type": 0,
        "takentime": takentime,
        "taken_at": "2025-01-01T00:00:00+00:00",
        "local_path": f"/fake/media/{item_id}_{fname}",
        "filesize": filesize,
        "district": district,
        "country": country,
        "first_level": first_level,
    }


# -----------------------------------------------------------------------
# Analysis caching (migrated from test_analyze.py)
# -----------------------------------------------------------------------


def _make_tiny_image(path: Path, size=(160, 90)) -> Path:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, color=(100, 150, 200))
    img.save(path, "JPEG")
    return path


def _write_manifest(cfg, items: list[dict]) -> None:
    for item in items:
        item.setdefault("takentime", 1700000000)
        item.setdefault("item_type", 0)
    cfg.manifest_path.write_text(json.dumps(items))


def _make_analysis_item(item_id: int, filename: str, local_path: str, **extra) -> dict:
    item = {
        "id": item_id,
        "local_path": local_path,
        "item_type": 0,
        "taken_at": "2025-01-01T00:00:00+00:00",
        "takentime": 1735689600,
    }
    item.update(extra)
    return item


class TestAnalysisCaching:
    def test_all_items_analyzed(self, mock_config):
        cfg = mock_config
        img = _make_tiny_image(cfg.media_dir / "100_photo.jpg")
        _write_manifest(cfg, [_make_analysis_item(100, "photo.jpg", str(img))])
        prepare(cfg)
        results = load_analysis(cfg)
        assert len(results) == 1

    def test_resumes_from_cache(self, mock_config):
        cfg = mock_config
        img = _make_tiny_image(cfg.media_dir / "101_photo.jpg")
        _write_manifest(cfg, [_make_analysis_item(101, "photo.jpg", str(img))])
        (cfg.cache_dir / "101.json").write_text(
            json.dumps({"thumbnail_path": "/cached/thumb.jpg"})
        )
        prepare(cfg)
        results = load_analysis(cfg)
        assert results[0]["thumbnail_path"] == "/cached/thumb.jpg"

    def test_saves_to_shared_cache(self, mock_config):
        cfg = mock_config
        img = _make_tiny_image(cfg.media_dir / "103_photo.jpg")
        _write_manifest(cfg, [_make_analysis_item(103, "photo.jpg", str(img))])
        prepare(cfg)
        cache_file = cfg.cache_dir / "103.json"
        assert cache_file.exists()
        assert "thumbnail_path" in json.loads(cache_file.read_text())

    def test_exif_from_cache_used(self, mock_config):
        cfg = mock_config
        img = _make_tiny_image(cfg.media_dir / "201_photo.jpg")
        _write_manifest(cfg, [_make_analysis_item(201, "photo.jpg", str(img))])
        cache_data = {
            "thumbnail_path": "/fake/thumb.jpg",
            "exif": {"focal_length": 24.0, "aperture": 1.4, "iso_speed": 100},
        }
        (cfg.cache_dir / "201.json").write_text(json.dumps(cache_data))
        prepare(cfg)
        results = load_analysis(cfg)
        assert results[0].get("exif") == {
            "focal_length": 24.0,
            "aperture": 1.4,
            "iso_speed": 100,
        }

    def test_progress_callback(self, mock_config):
        cfg = mock_config
        img1 = _make_tiny_image(cfg.media_dir / "109_a.jpg")
        img2 = _make_tiny_image(cfg.media_dir / "110_b.jpg")
        _write_manifest(
            cfg,
            [
                _make_analysis_item(109, "a.jpg", str(img1), takentime=1700000000),
                _make_analysis_item(110, "b.jpg", str(img2), takentime=1700000100),
            ],
        )
        calls = []
        prepare(cfg, progress_callback=lambda c, t, n: calls.append((c, t, n)))
        scan_calls = [c for c in calls if c[2] == "scan"]
        photo_calls = [c for c in calls if c[2] == "photos"]
        assert len(scan_calls) == 2
        assert scan_calls[0][1] == 2  # total = 2 items
        assert len(photo_calls) == 2
        assert photo_calls[0][1] == 2  # total = 2 photos
