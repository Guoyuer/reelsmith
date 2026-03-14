"""Tests for pipeline.edl — EDL data model and helpers."""

from __future__ import annotations

import pytest

from pipeline.edl import EDL, EditItem, MusicTrack, Segment, TextOverlay


# -----------------------------------------------------------------------
# Pure model tests
# -----------------------------------------------------------------------


class TestEditItemDefaults:
    def test_default_display_duration(self):
        """EditItem default display_duration is 4.0s."""
        item = EditItem(source_file="photo.jpg", media_type="photo")
        assert item.display_duration == 4.0

    def test_default_effect(self):
        """EditItem default effect is 'ken_burns_in'."""
        item = EditItem(source_file="photo.jpg", media_type="photo")
        assert item.effect == "ken_burns_in"

    def test_default_trim_times(self):
        """start_time and end_time default to None."""
        item = EditItem(source_file="video.mp4", media_type="video")
        assert item.start_time is None
        assert item.end_time is None

    def test_default_text_overlay(self):
        """text_overlay defaults to None."""
        item = EditItem(source_file="photo.jpg", media_type="photo")
        assert item.text_overlay is None


class TestSegmentDefaults:
    def test_default_transition(self):
        """Segment default transition is 'crossfade'."""
        seg = Segment(name="test", items=[])
        assert seg.transition == "crossfade"

    def test_default_transition_duration(self):
        """Segment default transition_duration is 0.8s."""
        seg = Segment(name="test", items=[])
        assert seg.transition_duration == 0.8


class TestAllItems:
    def test_flattens_segments(self, sample_edl: EDL):
        """all_items() should return a flat list of all items across segments."""
        items = sample_edl.all_items()
        assert len(items) == 6
        filenames = [item.source_file for item in items]
        assert filenames == [
            "IMG_001.jpg", "IMG_002.jpg", "VID_003.mp4",
            "IMG_004.jpg", "IMG_005.jpg", "VID_006.mp4",
        ]

    def test_empty_edl(self):
        """all_items() on an EDL with no segments returns empty list."""
        edl = EDL(title="Empty", target_duration=60.0, segments=[])
        assert edl.all_items() == []


class TestEstimatedDuration:
    def test_basic_calculation(self, sample_edl: EDL):
        """estimated_duration = sum(display_durations) - crossfade overlaps.

        Segment 1: 4 + 3 + 5 = 12s display, crossfade transitions = 0.8 * 2 = 1.6s
        Segment 2: 4 + 3.5 + 6 = 13.5s display, 'cut' transition = 0 overlap
        Total = 25.5 - 1.6 = 23.9s
        """
        assert sample_edl.estimated_duration() == pytest.approx(23.9)

    def test_single_item_no_transitions(self):
        """A segment with one item has no transitions to subtract."""
        edl = EDL(
            title="Solo",
            target_duration=10.0,
            segments=[
                Segment(
                    name="Only",
                    items=[EditItem(source_file="a.jpg", media_type="photo", display_duration=5.0)],
                    transition="crossfade",
                    transition_duration=1.0,
                ),
            ],
        )
        assert edl.estimated_duration() == 5.0


class TestJsonRoundtrip:
    def test_roundtrip(self, sample_edl: EDL):
        """EDL -> JSON -> EDL should produce an equivalent object."""
        json_str = sample_edl.model_dump_json()
        restored = EDL.model_validate_json(json_str)
        assert restored.title == sample_edl.title
        assert restored.target_duration == sample_edl.target_duration
        assert len(restored.segments) == len(sample_edl.segments)
        assert len(restored.all_items()) == len(sample_edl.all_items())
        assert restored.estimated_duration() == pytest.approx(sample_edl.estimated_duration())
        assert restored.music is not None
        assert restored.music.file == "bg_music.mp3"

    def test_roundtrip_no_music(self):
        """EDL without music should roundtrip correctly."""
        edl = EDL(
            title="No Music",
            target_duration=30.0,
            segments=[
                Segment(name="Seg1", items=[
                    EditItem(source_file="a.jpg", media_type="photo"),
                ]),
            ],
        )
        restored = EDL.model_validate_json(edl.model_dump_json())
        assert restored.music is None
        assert restored.resolution == (3840, 2160)
        assert restored.fps == 60
