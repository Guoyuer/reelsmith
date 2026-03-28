"""Tests for pipeline.assemble._assemble — orchestration, title cards, loudnorm parsing."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pipeline.assemble._assemble import (
    AssembleConfig,
    _find_first_frame,
    _parse_loudnorm_stats,
    _validate_output,
)
from pipeline.edl import EDL, EditItem, MusicTrack, Segment


# ---------------------------------------------------------------------------
# AssembleConfig validation
# ---------------------------------------------------------------------------


class TestAssembleConfig:
    def test_valid_config(self):
        ac = AssembleConfig(w=1920, h=1080, fps=30)
        assert ac.w == 1920

    def test_odd_width_rejected(self):
        with pytest.raises(ValueError, match="even"):
            AssembleConfig(w=1921, h=1080, fps=30)

    def test_odd_height_rejected(self):
        with pytest.raises(ValueError, match="even"):
            AssembleConfig(w=1920, h=1081, fps=30)

    def test_zero_width(self):
        with pytest.raises(ValueError, match="Invalid resolution"):
            AssembleConfig(w=0, h=1080, fps=30)

    def test_negative_height(self):
        with pytest.raises(ValueError, match="Invalid resolution"):
            AssembleConfig(w=1920, h=-1, fps=30)

    def test_zero_fps(self):
        with pytest.raises(ValueError, match="Invalid fps"):
            AssembleConfig(w=1920, h=1080, fps=0)

    def test_fps_over_120(self):
        with pytest.raises(ValueError, match="Invalid fps"):
            AssembleConfig(w=1920, h=1080, fps=121)

    def test_quality_zero(self):
        with pytest.raises(ValueError, match="Invalid quality"):
            AssembleConfig(w=1920, h=1080, fps=30, quality=0)

    def test_quality_over_5(self):
        with pytest.raises(ValueError, match="Invalid quality"):
            AssembleConfig(w=1920, h=1080, fps=30, quality=5.1)

    def test_valid_quality_range(self):
        ac = AssembleConfig(w=1920, h=1080, fps=30, quality=2.5)
        assert ac.quality == 2.5

    def test_version_optional(self):
        ac = AssembleConfig(w=1920, h=1080, fps=30)
        assert ac.version is None


# ---------------------------------------------------------------------------
# _parse_loudnorm_stats
# ---------------------------------------------------------------------------


class TestParseLoudnormStats:
    def test_parses_valid_json(self):
        stderr = (
            'some ffmpeg output\n'
            '{\n'
            '    "input_i": "-14.0",\n'
            '    "input_lra": "6.0",\n'
            '    "input_tp": "-1.0",\n'
            '    "input_thresh": "-24.0"\n'
            '}'
        )
        result = _parse_loudnorm_stats(stderr)
        assert result is not None
        assert result["input_i"] == "-14.0"
        assert result["input_lra"] == "6.0"
        assert result["input_tp"] == "-1.0"
        assert result["input_thresh"] == "-24.0"

    def test_returns_none_no_json(self):
        assert _parse_loudnorm_stats("no json here") is None

    def test_returns_none_missing_keys(self):
        stderr = '{"input_i": "-14.0", "some_other": "val"}'
        assert _parse_loudnorm_stats(stderr) is None

    def test_returns_none_invalid_json(self):
        stderr = "{not valid json"
        assert _parse_loudnorm_stats(stderr) is None

    def test_extracts_last_json_block(self):
        stderr = '{"early": 1}\nmore output\n{"input_i":"-14","input_lra":"6","input_tp":"-1","input_thresh":"-24"}'
        result = _parse_loudnorm_stats(stderr)
        assert result is not None
        assert result["input_i"] == "-14"


# ---------------------------------------------------------------------------
# _find_first_frame
# ---------------------------------------------------------------------------


class TestFindFirstFrame:
    def test_returns_first_item_source(self):
        edl = EDL(
            title="Test",
            target_duration=60,
            trip_type="family",
            style="upbeat",
            segments=[
                Segment(
                    name="S1",
                    items=[
                        EditItem(
                            source_file="/media/photo1.jpg",
                            media_type="photo",
                            display_duration=4.0,
                        ),
                    ],
                    transition="cut",
                )
            ],
        )
        assert _find_first_frame(edl) == "/media/photo1.jpg"

    def test_empty_segments(self):
        edl = EDL(
            title="Test",
            target_duration=60,
            trip_type="family",
            style="upbeat",
            segments=[],
        )
        assert _find_first_frame(edl) is None

    def test_empty_items(self):
        edl = EDL(
            title="Test",
            target_duration=60,
            trip_type="family",
            style="upbeat",
            segments=[Segment(name="Empty", items=[], transition="cut")],
        )
        assert _find_first_frame(edl) is None


# ---------------------------------------------------------------------------
# _render_title_card_if_needed
# ---------------------------------------------------------------------------


class TestRenderTitleCardIfNeeded:
    def test_no_title_returns_none(self):
        from pipeline.assemble._assemble import _render_title_card_if_needed
        from pipeline.assemble._encoder import RenderContext

        edl = EDL(
            title="",
            target_duration=60,
            trip_type="family",
            style="upbeat",
            segments=[],
        )
        ctx = RenderContext(w=1920, h=1080, fps=30)
        result = _render_title_card_if_needed(edl, "intro", Path("/tmp/intro.mp4"), ctx, "1080p30")
        assert result is None

    def test_unknown_kind_returns_none(self):
        from pipeline.assemble._assemble import _render_title_card_if_needed
        from pipeline.assemble._encoder import RenderContext

        edl = EDL(
            title="My Trip",
            target_duration=60,
            trip_type="family",
            style="upbeat",
            segments=[],
        )
        ctx = RenderContext(w=1920, h=1080, fps=30)
        result = _render_title_card_if_needed(edl, "unknown", Path("/tmp/x.mp4"), ctx, "1080p30")
        assert result is None
