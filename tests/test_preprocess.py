"""Tests for pipeline.prepare — family detection, timeline, analysis caching, integration."""

from __future__ import annotations

import json
import os
from datetime import timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline.config import Config
from pipeline.prepare import PrepareConfig, _build_timeline, _detect_family, prepare as preprocess

# Fixed timezone for deterministic tests (UTC)
_UTC = timezone.utc


# -----------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------


def _make_item(
    item_id: int,
    filename: str = "IMG_{:04d}.jpg",
    takentime: int = 1700000000,
    persons: list[str] | None = None,
    district: str | None = None,
    country: str | None = None,
    first_level: str | None = None,
    filesize: int = 5000000,
) -> dict:
    """Create a manifest item with sensible defaults."""
    fname = filename.format(item_id) if "{" in filename else filename
    return {
        "id": item_id,
        "filename": fname,
        "item_type": 0,
        "takentime": takentime,
        "taken_iso": "2025-01-01T00:00:00+00:00",
        "local_path": f"/fake/media/{item_id}_{fname}",
        "filesize": filesize,
        "district": district,
        "country": country,
        "first_level": first_level,
        "metadata": {"persons": persons or []},
    }


# -----------------------------------------------------------------------
# Family count tests
# -----------------------------------------------------------------------


class TestFamilyCount:
    """Test family_count assignment in prepare()."""

    def _run_preprocess(self, items: list[dict], tmp_path: Path) -> dict:
        """Write manifest and run preprocess, returning the result."""
        ws = tmp_path / "workspace"
        manifest_path = ws / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(items))

        with patch.dict(os.environ, {}, clear=True), \
             patch("pipeline.config.load_dotenv"):
            cfg = Config.load(workspace=str(ws))
        preprocess(cfg, PrepareConfig(family_names=["Alice", "Bob"]))
        # Read the analysis.json to check items
        analysis = json.loads((ws / "analysis.json").read_text())
        return analysis

    def test_two_family_members(self, tmp_path: Path):
        """Item with 2 family persons gets family_count=2."""
        items = [_make_item(1, persons=["Alice", "Bob"])]
        analysis = self._run_preprocess(items, tmp_path)
        assert analysis[0]["family_count"] == 2

    def test_one_family_member(self, tmp_path: Path):
        """Item with 1 family person gets family_count=1."""
        items = [_make_item(1, persons=["Alice"])]
        analysis = self._run_preprocess(items, tmp_path)
        assert analysis[0]["family_count"] == 1

    def test_no_family(self, tmp_path: Path):
        """Item with no family persons gets family_count=0."""
        items = [_make_item(1, persons=["Stranger"])]
        analysis = self._run_preprocess(items, tmp_path)
        assert analysis[0]["family_count"] == 0

    def test_no_persons(self, tmp_path: Path):
        """Item with empty persons gets family_count=0."""
        items = [_make_item(1, persons=[])]
        analysis = self._run_preprocess(items, tmp_path)
        assert analysis[0]["family_count"] == 0


# -----------------------------------------------------------------------
# _detect_family tests
# -----------------------------------------------------------------------


class TestDetectFamily:
    def test_returns_top_persons(self):
        """Most frequently appearing persons should be detected as family."""
        items = [
            _make_item(i, persons=["Alice", "Bob"]) for i in range(20)
        ] + [_make_item(99, persons=["Stranger"])]
        result = _detect_family(items)
        assert "Alice" in result
        assert "Bob" in result
        assert "Stranger" not in result

    def test_empty_manifest(self):
        assert _detect_family([]) == []

    def test_respects_top_n(self):
        items = [_make_item(i, persons=[f"P{i % 6}"]) for i in range(60)]
        result = _detect_family(items, top_n=3)
        assert len(result) <= 3


# -----------------------------------------------------------------------
# _build_timeline tests
# -----------------------------------------------------------------------


class TestBuildTimeline:
    def test_groups_by_day(self):
        """Items on different days produce separate timeline entries."""
        base = 1700000000
        items = [
            {"id": 1, "takentime": base, "family_count": 2, "district": "Marina Bay"},
            {"id": 2, "takentime": base + 86400, "family_count": 1, "district": "Orchard"},
        ]
        timeline = _build_timeline(items, tz=_UTC)
        assert len(timeline) == 2
        dates = [d["date"] for d in timeline]
        assert dates == sorted(dates)

    def test_groups_by_time_block_and_location(self):
        """Items in the same day but different locations get separate chapters."""
        base = 1700000000
        items = [
            {"id": 1, "takentime": base, "family_count": 2, "district": "Marina Bay"},
            {"id": 2, "takentime": base + 60, "family_count": 1, "district": "Chinatown"},
        ]
        timeline = _build_timeline(items, tz=_UTC)
        assert len(timeline) == 1
        assert len(timeline[0]["chapters"]) == 2

    def test_items_without_takentime_skipped(self):
        """Items missing takentime are excluded from the timeline."""
        items = [
            {"id": 1, "takentime": None, "family_count": 2},
            {"id": 2, "family_count": 1},  # no takentime key
        ]
        timeline = _build_timeline(items, tz=_UTC)
        assert len(timeline) == 0

    def test_chapter_has_item_ids(self):
        """Chapters should contain item_ids list."""
        base = 1700000000
        items = [
            {"id": 1, "takentime": base, "family_count": 2, "district": "Marina Bay"},
            {"id": 2, "takentime": base + 60, "family_count": 1, "district": "Marina Bay"},
        ]
        timeline = _build_timeline(items, tz=_UTC)
        chapter = timeline[0]["chapters"][0]
        assert "item_ids" in chapter
        assert set(chapter["item_ids"]) == {1, 2}


# -----------------------------------------------------------------------
# Full preprocess() integration test
# -----------------------------------------------------------------------


class TestPreprocessIntegration:
    def test_writes_preprocessed_json(self, tmp_path: Path, sample_manifest: list[dict]):
        """preprocess() writes preprocessed.json with expected structure."""
        ws = tmp_path / "workspace"
        ws.mkdir(parents=True)
        (ws / "manifest.json").write_text(json.dumps(sample_manifest))

        with patch.dict(os.environ, {}, clear=True), \
             patch("pipeline.config.load_dotenv"):
            cfg = Config.load(workspace=str(ws))

        result = preprocess(cfg, PrepareConfig(family_names=["Alice", "Bob"]))

        out_path = ws / "preprocessed.json"
        assert out_path.exists()

        saved = json.loads(out_path.read_text())
        assert saved["family_names"] == ["Alice", "Bob"]
        assert "timeline" in saved
        assert len(saved["timeline"]) >= 1


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
        item.setdefault("metadata", {"persons": []})
        item.setdefault("takentime", 1700000000)
        item.setdefault("item_type", 0)
    cfg.manifest_path.write_text(json.dumps(items))


def _make_analysis_item(item_id: int, filename: str, local_path: str, **extra) -> dict:
    item = {
        "id": item_id, "filename": filename, "local_path": local_path,
        "family_count": 0, "item_type": 0,
        "taken_iso": "2025-01-01T00:00:00+00:00", "takentime": 1735689600,
        "metadata": {"persons": []},
    }
    item.update(extra)
    return item


class TestAnalysisCaching:
    def test_all_items_analyzed(self, mock_config):
        cfg = mock_config
        img = _make_tiny_image(cfg.media_dir / "100_photo.jpg")
        _write_manifest(cfg, [_make_analysis_item(100, "photo.jpg", str(img))])
        preprocess(cfg)
        results = json.loads(cfg.analysis_path.read_text())
        assert len(results) == 1

    def test_resumes_from_existing(self, mock_config):
        cfg = mock_config
        img = _make_tiny_image(cfg.media_dir / "101_photo.jpg")
        _write_manifest(cfg, [_make_analysis_item(101, "photo.jpg", str(img))])
        existing = [{"id": 101, "filename": "photo.jpg", "local_path": str(img),
                     "vision": {"description": "already analyzed"}}]
        cfg.analysis_path.write_text(json.dumps(existing))
        preprocess(cfg)
        results = json.loads(cfg.analysis_path.read_text())
        assert results[0]["vision"]["description"] == "already analyzed"

    def test_uses_shared_cache(self, mock_config):
        cfg = mock_config
        img = _make_tiny_image(cfg.media_dir / "102_photo.jpg")
        _write_manifest(cfg, [_make_analysis_item(102, "photo.jpg", str(img))])
        (cfg.cache_dir / "102.json").write_text(json.dumps({"thumbnail_path": "/fake/thumb.jpg"}))
        preprocess(cfg)
        results = json.loads(cfg.analysis_path.read_text())
        assert results[0]["thumbnail_path"] == "/fake/thumb.jpg"

    def test_saves_to_shared_cache(self, mock_config):
        cfg = mock_config
        img = _make_tiny_image(cfg.media_dir / "103_photo.jpg")
        _write_manifest(cfg, [_make_analysis_item(103, "photo.jpg", str(img))])
        preprocess(cfg)
        cache_file = cfg.cache_dir / "103.json"
        assert cache_file.exists()
        assert "thumbnail_path" in json.loads(cache_file.read_text())

    def test_exif_cached(self, mock_config):
        cfg = mock_config
        img = _make_tiny_image(cfg.media_dir / "200_photo.jpg")
        _write_manifest(cfg, [_make_analysis_item(200, "photo.jpg", str(img))])
        preprocess(cfg)
        assert (cfg.cache_dir / "200.json").exists()

    def test_exif_from_cache_used(self, mock_config):
        cfg = mock_config
        img = _make_tiny_image(cfg.media_dir / "201_photo.jpg")
        _write_manifest(cfg, [_make_analysis_item(201, "photo.jpg", str(img))])
        cache_data = {"thumbnail_path": "/fake/thumb.jpg",
                      "exif": {"focal_length": 24.0, "aperture": 1.4, "iso": 100}}
        (cfg.cache_dir / "201.json").write_text(json.dumps(cache_data))
        preprocess(cfg)
        results = json.loads(cfg.analysis_path.read_text())
        assert results[0].get("exif") == {"focal_length": 24.0, "aperture": 1.4, "iso": 100}

    def test_progress_callback(self, mock_config):
        cfg = mock_config
        img1 = _make_tiny_image(cfg.media_dir / "109_a.jpg")
        img2 = _make_tiny_image(cfg.media_dir / "110_b.jpg")
        _write_manifest(cfg, [
            _make_analysis_item(109, "a.jpg", str(img1), takentime=1700000000),
            _make_analysis_item(110, "b.jpg", str(img2), takentime=1700000100),
        ])
        calls = []
        preprocess(cfg, progress_callback=lambda c, t, n: calls.append((c, t, n)))
        assert len(calls) == 2
        assert calls[0][1] == 2
