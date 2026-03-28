"""Tests for pipeline.prepare — orchestration, video probing, EXIF, preview generation."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from pipeline._types import cache_id
from pipeline.config import Config
from pipeline.prepare._prepare import (
    PrepareConfig,
    _base_analysis_entry,
    _generate_video_previews,
    _prepare_video,
    _read_exif,
    load_analysis,
    prepare,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_image(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (160, 90), color=(100, 150, 200)).save(path, "JPEG")
    return path


def _make_manifest_item(local_path: str, **extra) -> dict:
    item = {
        "local_path": local_path,
        "item_type": 0,
        "taken_at": "2025-01-01T00:00:00+00:00",
        "takentime": 1700000000,
    }
    item.update(extra)
    return item


# ---------------------------------------------------------------------------
# _read_exif
# ---------------------------------------------------------------------------


class TestReadExif:
    def test_returns_dict_for_jpeg(self, tmp_path):
        img_path = tmp_path / "photo.jpg"
        Image.new("RGB", (100, 100), "red").save(img_path, "JPEG")
        exif = _read_exif(img_path)
        assert isinstance(exif, dict)
        assert len(exif) == 0

    def test_returns_empty_dict_when_exif_missing(self, tmp_path):
        img_path = tmp_path / "photo.png"
        Image.new("RGB", (100, 100)).save(img_path, "PNG")
        exif = _read_exif(img_path)
        assert isinstance(exif, dict)
        assert len(exif) == 0

    def test_returns_empty_dict_for_nonexistent_file(self, tmp_path):
        exif = _read_exif(tmp_path / "nonexistent.jpg")
        assert exif == {}


# ---------------------------------------------------------------------------
# _base_analysis_entry
# ---------------------------------------------------------------------------


class TestBaseAnalysisEntry:
    def test_photo_entry(self):
        item = {
            "local_path": "/media/IMG_001.jpg",
            "taken_at": "2025-01-01T00:00:00+00:00",
            "country": "Singapore",
            "city": "Singapore City",
        }
        entry = _base_analysis_entry(item, is_video=False)
        assert entry["media_type"] == "photo"
        assert entry["local_path"] == "/media/IMG_001.jpg"
        assert entry["country"] == "Singapore"
        assert entry["district"] == "Singapore City"

    def test_video_entry(self):
        item = {
            "local_path": "/media/VID_001.mp4",
            "taken_at": "2025-01-01T00:00:00+00:00",
        }
        entry = _base_analysis_entry(item, is_video=True)
        assert entry["media_type"] == "video"

    def test_city_fallback_for_district(self):
        item = {
            "local_path": "/media/IMG_002.jpg",
            "taken_at": "2025-01-01T00:00:00+00:00",
            "city": "Kyoto",
        }
        entry = _base_analysis_entry(item, is_video=False)
        assert entry["district"] == "Kyoto"

    def test_no_location(self):
        item = {
            "local_path": "/media/IMG_003.jpg",
            "taken_at": "2025-01-01T00:00:00+00:00",
        }
        entry = _base_analysis_entry(item, is_video=False)
        assert entry.get("country") is None
        assert entry.get("district") is None


# ---------------------------------------------------------------------------
# _prepare_video
# ---------------------------------------------------------------------------


class TestPrepareVideo:
    def test_parses_probe_output(self):
        probe_data = {
            "format": {"duration": "30.5"},
            "streams": [{"width": 1920, "height": 1080, "r_frame_rate": "30000/1001"}],
        }
        fake_result = MagicMock()
        fake_result.stdout = json.dumps(probe_data)

        entry = {"local_path": "/media/video.mp4"}
        with patch(
            "pipeline.prepare._prepare.run_subprocess", return_value=fake_result
        ):
            _prepare_video(entry, Path("/media/video.mp4"), 1, 10)

        assert entry["video_duration"] == 30.5
        assert entry["video_width"] == 1920
        assert entry["video_height"] == 1080
        assert entry["video_fps"] == pytest.approx(30.0, abs=0.1)
        assert entry["video_orientation"] == "landscape"

    def test_portrait_orientation(self):
        probe_data = {
            "format": {"duration": "10"},
            "streams": [{"width": 1080, "height": 1920, "r_frame_rate": "30/1"}],
        }
        fake_result = MagicMock()
        fake_result.stdout = json.dumps(probe_data)

        entry = {"local_path": "/media/video.mp4"}
        with patch(
            "pipeline.prepare._prepare.run_subprocess", return_value=fake_result
        ):
            _prepare_video(entry, Path("/media/video.mp4"), 1, 1)

        assert entry["video_orientation"] == "portrait"

    def test_fallback_on_bad_probe(self):
        fake_result = MagicMock()
        fake_result.stdout = "not json"

        entry = {"local_path": "/media/video.mp4"}
        with patch(
            "pipeline.prepare._prepare.run_subprocess", return_value=fake_result
        ):
            _prepare_video(entry, Path("/media/video.mp4"), 1, 1)

        assert entry["video_duration"] == 10.0
        assert entry["video_width"] == 0
        assert entry["video_orientation"] == "landscape"


# ---------------------------------------------------------------------------
# _generate_video_previews
# ---------------------------------------------------------------------------


class TestGenerateVideoPreview:
    def test_skips_existing_preview(self, tmp_path):
        preview_dir = tmp_path / "previews"
        preview_dir.mkdir()

        local_path = "/fake/video.mp4"
        preview = preview_dir / f"preview_{cache_id(local_path)}.mp4"
        preview.write_bytes(b"\x00" * 1000)

        video_items = [{"local_path": local_path, "video_duration": 30}]
        with patch("pipeline.prepare._prepare.run_subprocess") as mock_run:
            _generate_video_previews(video_items, preview_dir)
        mock_run.assert_not_called()

    def test_force_deletes_existing(self, tmp_path):
        preview_dir = tmp_path / "previews"
        preview_dir.mkdir()

        local_path = "/fake/video.mp4"
        preview = preview_dir / f"preview_{cache_id(local_path)}.mp4"
        preview.write_bytes(b"\x00" * 1000)

        video_items = [{"local_path": local_path, "video_duration": 30}]

        def fake_run(cmd, **kwargs):
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


# ---------------------------------------------------------------------------
# load_analysis
# ---------------------------------------------------------------------------


class TestLoadAnalysis:
    def test_loads_valid_entries(self, tmp_path):
        cfg = Config(workspace=tmp_path / "runs" / "test")
        cfg.ensure_dirs()
        data = [
            {
                "local_path": "/media/photo.jpg",
                "media_type": "photo",
                "taken_at": "2025-01-01T00:00:00+00:00",
            }
        ]
        cfg.analysis_path.write_text(json.dumps(data))
        results = load_analysis(cfg)
        assert len(results) == 1
        assert results[0]["media_type"] == "photo"

    def test_skips_invalid_entries(self, tmp_path):
        cfg = Config(workspace=tmp_path / "runs" / "test")
        cfg.ensure_dirs()
        data = [
            {
                "local_path": "/media/photo.jpg",
                "media_type": "photo",
                "taken_at": "2025-01-01T00:00:00+00:00",
            },
            {"media_type": "photo"},
        ]
        cfg.analysis_path.write_text(json.dumps(data))
        results = load_analysis(cfg)
        assert len(results) == 1

    def test_missing_file_raises(self, tmp_path):
        cfg = Config(workspace=tmp_path / "runs" / "test")
        cfg.ensure_dirs()
        with pytest.raises(FileNotFoundError, match="Analysis not found"):
            load_analysis(cfg)


# ---------------------------------------------------------------------------
# prepare() orchestration
# ---------------------------------------------------------------------------


class TestPrepareOrchestration:
    def test_missing_manifest_raises(self, tmp_path):
        cfg = Config(workspace=tmp_path / "runs" / "test")
        cfg.ensure_dirs()
        with pytest.raises(FileNotFoundError, match="Manifest not found"):
            prepare(cfg)

    def test_skips_nonexistent_files(self, tmp_path):
        cfg = Config(workspace=tmp_path / "runs" / "test")
        cfg.ensure_dirs()
        manifest = [_make_manifest_item("/nonexistent/photo.jpg")]
        cfg.manifest_path.write_text(json.dumps(manifest))
        prepare(cfg)
        data = json.loads(cfg.analysis_path.read_text())
        assert len(data) == 0

    def test_processes_real_photo(self, tmp_path):
        cfg = Config(workspace=tmp_path / "runs" / "test")
        cfg.ensure_dirs()
        cfg.media_dir.mkdir(parents=True, exist_ok=True)
        photo = _make_image(cfg.media_dir / "photo.jpg")
        manifest = [_make_manifest_item(str(photo))]
        cfg.manifest_path.write_text(json.dumps(manifest))
        prepare(cfg)
        data = json.loads(cfg.analysis_path.read_text())
        assert len(data) == 1
        assert data[0]["media_type"] == "photo"
        assert "thumbnail_path" in data[0]

    def test_processes_video_with_mock(self, tmp_path):
        cfg = Config(workspace=tmp_path / "runs" / "test")
        cfg.ensure_dirs()
        cfg.media_dir.mkdir(parents=True, exist_ok=True)
        video = cfg.media_dir / "clip.mp4"
        video.write_bytes(b"\x00" * 100)

        probe_data = {
            "format": {"duration": "15.0"},
            "streams": [{"width": 1920, "height": 1080, "r_frame_rate": "30/1"}],
        }
        fake_result = MagicMock()
        fake_result.stdout = json.dumps(probe_data)
        fake_result.returncode = 0

        manifest = [_make_manifest_item(str(video), item_type=1)]
        cfg.manifest_path.write_text(json.dumps(manifest))

        with patch(
            "pipeline.prepare._prepare.run_subprocess", return_value=fake_result
        ):
            prepare(cfg)

        data = json.loads(cfg.analysis_path.read_text())
        assert len(data) == 1
        assert data[0]["media_type"] == "video"
        assert data[0]["video_duration"] == 15.0

    def test_default_prepare_config(self, tmp_path):
        cfg = Config(workspace=tmp_path / "runs" / "test")
        cfg.ensure_dirs()
        cfg.media_dir.mkdir(parents=True, exist_ok=True)
        photo = _make_image(cfg.media_dir / "p.jpg")
        manifest = [_make_manifest_item(str(photo))]
        cfg.manifest_path.write_text(json.dumps(manifest))
        prepare(cfg, None)
        assert cfg.analysis_path.exists()

    def test_rerun_updates_analysis(self, tmp_path):
        """Re-running prepare should overwrite analysis.json."""
        cfg = Config(workspace=tmp_path / "runs" / "test")
        cfg.ensure_dirs()
        cfg.media_dir.mkdir(parents=True, exist_ok=True)
        photo = _make_image(cfg.media_dir / "photo.jpg")
        manifest = [_make_manifest_item(str(photo))]
        cfg.manifest_path.write_text(json.dumps(manifest))

        prepare(cfg, PrepareConfig())
        first_mtime = cfg.analysis_path.stat().st_mtime
        time.sleep(0.05)
        prepare(cfg, PrepareConfig())
        assert cfg.analysis_path.stat().st_mtime > first_mtime

    def test_progress_callback(self, mock_config):
        cfg = mock_config
        img1 = _make_image(cfg.media_dir / "109_a.jpg")
        img2 = _make_image(cfg.media_dir / "110_b.jpg")
        manifest = [
            _make_manifest_item(str(img1)),
            _make_manifest_item(str(img2), takentime=1700000100),
        ]
        cfg.manifest_path.write_text(json.dumps(manifest))
        calls = []
        prepare(cfg, progress_callback=lambda c, t, n: calls.append((c, t, n)))
        analyze_calls = [c for c in calls if c[2] == "extract metadata"]
        assert len(analyze_calls) == 2
        assert analyze_calls[0][1] == 2
