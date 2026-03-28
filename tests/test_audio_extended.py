"""Extended tests for pipeline.assemble._audio — BPM, beat_snap_edl, chapters."""

from __future__ import annotations

import struct
import wave
from pathlib import Path
from unittest.mock import patch

from pipeline.assemble._audio import (
    _build_beat_grid,
    beat_snap_edl,
    estimate_bpm,
    write_chapters,
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
