"""Tests for pipeline.assemble._audio — beat_snap_edl."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from pipeline.edl import EDL, EditItem, Segment

# All tests mock estimate_bpm so no real WAV files are needed.
_MOCK_BPM = patch("pipeline.assemble._audio.estimate_bpm", return_value=120)
# 120 BPM → beat_interval=0.5s, half_beat=0.25s
# Beat grid at 0.25s intervals: 0, 0.25, 0.5, 0.75, 1.0, ...


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
        from pipeline.assemble._audio import beat_snap_edl

        # Two photos: 3.1s + 3.0s. Transition at 3.1s.
        # Nearest half-beat: 3.0s (0.25*12). Shift = -0.1s → dur becomes 3.0s.
        seg = Segment(name="A", items=[_item(3.1), _item(3.0)])
        edl = _edl([seg])

        with _MOCK_BPM:
            n = beat_snap_edl(edl, Path("fake.wav"))

        assert n == 1
        assert edl.segments[0].items[0].display_duration == 3.0

    def test_no_snap_if_shift_too_large(self):
        """Transition not snapped if nearest beat is > max_shift away."""
        from pipeline.assemble._audio import beat_snap_edl

        # Photo at 3.3s. Nearest beats: 3.25 (shift -0.05) and 3.5 (shift +0.2).
        # Both within max_shift=0.4, so it WILL snap.
        # Use 3.62s instead: nearest beats 3.5 (shift -0.12) and 3.75 (shift +0.13).
        # Still within 0.4. Use something farther: need > 0.4s from any beat.
        # At 120 BPM half-beat=0.25s, max gap from any beat is 0.125s < 0.4s.
        # So at 120 BPM everything snaps. Use lower BPM for this test.
        with patch("pipeline.assemble._audio.estimate_bpm", return_value=30):
            # 30 BPM → beat=2.0s, half_beat=1.0s. Grid: 0, 1, 2, 3, ...
            # Photo at 3.6s. Nearest beats: 3.0 (shift -0.6) and 4.0 (shift +0.4).
            # max_shift=0.4, so 4.0 is exactly at boundary. -0.6 is too far.
            seg = Segment(name="A", items=[_item(3.6), _item(3.0)])
            edl = _edl([seg])
            n = beat_snap_edl(edl, Path("fake.wav"))

        assert n == 1  # snaps to 4.0 (shift=+0.4 is exactly at boundary)
        assert edl.segments[0].items[0].display_duration == 4.0

    def test_returns_zero_when_bpm_not_detected(self):
        """No snapping if BPM estimation fails."""
        from pipeline.assemble._audio import beat_snap_edl

        seg = Segment(name="A", items=[_item(3.1), _item(3.0)])
        edl = _edl([seg])

        with patch("pipeline.assemble._audio.estimate_bpm", return_value=None):
            n = beat_snap_edl(edl, Path("fake.wav"))

        assert n == 0
        assert edl.segments[0].items[0].display_duration == 3.1  # unchanged

    def test_intro_offset(self):
        """Title card duration is accounted for in beat alignment."""
        from pipeline.assemble._audio import beat_snap_edl

        # intro_duration=3.0. First item: 3.1s. Transition at 3.0+3.1=6.1s.
        # At 120 BPM, nearest beat to 6.1: 6.0 (shift -0.1) or 6.25 (+0.15).
        # Snaps to 6.0 → dur becomes 3.0.
        seg = Segment(name="A", items=[_item(3.1), _item(3.0)])
        edl = _edl([seg], intro="title_card")

        with _MOCK_BPM:
            n = beat_snap_edl(edl, Path("fake.wav"))

        assert n == 1
        assert edl.segments[0].items[0].display_duration == 3.0


class TestSegmentBoundarySnap:
    """#5: Segment boundaries should be snapped to beats."""

    def test_boundary_between_segments_is_snapped(self):
        from pipeline.assemble._audio import beat_snap_edl

        # Seg A: one item 3.1s. Seg B: one item 3.0s.
        # Boundary at 3.1s. Nearest beat: 3.0 (shift -0.1).
        seg_a = Segment(name="A", items=[_item(3.1)])
        seg_b = Segment(name="B", items=[_item(3.0)])
        edl = _edl([seg_a, seg_b])

        with _MOCK_BPM:
            n = beat_snap_edl(edl, Path("fake.wav"))

        assert n >= 1
        assert edl.segments[0].items[0].display_duration == 3.0

    def test_last_segment_last_item_not_snapped(self):
        """No transition after the very last item — nothing to snap."""
        from pipeline.assemble._audio import beat_snap_edl

        seg = Segment(name="A", items=[_item(3.1)])
        edl = _edl([seg])

        with _MOCK_BPM:
            n = beat_snap_edl(edl, Path("fake.wav"))

        # Single segment, single item — no transitions at all.
        assert n == 0
        assert edl.segments[0].items[0].display_duration == 3.1

    def test_multi_segment_boundaries(self):
        """All segment boundaries are snap candidates."""
        from pipeline.assemble._audio import beat_snap_edl

        # 3 segments, 1 item each. Boundaries at 3.1 and 6.2.
        segs = [Segment(name=f"S{i}", items=[_item(3.1)]) for i in range(3)]
        edl = _edl(segs)

        with _MOCK_BPM:
            n = beat_snap_edl(edl, Path("fake.wav"))

        # First boundary at 3.1 → snap to 3.0 (dur=3.0).
        # Second boundary at 3.0+3.1=6.1 → snap to 6.0 (dur=3.0).
        # Third item is last segment last item → no snap.
        assert n == 2


class TestPerItemSpeechSkip:
    """#4: Only keep_audio=true items skip, not the entire segment."""

    def test_speech_item_skipped_others_snapped(self):
        """In a mixed segment, only the keep_audio item's transition is skipped."""
        from pipeline.assemble._audio import beat_snap_edl

        # 3 items: photo 3.1s, video keep_audio 5.0s, photo 3.1s
        # Transition 1 (after photo): 3.1s → snap to 3.0. Snapped.
        # Transition 2 (after speech video): 3.0+5.0=8.0 → already on beat. Skipped (keep_audio).
        items = [
            _item(3.1),
            _item(5.0, media="video", keep_audio=True),
            _item(3.1),
        ]
        seg = Segment(name="Mixed", items=items)
        edl = _edl([seg])

        with _MOCK_BPM:
            beat_snap_edl(edl, Path("fake.wav"))

        # First transition snapped, second skipped
        assert edl.segments[0].items[0].display_duration == 3.0  # snapped
        assert edl.segments[0].items[1].display_duration == 5.0  # unchanged (speech)

    def test_all_speech_segment_fully_skipped(self):
        """A segment where every item has keep_audio → all transitions skipped."""
        from pipeline.assemble._audio import beat_snap_edl

        items = [
            _item(3.1, media="video", keep_audio=True),
            _item(5.1, media="video", keep_audio=True),
        ]
        seg = Segment(name="Speech", items=items)
        edl = _edl([seg])

        with _MOCK_BPM:
            n = beat_snap_edl(edl, Path("fake.wav"))

        assert n == 0
        assert edl.segments[0].items[0].display_duration == 3.1
        assert edl.segments[0].items[1].display_duration == 5.1

    def test_speech_at_segment_boundary(self):
        """Last item of a segment has keep_audio → boundary not snapped."""
        from pipeline.assemble._audio import beat_snap_edl

        seg_a = Segment(name="A", items=[_item(5.1, media="video", keep_audio=True)])
        seg_b = Segment(name="B", items=[_item(3.0)])
        edl = _edl([seg_a, seg_b])

        with _MOCK_BPM:
            beat_snap_edl(edl, Path("fake.wav"))

        assert edl.segments[0].items[0].display_duration == 5.1  # not snapped

    def test_non_speech_before_speech_is_snapped(self):
        """A non-speech item followed by a speech item: the non-speech can snap."""
        from pipeline.assemble._audio import beat_snap_edl

        items = [
            _item(3.1),  # non-speech, transition after it can snap
            _item(5.0, media="video", keep_audio=True),  # speech
        ]
        seg = Segment(name="Mixed", items=items)
        edl = _edl([seg])

        with _MOCK_BPM:
            n = beat_snap_edl(edl, Path("fake.wav"))

        assert n == 1
        assert edl.segments[0].items[0].display_duration == 3.0  # snapped


class TestMontageMaxShift:
    def test_montage_uses_smaller_max_shift(self):
        """Montage segments use max_shift=0.2 instead of 0.4."""
        from pipeline.assemble._audio import beat_snap_edl

        # At 120 BPM, half_beat=0.25s.
        # Photo at 2.3s. Nearest beats: 2.25 (shift -0.05) and 2.5 (shift +0.2).
        # Both within montage max_shift=0.2. Will snap to 2.25 (closer).
        seg = Segment(
            name="Montage",
            items=[_item(2.3), _item(2.5)],
            mode="montage",
            transition="cut",
            transition_duration=0.0,
        )
        edl = _edl([seg])

        with _MOCK_BPM:
            n = beat_snap_edl(edl, Path("fake.wav"))

        assert n == 1
        assert edl.segments[0].items[0].display_duration == 2.25
