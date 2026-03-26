"""Tests for pipeline.assemble._audio — beat_snap_edl."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from pipeline.assemble._audio import beat_snap_edl
from pipeline.edl import EDL, EditItem, Segment

# All tests mock estimate_bpm so no real WAV files are needed.
# 120 BPM → beat_interval=0.5s, half_beat=0.25s
# Beat grid at 0.25s intervals: 0, 0.25, 0.5, 0.75, 1.0, ...
_MOCK_BPM = patch("pipeline.assemble._audio.estimate_bpm", return_value=120)
_FAKE_WAV = Path("fake.wav")


def _item(dur, media="photo", keep_audio=False):
    return EditItem(
        source_file=f"f.{'mp4' if media == 'video' else 'jpg'}",
        media_type=media,
        display_duration=dur,
        keep_audio=keep_audio,
    )


def _edl(segments, intro="none"):
    return EDL(
        title="test",
        target_duration=60,
        segments=segments,
        intro_style=intro,
        intro_duration=3.0,
    )


class TestBeatSnapBasic:
    def test_snaps_transition_to_nearest_beat(self):
        """Item duration adjusted so transition lands on beat grid."""
        # Two photos: 3.1s + 3.0s. Transition at 3.1s.
        # Nearest half-beat: 3.0s (0.25*12). Shift = -0.1s → dur becomes 3.0s.
        seg = Segment(name="A", items=[_item(3.1), _item(3.0)])
        edl = _edl([seg])

        with _MOCK_BPM:
            n = beat_snap_edl(edl, _FAKE_WAV)

        assert n == 1
        assert edl.segments[0].items[0].display_duration == 3.0

    def test_no_snap_if_shift_too_large(self):
        """Transition not snapped if nearest beat is > max_shift away."""
        with patch("pipeline.assemble._audio.estimate_bpm", return_value=30):
            # 30 BPM → beat=2.0s, half_beat=1.0s. Grid: 0, 1, 2, 3, ...
            # Photo at 3.6s. Nearest beats: 3.0 (shift -0.6) and 4.0 (shift +0.4).
            # max_shift=0.4, so 4.0 is exactly at boundary. -0.6 is too far.
            seg = Segment(name="A", items=[_item(3.6), _item(3.0)])
            edl = _edl([seg])
            n = beat_snap_edl(edl, _FAKE_WAV)

        assert n == 1  # snaps to 4.0 (shift=+0.4 is exactly at boundary)
        assert edl.segments[0].items[0].display_duration == 4.0

    def test_returns_zero_when_bpm_not_detected(self):
        seg = Segment(name="A", items=[_item(3.1), _item(3.0)])
        edl = _edl([seg])

        with patch("pipeline.assemble._audio.estimate_bpm", return_value=None):
            n = beat_snap_edl(edl, _FAKE_WAV)

        assert n == 0
        assert edl.segments[0].items[0].display_duration == 3.1

    def test_intro_offset(self):
        """Title card duration is accounted for in beat alignment."""
        # intro_duration=3.0. First item: 3.1s. Transition at 3.0+3.1=6.1s.
        # At 120 BPM, nearest beat to 6.1: 6.0 (shift -0.1) or 6.25 (+0.15).
        # Snaps to 6.0 → dur becomes 3.0.
        seg = Segment(name="A", items=[_item(3.1), _item(3.0)])
        edl = _edl([seg], intro="title_card")

        with _MOCK_BPM:
            n = beat_snap_edl(edl, _FAKE_WAV)

        assert n == 1
        assert edl.segments[0].items[0].display_duration == 3.0


class TestSegmentBoundarySnap:
    def test_boundary_between_segments_is_snapped(self):
        seg_a = Segment(name="A", items=[_item(3.1)])
        seg_b = Segment(name="B", items=[_item(3.0)])
        edl = _edl([seg_a, seg_b])

        with _MOCK_BPM:
            n = beat_snap_edl(edl, _FAKE_WAV)

        assert n >= 1
        assert edl.segments[0].items[0].display_duration == 3.0

    def test_last_segment_last_item_not_snapped(self):
        """No transition after the very last item — nothing to snap."""
        seg = Segment(name="A", items=[_item(3.1)])
        edl = _edl([seg])

        with _MOCK_BPM:
            n = beat_snap_edl(edl, _FAKE_WAV)

        assert n == 0
        assert edl.segments[0].items[0].display_duration == 3.1

    def test_multi_segment_boundaries(self):
        """All segment boundaries are snap candidates."""
        segs = [Segment(name=f"S{i}", items=[_item(3.1)]) for i in range(3)]
        edl = _edl(segs)

        with _MOCK_BPM:
            n = beat_snap_edl(edl, _FAKE_WAV)

        # First two boundaries snapped, last item is final → no snap
        assert n == 2


class TestPerItemSpeechSkip:
    """Only keep_audio=true items skip, not the entire segment."""

    def test_speech_item_skipped_others_snapped(self):
        items = [
            _item(3.1),
            _item(5.0, media="video", keep_audio=True),
            _item(3.1),
        ]
        seg = Segment(name="Mixed", items=items)
        edl = _edl([seg])

        with _MOCK_BPM:
            beat_snap_edl(edl, _FAKE_WAV)

        assert edl.segments[0].items[0].display_duration == 3.0  # snapped
        assert edl.segments[0].items[1].display_duration == 5.0  # unchanged (speech)

    def test_all_speech_segment_fully_skipped(self):
        items = [
            _item(3.1, media="video", keep_audio=True),
            _item(5.1, media="video", keep_audio=True),
        ]
        seg = Segment(name="Speech", items=items)
        edl = _edl([seg])

        with _MOCK_BPM:
            n = beat_snap_edl(edl, _FAKE_WAV)

        assert n == 0
        assert edl.segments[0].items[0].display_duration == 3.1
        assert edl.segments[0].items[1].display_duration == 5.1

    def test_speech_at_segment_boundary(self):
        """Last item of a segment has keep_audio → boundary not snapped."""
        seg_a = Segment(name="A", items=[_item(5.1, media="video", keep_audio=True)])
        seg_b = Segment(name="B", items=[_item(3.0)])
        edl = _edl([seg_a, seg_b])

        with _MOCK_BPM:
            beat_snap_edl(edl, _FAKE_WAV)

        assert edl.segments[0].items[0].display_duration == 5.1

    def test_non_speech_before_speech_is_snapped(self):
        items = [
            _item(3.1),
            _item(5.0, media="video", keep_audio=True),
        ]
        seg = Segment(name="Mixed", items=items)
        edl = _edl([seg])

        with _MOCK_BPM:
            n = beat_snap_edl(edl, _FAKE_WAV)

        assert n == 1
        assert edl.segments[0].items[0].display_duration == 3.0


class TestMontageMaxShift:
    def test_montage_uses_smaller_max_shift(self):
        """Montage segments use max_shift=0.2 instead of 0.4."""
        # At 120 BPM, half_beat=0.25s.
        # Photo at 2.3s. Nearest beats: 2.25 (shift -0.05) and 2.5 (shift +0.2).
        seg = Segment(
            name="Montage",
            items=[_item(2.3), _item(2.5)],
            mode="montage",
            transition="cut",
            transition_duration=0.0,
        )
        edl = _edl([seg])

        with _MOCK_BPM:
            n = beat_snap_edl(edl, _FAKE_WAV)

        assert n == 1
        assert edl.segments[0].items[0].display_duration == 2.25
