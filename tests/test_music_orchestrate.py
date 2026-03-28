"""Tests for pipeline.music._orchestrate — composite music building."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from pipeline.config import Config
from pipeline.edl import EDL, EditItem, MusicTrack, Segment

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def music_workspace(tmp_path):
    """Minimal workspace with music-related directories."""
    ws = tmp_path / "workspace"
    for d in ("media", "render", "output", "music"):
        (ws / d).mkdir(parents=True)
    return ws


def _write_edl(ws, music_mode="auto", music_mood="gentle piano", music=None):
    """Write an EDL to the workspace and return the Config."""
    edl = EDL(
        title="Test",
        target_duration=30.0,
        music_mode=music_mode,
        trip_type="family",
        style="upbeat",
        segments=[
            Segment(
                name="test",
                music_mood=music_mood,
                items=[
                    EditItem(
                        source_file="test.jpg",
                        media_type="photo",
                        display_duration=10.0,
                    )
                ],
            )
        ],
        music=music,
    )
    (ws / "edl_v1.json").write_text(edl.model_dump_json(indent=2))
    return Config.load(str(ws))


# ---------------------------------------------------------------------------
# _segment_duration
# ---------------------------------------------------------------------------


class TestSegmentDuration:
    @pytest.mark.parametrize(
        "items, transition, trans_dur, expected",
        [
            (
                [("a.jpg", 4.0), ("b.jpg", 3.0), ("c.jpg", 5.0)],
                "crossfade",
                0.5,
                11.0,  # 4+3+5 - 2*0.5
            ),
            (
                [("a.jpg", 4.0), ("b.jpg", 3.0)],
                "cut",
                0.0,
                7.0,
            ),
            (
                [("a.jpg", 2.0)],
                "cut",
                0.0,
                5.0,  # minimum 5s
            ),
        ],
        ids=["crossfade_with_transitions", "cut_no_subtraction", "minimum_5s"],
    )
    def test_duration(self, items, transition, trans_dur, expected):
        from pipeline.music._orchestrate import _segment_duration

        seg = Segment(
            name="S",
            items=[
                EditItem(source_file=f, media_type="photo", display_duration=d)
                for f, d in items
            ],
            transition=transition,
            transition_duration=trans_dur,
        )
        assert abs(_segment_duration(seg) - expected) < 0.1


# ---------------------------------------------------------------------------
# generate_music_for_edl
# ---------------------------------------------------------------------------


class TestGenerateMusicForEdl:
    @pytest.mark.parametrize("music_mode", ["none", "file"])
    def test_skips_non_auto_modes(self, music_workspace, music_mode):
        from pipeline.music import generate_music_for_edl

        cfg = _write_edl(music_workspace, music_mode=music_mode)
        assert generate_music_for_edl(cfg) is None

    def test_returns_existing_music_file(self, music_workspace, tmp_path):
        from pipeline.music import generate_music_for_edl

        music_file = tmp_path / "existing.wav"
        music_file.write_bytes(b"RIFF" + b"\x00" * 100)
        cfg = _write_edl(music_workspace, music=MusicTrack(file=str(music_file)))
        assert generate_music_for_edl(cfg) == music_file

    def test_calls_generate_per_segment(self, music_workspace, tmp_path):
        from pipeline.music import generate_music_for_edl

        cfg = _write_edl(music_workspace)
        fake_track = tmp_path / "track.wav"
        fake_track.write_bytes(b"RIFF" + b"\x00" * 100)

        with patch(
            "pipeline.music._gemini.generate_music_gemini", return_value=fake_track
        ):
            assert generate_music_for_edl(cfg) is not None

    def test_updates_edl_on_success(self, music_workspace, tmp_path):
        from pipeline.music import generate_music_for_edl

        cfg = _write_edl(music_workspace)
        fake_track = tmp_path / "track.wav"
        fake_track.write_bytes(b"RIFF" + b"\x00" * 100)

        with patch(
            "pipeline.music._gemini.generate_music_gemini", return_value=fake_track
        ):
            generate_music_for_edl(cfg)

        edl = EDL.model_validate_json((music_workspace / "edl_v1.json").read_text())
        assert edl.music is not None

    def test_handles_generation_failure(self, music_workspace):
        from pipeline.music import generate_music_for_edl

        cfg = _write_edl(music_workspace)
        with patch("pipeline.music._gemini.generate_music_gemini", return_value=None):
            assert generate_music_for_edl(cfg) is None

    def test_progress_callback(self, music_workspace, tmp_path):
        from pipeline.music import generate_music_for_edl

        cfg = _write_edl(music_workspace)
        fake_track = tmp_path / "track.wav"
        fake_track.write_bytes(b"RIFF" + b"\x00" * 100)

        calls = []
        with patch(
            "pipeline.music._gemini.generate_music_gemini", return_value=fake_track
        ):
            generate_music_for_edl(
                cfg,
                progress_callback=lambda done, total, detail: calls.append(
                    (done, total)
                ),
            )
        assert len(calls) >= 1
