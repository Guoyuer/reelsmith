"""Tests for pipeline.plan._preview — burst dedup and item text building."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline.plan._preview import (
    _build_item_text,
    _build_offset_table,
    _dedup_burst_photos,
    _group_by_timestamp,
    _histogram_similarity,
    _photo_histogram,
)
from tests.helpers import make_jpeg

# ---------------------------------------------------------------------------
# _photo_histogram
# ---------------------------------------------------------------------------


class TestPhotoHistogram:
    def test_returns_list(self, tmp_path):
        img = tmp_path / "thumb.jpg"
        # Save as RGB; _photo_histogram converts to HSV internally
        make_jpeg(img, color=(200, 100, 50))
        hist = _photo_histogram(img)
        assert hist is not None
        assert isinstance(hist, list)
        assert len(hist) > 0

    def test_nonexistent_returns_none(self, tmp_path):
        hist = _photo_histogram(tmp_path / "missing.jpg")
        assert hist is None


# ---------------------------------------------------------------------------
# _histogram_similarity
# ---------------------------------------------------------------------------


class TestHistogramSimilarity:
    def test_identical_is_one(self):
        h = [10, 20, 30, 40]
        assert _histogram_similarity(h, h) == pytest.approx(1.0)

    def test_orthogonal_is_zero(self):
        h1 = [1, 0]
        h2 = [0, 1]
        assert _histogram_similarity(h1, h2) == pytest.approx(0.0)

    def test_zero_magnitude(self):
        h1 = [0, 0, 0]
        h2 = [1, 2, 3]
        assert _histogram_similarity(h1, h2) == 0.0

    def test_similar_histograms(self):
        h1 = [10, 20, 30]
        h2 = [11, 21, 29]
        sim = _histogram_similarity(h1, h2)
        assert sim > 0.99


# ---------------------------------------------------------------------------
# _group_by_timestamp
# ---------------------------------------------------------------------------


class TestGroupByTimestamp:
    def _photo(self, taken_at, **kw):
        return {
            "local_path": f"/media/{taken_at}.jpg",
            "media_type": "photo",
            "taken_at": taken_at,
            **kw,
        }

    def test_single_photo(self):
        photos = [self._photo("2025-01-01T00:00:00+00:00")]
        bursts = _group_by_timestamp(photos, 10)
        assert len(bursts) == 1
        assert len(bursts[0]) == 1

    def test_two_close_photos(self):
        photos = [
            self._photo("2025-01-01T00:00:00+00:00"),
            self._photo("2025-01-01T00:00:05+00:00"),
        ]
        bursts = _group_by_timestamp(photos, 10)
        assert len(bursts) == 1
        assert len(bursts[0]) == 2

    def test_two_far_photos(self):
        photos = [
            self._photo("2025-01-01T00:00:00+00:00"),
            self._photo("2025-01-01T00:01:00+00:00"),
        ]
        bursts = _group_by_timestamp(photos, 10)
        assert len(bursts) == 2

    def test_invalid_timestamp_starts_new_group(self):
        photos = [
            self._photo("2025-01-01T00:00:00+00:00"),
            self._photo("not-a-date"),
        ]
        bursts = _group_by_timestamp(photos, 10)
        assert len(bursts) == 2


# ---------------------------------------------------------------------------
# _dedup_burst_photos
# ---------------------------------------------------------------------------


class TestDedupBurstPhotos:
    def test_videos_pass_through(self, tmp_path):
        items = [
            {
                "local_path": "/media/vid.mp4",
                "media_type": "video",
                "taken_at": "2025-01-01T00:00:00+00:00",
            }
        ]
        result = _dedup_burst_photos(items, tmp_path)
        assert len(result) == 1

    def test_single_photo_unchanged(self, tmp_path):
        items = [
            {
                "local_path": "/media/photo.jpg",
                "media_type": "photo",
                "taken_at": "2025-01-01T00:00:00+00:00",
            }
        ]
        result = _dedup_burst_photos(items, tmp_path)
        assert len(result) == 1

    def test_mixed_types_preserved(self, tmp_path):
        items = [
            {
                "local_path": "/media/photo1.jpg",
                "media_type": "photo",
                "taken_at": "2025-01-01T00:00:00+00:00",
            },
            {
                "local_path": "/media/video.mp4",
                "media_type": "video",
                "taken_at": "2025-01-01T00:00:05+00:00",
            },
        ]
        result = _dedup_burst_photos(items, tmp_path)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# _build_item_text
# ---------------------------------------------------------------------------


class TestBuildItemText:
    def test_photo_with_exif(self):
        entry = {
            "local_path": "/media/IMG_001.jpg",
            "media_type": "photo",
            "district": "Marina Bay",
            "exif": {"focal_length": 50.0, "aperture": 2.0, "iso_speed": 400},
        }
        text, photo_path = _build_item_text(1, entry)
        assert "#01:" in text
        assert "at=Marina Bay" in text
        assert "50mm" in text
        assert "f/2.0" in text
        assert "ISO400" in text
        assert "file=IMG_001.jpg" in text
        assert photo_path == Path("/media/IMG_001.jpg")

    def test_video_item(self):
        entry = {
            "local_path": "/media/VID_001.mp4",
            "media_type": "video",
            "video_duration": 45.0,
            "video_width": 1920,
            "video_height": 1080,
            "video_fps": 30,
        }
        text, photo_path = _build_item_text(3, entry)
        assert "#03:" in text
        assert "video=45s" in text
        assert "1920x1080" in text
        assert photo_path is None

    def test_high_fps_video(self):
        entry = {
            "local_path": "/media/VID_002.mp4",
            "media_type": "video",
            "video_duration": 10.0,
            "video_width": 3840,
            "video_height": 2160,
            "video_fps": 60,
        }
        text, _ = _build_item_text(5, entry)
        assert "60fps" in text

    def test_portrait_video(self):
        entry = {
            "local_path": "/media/VID_003.mp4",
            "media_type": "video",
            "video_duration": 10.0,
            "video_width": 1080,
            "video_height": 1920,
            "video_orientation": "portrait",
        }
        text, _ = _build_item_text(1, entry)
        assert "(portrait)" in text

    def test_photo_no_exif(self):
        entry = {
            "local_path": "/media/IMG_002.jpg",
            "media_type": "photo",
        }
        text, photo_path = _build_item_text(2, entry)
        assert "#02:" in text
        assert "file=IMG_002.jpg" in text
        assert photo_path is not None

    def test_no_location(self):
        entry = {
            "local_path": "/media/IMG_003.jpg",
            "media_type": "photo",
        }
        text, _ = _build_item_text(1, entry)
        assert "at=" not in text


# ---------------------------------------------------------------------------
# _build_offset_table
# ---------------------------------------------------------------------------


class TestBuildOffsetTable:
    def test_builds_cumulative_offsets(self):
        entries = [
            (1, 10.0, Path("/p1.mp4")),
            (2, 15.0, Path("/p2.mp4")),
            (3, 20.0, Path("/p3.mp4")),
        ]
        with patch(
            "pipeline.plan._preview.probe_duration", side_effect=[10.0, 15.0, 20.0]
        ):
            table, valid = _build_offset_table(entries)

        assert len(table) == 3
        assert table[0] == (1, 10.0, 0.0)
        assert table[1] == (2, 15.0, 10.0)
        assert table[2] == (3, 20.0, 25.0)

    def test_skips_zero_duration(self):
        entries = [
            (1, 10.0, Path("/p1.mp4")),
            (2, 15.0, Path("/p2.mp4")),
        ]
        with patch("pipeline.plan._preview.probe_duration", side_effect=[10.0, 0]):
            table, valid = _build_offset_table(entries)

        assert len(table) == 1
        assert len(valid) == 1

    def test_empty_entries(self):
        with patch("pipeline.plan._preview.probe_duration"):
            table, valid = _build_offset_table([])
        assert table == []
        assert valid == []
