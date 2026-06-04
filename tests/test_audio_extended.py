"""Extended tests for pipeline.assemble._audio — BPM, beat_snap_edl, chapters."""

from __future__ import annotations

import json
import struct
import wave
from pathlib import Path
from unittest.mock import patch

from pipeline.assemble._audio import (
    _build_beat_grid,
    beat_snap_edl,
    estimate_bpm,
    write_chapters,
    write_cue_sheet,
)
from pipeline.edl import EDL, EditItem, Segment

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_wav(
    path: Path, duration: float = 5.0, sample_rate: int = 44100, freq: float = 440.0
) -> Path:
    """Create a simple sine-wave WAV file."""
    import math

    n_frames = int(sample_rate * duration)
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        for i in range(n_frames):
            sample = int(16000 * math.sin(2 * math.pi * freq * i / sample_rate))
            w.writeframes(struct.pack("<h", sample))
    return path


def _make_edl(items_per_seg: list[int], duration: float = 4.0) -> EDL:
    segments = []
    for si, n_items in enumerate(items_per_seg):
        items = [
            EditItem(
                source_file=f"/fake/s{si}_i{ii}.jpg",
                media_type="photo",
                display_duration=duration,
            )
            for ii in range(n_items)
        ]
        segments.append(
            Segment(
                name=f"S{si}",
                items=items,
                transition="crossfade",
                transition_duration=0.5,
            )
        )
    return EDL(
        title="T",
        target_duration=60,
        trip_type="family",
        style="upbeat",
        segments=segments,
    )


# ---------------------------------------------------------------------------
# estimate_bpm
# ---------------------------------------------------------------------------


class TestEstimateBpm:
    def test_returns_bpm_for_valid_wav(self, tmp_path):
        wav = _make_wav(tmp_path / "test.wav", duration=10.0)
        bpm = estimate_bpm(wav)
        # A sine wave doesn't have strong beats; result may vary
        # but should return an integer or None
        assert bpm is None or isinstance(bpm, int)

    def test_returns_none_for_short_wav(self, tmp_path):
        wav = _make_wav(tmp_path / "short.wav", duration=0.5)
        assert estimate_bpm(wav) is None

    def test_returns_none_for_nonexistent(self, tmp_path):
        assert estimate_bpm(tmp_path / "nope.wav") is None

    def test_returns_none_for_corrupt(self, tmp_path):
        bad = tmp_path / "bad.wav"
        bad.write_bytes(b"not a wav file")
        assert estimate_bpm(bad) is None

    def test_stereo_wav(self, tmp_path):
        """Stereo WAV should be averaged to mono."""
        import math

        path = tmp_path / "stereo.wav"
        sample_rate = 44100
        n_frames = sample_rate * 5
        with wave.open(str(path), "w") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            for i in range(n_frames):
                sample = int(16000 * math.sin(2 * math.pi * 440 * i / sample_rate))
                w.writeframes(struct.pack("<hh", sample, sample))
        result = estimate_bpm(path)
        assert result is None or isinstance(result, int)

    def test_32bit_wav(self, tmp_path):
        """32-bit WAV should work."""
        import math

        path = tmp_path / "32bit.wav"
        sample_rate = 44100
        n_frames = sample_rate * 5
        with wave.open(str(path), "w") as w:
            w.setnchannels(1)
            w.setsampwidth(4)
            w.setframerate(sample_rate)
            for i in range(n_frames):
                sample = int(
                    1_000_000_000 * math.sin(2 * math.pi * 440 * i / sample_rate)
                )
                w.writeframes(struct.pack("<i", sample))
        result = estimate_bpm(path)
        assert result is None or isinstance(result, int)

    def test_multichannel_returns_none(self, tmp_path):
        """Audio with >2 channels should return None."""
        path = tmp_path / "quad.wav"
        sample_rate = 44100
        n_frames = sample_rate * 3
        with wave.open(str(path), "w") as w:
            w.setnchannels(4)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            for _ in range(n_frames):
                w.writeframes(struct.pack("<hhhh", 0, 0, 0, 0))
        assert estimate_bpm(path) is None


# ---------------------------------------------------------------------------
# _build_beat_grid
# ---------------------------------------------------------------------------


class TestBuildBeatGrid:
    def test_returns_half_beat_grid(self):
        """Grid should contain half-beat intervals for the given BPM."""
        with patch("pipeline.assemble._audio.estimate_bpm", return_value=120):
            result = _build_beat_grid(Path("/fake.wav"))
        assert result is not None
        beats, bpm = result
        assert bpm == 120
        # 120 BPM → beat_interval=0.5s, half_beat=0.25s
        assert beats[0] == 0.0
        assert beats[1] == 0.25
        assert beats[2] == 0.50

    def test_returns_none_when_no_bpm(self):
        with patch("pipeline.assemble._audio.estimate_bpm", return_value=None):
            assert _build_beat_grid(Path("/fake.wav")) is None


# ---------------------------------------------------------------------------
# beat_snap_edl
# ---------------------------------------------------------------------------


class TestBeatSnapEdl:
    def test_snaps_with_bpm(self):
        edl = _make_edl([3, 2], duration=4.0)
        with patch("pipeline.assemble._audio.estimate_bpm", return_value=120):
            snapped = beat_snap_edl(edl, Path("/fake.wav"))
        # With BPM=120, beat interval=0.5s, half-beat=0.25s
        # Some transitions may snap
        assert isinstance(snapped, int)
        assert snapped >= 0

    def test_min_duration_respected(self):
        """Beat snap should not reduce photo below 2s or video below 3s."""
        edl = EDL(
            title="T",
            target_duration=60,
            trip_type="family",
            style="upbeat",
            segments=[
                Segment(
                    name="S",
                    items=[
                        EditItem(
                            source_file="/a.jpg",
                            media_type="photo",
                            display_duration=2.1,  # just above minimum
                        ),
                        EditItem(
                            source_file="/b.jpg",
                            media_type="photo",
                            display_duration=4.0,
                        ),
                    ],
                    transition="crossfade",
                    transition_duration=0.5,
                )
            ],
        )
        with patch("pipeline.assemble._audio.estimate_bpm", return_value=120):
            beat_snap_edl(edl, Path("/fake.wav"))
        assert edl.all_items()[0].display_duration >= 2.0


# ---------------------------------------------------------------------------
# write_chapters
# ---------------------------------------------------------------------------


class TestWriteChapters:
    def test_basic_chapters(self, tmp_path):
        edl = EDL(
            title="T",
            target_duration=120,
            trip_type="family",
            style="upbeat",
            segments=[
                Segment(
                    name="Opening",
                    items=[
                        EditItem(
                            source_file="a.jpg",
                            media_type="photo",
                            display_duration=4.0,
                        )
                    ],
                    transition="cut",
                ),
                Segment(
                    name="Middle",
                    items=[
                        EditItem(
                            source_file="b.jpg",
                            media_type="photo",
                            display_duration=4.0,
                        )
                    ],
                    transition="cut",
                ),
                Segment(
                    name="Closing",
                    items=[
                        EditItem(
                            source_file="c.jpg",
                            media_type="photo",
                            display_duration=4.0,
                        )
                    ],
                    transition="cut",
                ),
            ],
            intro_duration=3.0,
        )
        out = tmp_path / "chapters.txt"
        write_chapters(edl, [30.0, 45.0, 25.0], out)
        text = out.read_text()
        lines = text.strip().split("\n")
        assert len(lines) == 3
        assert lines[0] == "0:03 Opening"  # intro offset = 3s
        assert lines[1] == "0:33 Middle"  # 3 + 30 = 33s
        assert lines[2] == "1:18 Closing"  # 33 + 45 = 78s

    def test_no_intro(self, tmp_path):
        edl = EDL(
            title="T",
            target_duration=60,
            trip_type="family",
            style="upbeat",
            segments=[
                Segment(
                    name="S1",
                    items=[
                        EditItem(
                            source_file="a.jpg",
                            media_type="photo",
                            display_duration=4.0,
                        )
                    ],
                    transition="cut",
                ),
            ],
            intro_duration=0,
        )
        out = tmp_path / "chapters.txt"
        write_chapters(edl, [30.0], out)
        text = out.read_text()
        assert text.startswith("0:00 S1")

    def test_empty_durations(self, tmp_path):
        edl = EDL(
            title="T",
            target_duration=60,
            trip_type="family",
            style="upbeat",
            segments=[
                Segment(
                    name="S1",
                    items=[
                        EditItem(
                            source_file="a.jpg",
                            media_type="photo",
                            display_duration=4.0,
                        )
                    ],
                    transition="cut",
                ),
            ],
        )
        out = tmp_path / "chapters.txt"
        write_chapters(edl, [], out)
        assert out.exists()

    def test_beyond_one_hour(self, tmp_path):
        """Chapters beyond 1 hour should format minutes > 60 correctly."""
        edl = EDL(
            title="T",
            target_duration=7200,
            trip_type="family",
            style="upbeat",
            segments=[
                Segment(
                    name="Early",
                    items=[
                        EditItem(
                            source_file="a.jpg",
                            media_type="photo",
                            display_duration=4.0,
                        )
                    ],
                    transition="cut",
                ),
                Segment(
                    name="Late",
                    items=[
                        EditItem(
                            source_file="b.jpg",
                            media_type="photo",
                            display_duration=4.0,
                        )
                    ],
                    transition="cut",
                ),
            ],
            intro_duration=0,
        )
        out = tmp_path / "chapters.txt"
        write_chapters(edl, [3600.0, 1800.0], out)
        lines = out.read_text().strip().split("\n")
        assert lines[0] == "0:00 Early"
        assert lines[1] == "60:00 Late"  # 3600s = 60 minutes


# ---------------------------------------------------------------------------
# write_cue_sheet
# ---------------------------------------------------------------------------


def _cue_edl() -> EDL:
    """2 segments (3 + 2 items), intro 3s, for cue-sheet tests."""
    return EDL(
        title="Atlanta Trip",
        target_duration=120,
        trip_type="family",
        style="upbeat",
        segments=[
            Segment(
                name="Zoo",
                items=[
                    EditItem(
                        source_file="IMG_001.jpg",
                        media_type="photo",
                        display_duration=4.0,
                    ),
                    EditItem(
                        source_file="VID_002.mp4",
                        media_type="video",
                        display_duration=5.0,
                        start_time=2.0,
                        end_time=7.0,
                        keep_audio=True,
                        playback_speed=1.0,
                    ),
                    EditItem(
                        source_file="IMG_003.jpg",
                        media_type="photo",
                        display_duration=3.0,
                    ),
                ],
                transition="cut",
            ),
            Segment(
                name="Aquarium",
                items=[
                    EditItem(
                        source_file="VID_004.mp4",
                        media_type="video",
                        display_duration=6.0,
                        start_time=0.0,
                        end_time=3.0,
                        playback_speed=0.5,
                    ),
                    EditItem(
                        source_file="IMG_005.jpg",
                        media_type="photo",
                        display_duration=4.0,
                    ),
                ],
                transition="cut",
            ),
        ],
        intro_duration=3.0,
        outro_duration=2.0,
    )


class TestWriteCueSheet:
    def test_basic_structure(self, tmp_path):
        edl = _cue_edl()
        out = tmp_path / "cuesheet.json"
        # seg0 items sum to 12s, seg1 to 10s — pass probed durations that match.
        write_cue_sheet(edl, [12.0, 10.0], out)
        data = json.loads(out.read_text())

        assert data["title"] == "Atlanta Trip"
        assert data["intro_duration"] == 3.0
        assert data["outro_duration"] == 2.0
        # 3 intro + 12 + 10 + 2 outro = 27s
        assert data["total_duration"] == 27.0
        assert len(data["items"]) == 5

    def test_first_item_starts_after_intro(self, tmp_path):
        edl = _cue_edl()
        out = tmp_path / "cuesheet.json"
        write_cue_sheet(edl, [12.0, 10.0], out)
        items = json.loads(out.read_text())["items"]
        # First real item starts at intro_duration (3s), not 0.
        assert items[0]["record_in"] == 3.0
        assert items[0]["source_file"] == "IMG_001.jpg"

    def test_contiguous_coverage(self, tmp_path):
        """Each item's record_out is the next item's record_in (no gaps/overlaps)."""
        edl = _cue_edl()
        out = tmp_path / "cuesheet.json"
        write_cue_sheet(edl, [12.0, 10.0], out)
        items = json.loads(out.read_text())["items"]
        for prev, nxt in zip(items, items[1:]):
            assert prev["record_out"] == nxt["record_in"]
        # Last item ends at intro + seg0 + seg1 = 3 + 12 + 10 = 25s.
        assert items[-1]["record_out"] == 25.0

    def test_segment_boundary_anchored_to_probed_duration(self, tmp_path):
        """Last item of a segment absorbs drift so boundaries match chapters."""
        edl = _cue_edl()
        out = tmp_path / "cuesheet.json"
        # seg0 probed 13s (items sum 12) — last item must stretch to the boundary.
        write_cue_sheet(edl, [13.0, 10.0], out)
        items = json.loads(out.read_text())["items"]
        seg0 = [it for it in items if it["segment_index"] == 0]
        # seg0 starts at 3s, probed 13s → ends at 16s.
        assert seg0[-1]["record_out"] == 16.0
        # seg1 first item starts exactly at the boundary.
        seg1 = [it for it in items if it["segment_index"] == 1]
        assert seg1[0]["record_in"] == 16.0

    def test_lookup_finds_source_at_timestamp(self, tmp_path):
        """The whole point: map a final-video second back to its source clip."""
        edl = _cue_edl()
        out = tmp_path / "cuesheet.json"
        write_cue_sheet(edl, [12.0, 10.0], out)
        items = json.loads(out.read_text())["items"]

        def source_at(t):
            for it in items:
                if it["record_in"] <= t < it["record_out"]:
                    return it["source_file"]
            return None

        # 3-7s = IMG_001, 7-12s = VID_002 (the speech clip), 12-15s = IMG_003.
        assert source_at(5.0) == "IMG_001.jpg"
        assert source_at(9.0) == "VID_002.mp4"
        assert source_at(13.0) == "IMG_003.jpg"
        assert source_at(20.0) == "VID_004.mp4"

    def test_item_fields_recorded(self, tmp_path):
        edl = _cue_edl()
        out = tmp_path / "cuesheet.json"
        write_cue_sheet(edl, [12.0, 10.0], out)
        items = json.loads(out.read_text())["items"]
        vid = items[1]  # VID_002.mp4
        assert vid["media_type"] == "video"
        assert vid["trim_start"] == 2.0
        assert vid["trim_end"] == 7.0
        assert vid["keep_audio"] is True
        assert vid["segment_name"] == "Zoo"
        photo = items[0]  # IMG_001.jpg
        assert photo["trim_start"] is None
        assert photo["keep_audio"] is False

    def test_missing_durations_fall_back_to_display_sum(self, tmp_path):
        """If probed durations are short, fall back to summing display_duration."""
        edl = _cue_edl()
        out = tmp_path / "cuesheet.json"
        write_cue_sheet(edl, [], out)  # no probed durations
        data = json.loads(out.read_text())
        assert len(data["items"]) == 5
        # Falls back to display sums: 3 + 12 + 10 + 2 = 27s.
        assert data["total_duration"] == 27.0

    def test_boundaries_match_chapters(self, tmp_path):
        """Cue sheet segment starts must equal the chapter timestamps exactly.

        Both go through _segment_boundaries, so a per-segment first-item
        record_in must line up with that segment's chapter offset.
        """
        edl = _cue_edl()
        seg_durations = [13.0, 9.0]  # drift vs display sums on purpose
        cue = tmp_path / "cuesheet.json"
        chap = tmp_path / "chapters.txt"
        write_cue_sheet(edl, seg_durations, cue)
        write_chapters(edl, seg_durations, chap)

        items = json.loads(cue.read_text())["items"]
        # First item's record_in per segment = that segment's start.
        seg_starts = {
            it["segment_index"]: it["record_in"]
            for it in reversed(items)  # reversed → keep the first item per seg
        }
        chapter_secs = []
        for line in chap.read_text().splitlines():
            mm, ss = line.split(" ", 1)[0].split(":")
            chapter_secs.append(int(mm) * 60 + int(ss))
        # int() truncation in chapters matches int(record_in).
        assert [int(seg_starts[i]) for i in range(len(chapter_secs))] == chapter_secs
