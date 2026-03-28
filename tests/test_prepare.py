"""Tests for pipeline.prepare — analysis caching, integration."""

from __future__ import annotations

import json
from pathlib import Path

from pipeline.prepare import load_analysis, prepare

# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------


def _make_item(
    filename: str = "IMG_0001.jpg",
    takentime: int = 1700000000,
    district: str | None = None,
    country: str | None = None,
    first_level: str | None = None,
    filesize: int = 5000000,
) -> dict:
    """Create a manifest item with sensible defaults."""
    return {
        "item_type": 0,
        "takentime": takentime,
        "taken_at": "2025-01-01T00:00:00+00:00",
        "local_path": f"/fake/media/{filename}",
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


def _make_analysis_item(filename: str, local_path: str, **extra) -> dict:
    item = {
        "local_path": local_path,
        "item_type": 0,
        "taken_at": "2025-01-01T00:00:00+00:00",
        "takentime": 1735689600,
    }
    item.update(extra)
    return item


class TestAnalysis:
    def test_progress_callback(self, mock_config):
        cfg = mock_config
        img1 = _make_tiny_image(cfg.media_dir / "109_a.jpg")
        img2 = _make_tiny_image(cfg.media_dir / "110_b.jpg")
        _write_manifest(
            cfg,
            [
                _make_analysis_item("a.jpg", str(img1), takentime=1700000000),
                _make_analysis_item("b.jpg", str(img2), takentime=1700000100),
            ],
        )
        calls = []
        prepare(cfg, progress_callback=lambda c, t, n: calls.append((c, t, n)))
        photo_calls = [c for c in calls if c[2] == "photos"]
        assert len(photo_calls) == 2
        assert photo_calls[0][1] == 2  # total = 2 items
