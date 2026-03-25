"""Tests for pipeline.music._orchestrate — composite music building."""

from __future__ import annotations

from unittest.mock import patch

from pipeline.config import Config
from pipeline.edl import EDL, EditItem, MusicTrack, Segment


class TestBuildCompositeMusic:
    def test_single_segment_copies(self, tmp_path):
        from pipeline.music._orchestrate import _build_composite_music

        track = tmp_path / "track.wav"
        track.write_bytes(b"RIFF" + b"\x00" * 100)
        out = tmp_path / "composite.wav"

        result = _build_composite_music([(10.0, track)], out)
        assert result is True
        assert out.exists()

    def test_empty_returns_false(self, tmp_path):
        from pipeline.music._orchestrate import _build_composite_music

        out = tmp_path / "composite.wav"
        result = _build_composite_music([], out)
        assert result is False


class TestSegmentDuration:
    def test_narrative_with_transitions(self):
        from pipeline.music._orchestrate import _segment_duration

        seg = Segment(
            name="S",
            items=[
                EditItem(source_file="a.jpg", media_type="photo", display_duration=4.0),
                EditItem(source_file="b.jpg", media_type="photo", display_duration=3.0),
                EditItem(source_file="c.jpg", media_type="photo", display_duration=5.0),
            ],
            transition="crossfade",
            transition_duration=0.5,
        )
        dur = _segment_duration(seg)
        # 4 + 3 + 5 - 2*0.5 = 11.0
        assert abs(dur - 11.0) < 0.1

    def test_cut_no_subtraction(self):
        from pipeline.music._orchestrate import _segment_duration

        seg = Segment(
            name="S",
            items=[
                EditItem(source_file="a.jpg", media_type="photo", display_duration=4.0),
                EditItem(source_file="b.jpg", media_type="photo", display_duration=3.0),
            ],
            transition="cut",
            transition_duration=0.0,
        )
        dur = _segment_duration(seg)
        assert abs(dur - 7.0) < 0.1

    def test_minimum_5s(self):
        from pipeline.music._orchestrate import _segment_duration

        seg = Segment(
            name="S",
            items=[
                EditItem(source_file="a.jpg", media_type="photo", display_duration=2.0)
            ],
            transition="cut",
        )
        dur = _segment_duration(seg)
        assert abs(dur - 5.0) < 0.01  # minimum 5s


class TestGenerateMusicForEdl:
    def _make_workspace(self, tmp_path, music_mode="auto", music_mood="gentle piano"):
        ws = tmp_path / "workspace"
        for d in ("media", "clips", "output", "music"):
            (ws / d).mkdir(parents=True)
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
        )
        (ws / "edl_v1.json").write_text(edl.model_dump_json(indent=2))
        return ws

    def test_skips_when_music_mode_none(self, tmp_path):
        from pipeline.music import generate_music_for_edl

        ws = self._make_workspace(tmp_path, music_mode="none")
        cfg = Config.load(str(ws))
        result = generate_music_for_edl(cfg)
        assert result is None

    def test_returns_existing_music_file(self, tmp_path):
        from pipeline.music import generate_music_for_edl

        music_file = tmp_path / "existing.wav"
        music_file.write_bytes(b"RIFF" + b"\x00" * 100)

        ws = self._make_workspace(tmp_path)
        cfg = Config.load(str(ws))

        # Manually set music on the EDL
        edl_path = ws / "edl_v1.json"
        edl = EDL.model_validate_json(edl_path.read_text())
        edl.music = MusicTrack(file=str(music_file))
        edl_path.write_text(edl.model_dump_json(indent=2))

        result = generate_music_for_edl(cfg)
        assert result == music_file

    def test_calls_generate_per_segment(self, tmp_path):
        from pipeline.music import generate_music_for_edl

        ws = self._make_workspace(tmp_path)
        cfg = Config.load(str(ws))

        fake_track = tmp_path / "track.wav"
        fake_track.write_bytes(b"RIFF" + b"\x00" * 100)

        with patch(
            "pipeline.music._gemini.generate_music_gemini", return_value=fake_track
        ):
            result = generate_music_for_edl(cfg)

        assert result is not None

    def test_progress_callback(self, tmp_path):
        from pipeline.music import generate_music_for_edl

        ws = self._make_workspace(tmp_path)
        cfg = Config.load(str(ws))

        fake_track = tmp_path / "track.wav"
        fake_track.write_bytes(b"RIFF" + b"\x00" * 100)

        calls = []

        def cb(done, total, detail):
            calls.append((done, total))

        with patch(
            "pipeline.music._gemini.generate_music_gemini", return_value=fake_track
        ):
            generate_music_for_edl(cfg, progress_callback=cb)

        assert len(calls) >= 1
