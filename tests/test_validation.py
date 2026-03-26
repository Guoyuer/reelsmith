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

    @pytest.mark.parametrize(
        "kwargs, match",
        [
            ({"w": 0, "h": 1080, "fps": 30}, "Invalid resolution"),
            ({"w": 1920, "h": -1, "fps": 30}, "Invalid resolution"),
            ({"w": 1921, "h": 1080, "fps": 30}, "must be even"),
            ({"w": 1920, "h": 1081, "fps": 30}, "must be even"),
            ({"w": 1920, "h": 1080, "fps": 0}, "Invalid fps"),
            ({"w": 1920, "h": 1080, "fps": 121}, "Invalid fps"),
            ({"w": 1920, "h": 1080, "fps": 30, "quality": 0}, "Invalid quality"),
            ({"w": 1920, "h": 1080, "fps": 30, "quality": 6.0}, "Invalid quality"),
        ],
        ids=[
            "zero_width",
            "negative_height",
            "odd_width",
            "odd_height",
            "zero_fps",
            "fps_over_120",
            "zero_quality",
            "quality_over_5",
        ],
    )
    def test_invalid_raises(self, kwargs, match):
        with pytest.raises(ValueError, match=match):
            AssembleConfig(**kwargs)


# ---------------------------------------------------------------------------
# RenderContext validation
# ---------------------------------------------------------------------------


class TestRenderContextValidation:
    def test_valid_context(self):
        ctx = RenderContext(w=1920, h=1080, fps=30)
        assert ctx.w == 1920

    @pytest.mark.parametrize(
        "kwargs, match",
        [
            ({"w": 0, "h": 0, "fps": 30}, "Invalid resolution"),
            ({"w": 1921, "h": 1080, "fps": 30}, "must be even"),
            ({"w": 1920, "h": 1080, "fps": 0}, "Invalid fps"),
        ],
        ids=["zero_resolution", "odd_resolution", "zero_fps"],
    )
    def test_invalid_raises(self, kwargs, match):
        with pytest.raises(ValueError, match=match):
            RenderContext(**kwargs)


# ---------------------------------------------------------------------------
# PlanConfig validation
# ---------------------------------------------------------------------------


class TestPlanConfigValidation:
    def test_valid_config(self):
        pc = PlanConfig(target_duration=180)
        assert pc.target_duration == 180

    @pytest.mark.parametrize(
        "kwargs, match",
        [
            ({"target_duration": 0}, "must be positive"),
            ({"target_duration": -10}, "must be positive"),
            ({"target_duration": 60, "trip_type": "invalid"}, "Unknown trip_type"),
        ],
        ids=["zero_duration", "negative_duration", "invalid_trip_type"],
    )
    def test_invalid_raises(self, kwargs, match):
        with pytest.raises(ValueError, match=match):
            PlanConfig(**kwargs)

    @pytest.mark.parametrize(
        "trip_type",
        ["family", "solo", "food", "adventure", "architecture", "general"],
    )
    def test_all_valid_trip_types(self, trip_type):
        pc = PlanConfig(target_duration=60, trip_type=trip_type)
        assert pc.trip_type == trip_type


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

    @pytest.mark.parametrize(
        "edl_kwargs, expected_message",
        [
            ({"title": ""}, "no title"),
            ({"segments": []}, "No segments"),
            ({"target_duration": -1}, "Invalid target_duration"),
            ({"intro_duration": 0}, "Invalid intro_duration"),
            ({"outro_duration": 20}, "Invalid outro_duration"),
        ],
        ids=[
            "no_title",
            "no_segments",
            "negative_duration",
            "invalid_intro",
            "invalid_outro",
        ],
    )
    def test_top_level_issues(self, edl_kwargs, expected_message):
        edl = _make_edl(**edl_kwargs)
        issues = validate_edl(edl, strict=False)
        assert any(expected_message in i["message"] for i in issues)


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

    @pytest.mark.parametrize(
        "item_kwargs, expected_message",
        [
            (
                {"source_file": "/f.jpg", "media_type": "photo", "display_duration": 0},
                "display_duration <= 0",
            ),
            (
                {
                    "source_file": "/f.mp4",
                    "media_type": "video",
                    "display_duration": 5.0,
                    "effect": "ken_burns_in",
                },
                "effect='none'",
            ),
            (
                {
                    "source_file": "/f.mp4",
                    "media_type": "video",
                    "display_duration": 5.0,
                    "start_time": 10.0,
                    "end_time": 5.0,
                },
                "start_time",
            ),
            (
                {
                    "source_file": "/f.jpg",
                    "media_type": "photo",
                    "display_duration": 4.0,
                    "keep_audio": True,
                },
                "keep_audio",
            ),
            (
                {
                    "source_file": "/f.jpg",
                    "media_type": "photo",
                    "display_duration": 4.0,
                    "start_time": 0.0,
                    "end_time": 4.0,
                },
                "start_time/end_time",
            ),
            (
                {
                    "source_file": "/f.mp4",
                    "media_type": "video",
                    "display_duration": 5.0,
                    "playback_speed": 0,
                },
                "playback_speed",
            ),
        ],
        ids=[
            "zero_duration",
            "video_ken_burns",
            "start_after_end",
            "photo_keep_audio",
            "photo_with_trim",
            "zero_playback_speed",
        ],
    )
    def test_item_issues(self, item_kwargs, expected_message):
        edl = _make_edl(
            segments=[
                Segment(
                    name="S",
                    items=[EditItem(**item_kwargs)],
                    transition="cut",
                )
            ]
        )
        issues = validate_edl(edl, strict=False)
        assert any(expected_message in i["message"] for i in issues)

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
        assert any("no title" in i["message"] for i in non_strict_warnings)
        assert any("no title" in i["message"] for i in strict_errors)

    def test_music_file_missing_warns(self):
        edl = _make_edl(music=MusicTrack(file="/nonexistent/music.mp3"))
        issues = validate_edl(edl, strict=False)
        assert any("Music file not found" in i["message"] for i in issues)
