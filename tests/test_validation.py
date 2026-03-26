"""Tests for __post_init__ validation on config dataclasses and validate_edl."""

from __future__ import annotations

import pytest

from pipeline.assemble._assemble import AssembleConfig
from pipeline.assemble._encoder import RenderContext
from pipeline.edl import EDL, EditItem, MusicTrack, Segment, validate_edl
from pipeline.plan import PlanConfig

# ---------------------------------------------------------------------------
# AssembleConfig validation
# ---------------------------------------------------------------------------


class TestAssembleConfigValidation:
    def test_valid_config(self):
        ac = AssembleConfig(w=1920, h=1080, fps=30)
        assert ac.w == 1920

    def test_zero_width_raises(self):
        with pytest.raises(ValueError, match="Invalid resolution"):
            AssembleConfig(w=0, h=1080, fps=30)

    def test_negative_height_raises(self):
        with pytest.raises(ValueError, match="Invalid resolution"):
            AssembleConfig(w=1920, h=-1, fps=30)

    def test_odd_width_raises(self):
        with pytest.raises(ValueError, match="must be even"):
            AssembleConfig(w=1921, h=1080, fps=30)

    def test_odd_height_raises(self):
        with pytest.raises(ValueError, match="must be even"):
            AssembleConfig(w=1920, h=1081, fps=30)

    def test_zero_fps_raises(self):
        with pytest.raises(ValueError, match="Invalid fps"):
            AssembleConfig(w=1920, h=1080, fps=0)

    def test_fps_over_120_raises(self):
        with pytest.raises(ValueError, match="Invalid fps"):
            AssembleConfig(w=1920, h=1080, fps=121)

    def test_zero_quality_raises(self):
        with pytest.raises(ValueError, match="Invalid quality"):
            AssembleConfig(w=1920, h=1080, fps=30, quality=0)

    def test_quality_over_5_raises(self):
        with pytest.raises(ValueError, match="Invalid quality"):
            AssembleConfig(w=1920, h=1080, fps=30, quality=6.0)


# ---------------------------------------------------------------------------
# RenderContext validation
# ---------------------------------------------------------------------------


class TestRenderContextValidation:
    def test_valid_context(self):
        ctx = RenderContext(w=1920, h=1080, fps=30)
        assert ctx.w == 1920

    def test_zero_resolution_raises(self):
        with pytest.raises(ValueError, match="Invalid resolution"):
            RenderContext(w=0, h=0, fps=30)

    def test_odd_resolution_raises(self):
        with pytest.raises(ValueError, match="must be even"):
            RenderContext(w=1921, h=1080, fps=30)

    def test_zero_fps_raises(self):
        with pytest.raises(ValueError, match="Invalid fps"):
            RenderContext(w=1920, h=1080, fps=0)


# ---------------------------------------------------------------------------
# PlanConfig validation
# ---------------------------------------------------------------------------


class TestPlanConfigValidation:
    def test_valid_config(self):
        pc = PlanConfig(target_duration=180)
        assert pc.target_duration == 180

    def test_zero_duration_raises(self):
        with pytest.raises(ValueError, match="must be positive"):
            PlanConfig(target_duration=0)

    def test_negative_duration_raises(self):
        with pytest.raises(ValueError, match="must be positive"):
            PlanConfig(target_duration=-10)

    def test_invalid_trip_type_raises(self):
        with pytest.raises(ValueError, match="Unknown trip_type"):
            PlanConfig(target_duration=60, trip_type="invalid")

    def test_all_valid_trip_types(self):
        for tt in ["family", "solo", "food", "adventure", "architecture", "general"]:
            pc = PlanConfig(target_duration=60, trip_type=tt)
            assert pc.trip_type == tt


# ---------------------------------------------------------------------------
# validate_edl — comprehensive tests
# ---------------------------------------------------------------------------


def _make_edl(**kwargs) -> EDL:
    defaults = {
        "title": "Test",
        "target_duration": 60.0,
        "segments": [
            Segment(
                name="Seg1",
                items=[
                    EditItem(
                        source_file="/fake/photo.jpg",
                        media_type="photo",
                        display_duration=4.0,
                    )
                ],
                transition="crossfade",
                transition_duration=0.5,
            )
        ],
    }
    defaults.update(kwargs)
    return EDL(**defaults)


class TestValidateEdlTopLevel:
    def test_valid_edl_no_issues(self, tmp_path):
        photo = tmp_path / "photo.jpg"
        photo.write_bytes(b"\xff\xd8" + b"\x00" * 100)
        edl = _make_edl(
            segments=[
                Segment(
                    name="S",
                    items=[
                        EditItem(
                            source_file=str(photo),
                            media_type="photo",
                            display_duration=4.0,
                        )
                    ],
                    transition="crossfade",
                    transition_duration=0.5,
                )
            ]
        )
        issues = validate_edl(edl, strict=False)
        errors = [i for i in issues if i["level"] == "error"]
        assert len(errors) == 0

    def test_no_title_warns(self):
        edl = _make_edl(title="")
        issues = validate_edl(edl, strict=False)
        assert any("no title" in i["message"] for i in issues)

    def test_no_segments_errors(self):
        edl = _make_edl(segments=[])
        issues = validate_edl(edl, strict=False)
        assert any("No segments" in i["message"] for i in issues)

    def test_negative_target_duration_errors(self):
        edl = _make_edl(target_duration=-1)
        issues = validate_edl(edl, strict=False)
        assert any("Invalid target_duration" in i["message"] for i in issues)

    def test_invalid_intro_duration_errors(self):
        edl = _make_edl(intro_duration=0)
        issues = validate_edl(edl, strict=False)
        assert any("Invalid intro_duration" in i["message"] for i in issues)

    def test_invalid_outro_duration_errors(self):
        edl = _make_edl(outro_duration=20)
        issues = validate_edl(edl, strict=False)
        assert any("Invalid outro_duration" in i["message"] for i in issues)


class TestValidateEdlItems:
    def test_missing_source_file_errors(self):
        edl = _make_edl()  # uses /fake/photo.jpg which doesn't exist
        issues = validate_edl(edl, strict=False)
        assert any("source file not found" in i["message"] for i in issues)

    def test_duplicate_source_warns(self, tmp_path):
        photo = tmp_path / "photo.jpg"
        photo.write_bytes(b"\xff\xd8" + b"\x00" * 100)
        edl = _make_edl(
            segments=[
                Segment(
                    name="S",
                    items=[
                        EditItem(
                            source_file=str(photo),
                            media_type="photo",
                            display_duration=4.0,
                        ),
                        EditItem(
                            source_file=str(photo),
                            media_type="photo",
                            display_duration=3.0,
                        ),
                    ],
                    transition="crossfade",
                    transition_duration=0.5,
                )
            ]
        )
        issues = validate_edl(edl, strict=False)
        assert any("duplicate" in i["message"] for i in issues)

    def test_zero_display_duration_errors(self):
        edl = _make_edl(
            segments=[
                Segment(
                    name="S",
                    items=[
                        EditItem(
                            source_file="/f.jpg", media_type="photo", display_duration=0
                        )
                    ],
                    transition="cut",
                )
            ]
        )
        issues = validate_edl(edl, strict=False)
        assert any("display_duration <= 0" in i["message"] for i in issues)

    def test_video_with_ken_burns_errors(self):
        edl = _make_edl(
            segments=[
                Segment(
                    name="S",
                    items=[
                        EditItem(
                            source_file="/f.mp4",
                            media_type="video",
                            display_duration=5.0,
                            effect="ken_burns_in",
                        )
                    ],
                    transition="cut",
                )
            ]
        )
        issues = validate_edl(edl, strict=False)
        assert any("effect='none'" in i["message"] for i in issues)

    def test_video_start_after_end_errors(self):
        edl = _make_edl(
            segments=[
                Segment(
                    name="S",
                    items=[
                        EditItem(
                            source_file="/f.mp4",
                            media_type="video",
                            display_duration=5.0,
                            start_time=10.0,
                            end_time=5.0,
                        )
                    ],
                    transition="cut",
                )
            ]
        )
        issues = validate_edl(edl, strict=False)
        assert any(
            "start_time" in i["message"] and "end_time" in i["message"] for i in issues
        )

    def test_photo_with_keep_audio_errors(self):
        edl = _make_edl(
            segments=[
                Segment(
                    name="S",
                    items=[
                        EditItem(
                            source_file="/f.jpg",
                            media_type="photo",
                            display_duration=4.0,
                            keep_audio=True,
                        )
                    ],
                    transition="cut",
                )
            ]
        )
        issues = validate_edl(edl, strict=False)
        assert any("keep_audio" in i["message"] for i in issues)

    def test_photo_with_trim_errors(self):
        edl = _make_edl(
            segments=[
                Segment(
                    name="S",
                    items=[
                        EditItem(
                            source_file="/f.jpg",
                            media_type="photo",
                            display_duration=4.0,
                            start_time=0.0,
                            end_time=4.0,
                        )
                    ],
                    transition="cut",
                )
            ]
        )
        issues = validate_edl(edl, strict=False)
        assert any("start_time/end_time" in i["message"] for i in issues)

    def test_transition_duration_exceeds_clip_errors(self, tmp_path):
        photo = tmp_path / "p.jpg"
        photo.write_bytes(b"\xff\xd8" + b"\x00" * 100)
        edl = _make_edl(
            segments=[
                Segment(
                    name="S",
                    items=[
                        EditItem(
                            source_file=str(photo),
                            media_type="photo",
                            display_duration=2.0,
                        ),
                        EditItem(
                            source_file=str(photo),
                            media_type="photo",
                            display_duration=1.5,
                        ),
                    ],
                    transition="crossfade",
                    transition_duration=2.0,
                )
            ]
        )
        issues = validate_edl(edl, strict=False)
        assert any(
            "transition_duration" in i["message"] and "shortest" in i["message"]
            for i in issues
        )

    def test_strict_promotes_warnings_to_errors(self):
        edl = _make_edl(title="")
        strict_issues = validate_edl(edl, strict=True)
        non_strict_issues = validate_edl(edl, strict=False)
        strict_errors = [i for i in strict_issues if i["level"] == "error"]
        non_strict_warnings = [i for i in non_strict_issues if i["level"] == "warning"]
        # "no title" should be warning in non-strict but error in strict
        assert any("no title" in i["message"] for i in non_strict_warnings)
        assert any("no title" in i["message"] for i in strict_errors)

    def test_music_file_missing_warns(self):
        edl = _make_edl(music=MusicTrack(file="/nonexistent/music.mp3"))
        issues = validate_edl(edl, strict=False)
        assert any("Music file not found" in i["message"] for i in issues)

    def test_invalid_playback_speed_errors(self):
        edl = _make_edl(
            segments=[
                Segment(
                    name="S",
                    items=[
                        EditItem(
                            source_file="/f.mp4",
                            media_type="video",
                            display_duration=5.0,
                            playback_speed=0,
                        )
                    ],
                    transition="cut",
                )
            ]
        )
        issues = validate_edl(edl, strict=False)
        assert any("playback_speed" in i["message"] for i in issues)
