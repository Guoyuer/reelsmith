"""Tests for pipeline.edl — EDL data model and helpers."""

from __future__ import annotations

import pytest

from pipeline.edl import EDL, EditItem, Segment

# -----------------------------------------------------------------------
# Pure model tests
# -----------------------------------------------------------------------


class TestEditItemDefaults:
    @pytest.mark.parametrize(
        "field, expected",
        [
            ("display_duration", 4.0),
            ("effect", "ken_burns_in"),
            ("text_overlay", None),
        ],
    )
    def test_defaults(self, field, expected):
        item = EditItem(source_file="photo.jpg", media_type="photo")
        assert getattr(item, field) == expected

    def test_default_trim_times(self):
        """start_time and end_time default to None."""
        item = EditItem(source_file="video.mp4", media_type="video")
        assert item.start_time is None
        assert item.end_time is None


class TestSegmentDefaults:
    @pytest.mark.parametrize(
        "field, expected",
        [
            ("transition", "crossfade"),
            ("transition_duration", 0.4),
        ],
    )
    def test_defaults(self, field, expected):
        seg = Segment(name="test", items=[])
        assert getattr(seg, field) == expected


class TestAllItems:
    def test_flattens_segments(self, sample_edl: EDL):
        """all_items() should return a flat list of all items across segments."""
        items = sample_edl.all_items()
        assert len(items) == 6
        filenames = [item.source_file for item in items]
        assert filenames == [
            "IMG_001.jpg",
            "IMG_002.jpg",
            "VID_003.mp4",
            "IMG_004.jpg",
            "IMG_005.jpg",
            "VID_006.mp4",
        ]

    def test_empty_edl(self):
        """all_items() on an EDL with no segments returns empty list."""
        edl = EDL(
            title="Empty",
            target_duration=60.0,
            trip_type="family",
            style="upbeat",
            segments=[],
        )
        assert edl.all_items() == []


class TestEstimatedDuration:
    def test_basic_calculation(self, sample_edl: EDL):
        """estimated_duration = sum(display_durations) + intro + outro.

        Segment 1: 4 + 3 + 5 = 12s
        Segment 2: 4 + 3.5 + 6 = 13.5s
        Total = 25.5 + intro (3s) + outro (3s) = 31.5s
        """
        assert sample_edl.estimated_duration() == pytest.approx(31.5)

    def test_single_item_no_transitions(self):
        """A segment with one item has no transitions to subtract."""
        edl = EDL(
            title="Solo",
            target_duration=10.0,
            trip_type="family",
            style="upbeat",
            segments=[
                Segment(
                    name="Only",
                    items=[
                        EditItem(
                            source_file="a.jpg",
                            media_type="photo",
                            display_duration=5.0,
                        )
                    ],
                    transition="crossfade",
                    transition_duration=1.0,
                ),
            ],
            intro_duration=0,
            outro_duration=0,
        )
        assert edl.estimated_duration() == 5.0


class TestJsonRoundtrip:
    def test_roundtrip(self, sample_edl: EDL):
        """EDL -> JSON -> EDL should produce an equivalent object."""
        restored = EDL.model_validate_json(sample_edl.model_dump_json())
        assert restored.title == sample_edl.title
        assert restored.target_duration == sample_edl.target_duration
        assert len(restored.segments) == len(sample_edl.segments)
        assert len(restored.all_items()) == len(sample_edl.all_items())
        assert restored.estimated_duration() == pytest.approx(
            sample_edl.estimated_duration()
        )
        assert restored.music is not None
        assert restored.music.file == "bg_music.mp3"

    def test_roundtrip_no_music(self):
        """EDL without music should roundtrip correctly."""
        edl = EDL(
            title="No Music",
            target_duration=30.0,
            trip_type="family",
            style="upbeat",
            segments=[
                Segment(
                    name="Seg1",
                    items=[
                        EditItem(source_file="a.jpg", media_type="photo"),
                    ],
                ),
            ],
        )
        restored = EDL.model_validate_json(edl.model_dump_json())
        assert restored.music is None


# -----------------------------------------------------------------------
# EDL persistence helpers
# -----------------------------------------------------------------------


class TestEDLPersistence:
    def test_save_and_load(self, tmp_path):
        from pipeline.config import Config
        from pipeline.edl import load_latest_edl, save_edl

        cfg = Config(workspace=tmp_path / "runs" / "test")
        cfg.ensure_dirs()
        edl = EDL(
            title="Test",
            target_duration=30,
            trip_type="family",
            style="upbeat",
            segments=[
                Segment(
                    name="S1",
                    items=[
                        EditItem(source_file="a.jpg", media_type="photo"),
                    ],
                ),
            ],
        )
        save_edl(cfg, edl, version=3)
        assert (cfg.workspace / "edl_v3.json").exists()

        loaded, version = load_latest_edl(cfg)
        assert version == 3
        assert loaded.title == "Test"

    def test_find_latest_version(self, tmp_path):
        from pipeline.config import Config
        from pipeline.edl import find_latest_version

        cfg = Config(workspace=tmp_path / "runs" / "test")
        cfg.ensure_dirs()
        (cfg.workspace / "edl_v1.json").write_text("{}")
        (cfg.workspace / "edl_v5.json").write_text("{}")
        (cfg.workspace / "edl_v3.json").write_text("{}")
        assert find_latest_version(cfg) == 5

    def test_no_edl_raises(self, tmp_path):
        from pipeline.config import Config
        from pipeline.edl import load_latest_edl

        cfg = Config(workspace=tmp_path / "runs" / "test")
        cfg.ensure_dirs()
        with pytest.raises(FileNotFoundError):
            load_latest_edl(cfg)
