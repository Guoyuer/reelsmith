"""Tests for pipeline.assemble._assemble — helper functions."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from pipeline.assemble._assemble import (
    _find_first_frame,
    _parse_loudnorm_stats,
    _render_title_card_if_needed,
)
from pipeline.assemble._encoder import RenderContext
from pipeline.edl import EditItem, Segment
from tests.conftest import minimal_edl

# ---------------------------------------------------------------------------
# _parse_loudnorm_stats
# ---------------------------------------------------------------------------


class TestParseLoudnormStats:
    def test_parses_valid_json(self):
        stderr = (
            "some ffmpeg output\n"
            "{\n"
            '    "input_i": "-14.0",\n'
            '    "input_lra": "6.0",\n'
            '    "input_tp": "-1.0",\n'
            '    "input_thresh": "-24.0"\n'
            "}"
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
    def test_finds_photo(self):
        edl = minimal_edl(
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

    def test_finds_video(self):
        edl = minimal_edl(
            segments=[
                Segment(
                    name="S",
                    items=[
                        EditItem(
                            source_file="/fake/clip.mp4",
                            media_type="video",
                            display_duration=5.0,
                        )
                    ],
                    transition="cut",
                )
            ],
        )
        assert _find_first_frame(edl) == "/fake/clip.mp4"

    def test_empty_segments(self):
        edl = minimal_edl(segments=[])
        assert _find_first_frame(edl) is None

    def test_empty_items(self):
        edl = minimal_edl(segments=[Segment(name="Empty", items=[], transition="cut")])
        assert _find_first_frame(edl) is None


# ---------------------------------------------------------------------------
# _render_title_card_if_needed
# ---------------------------------------------------------------------------


class TestRenderTitleCardIfNeeded:
    @pytest.fixture
    def ctx(self):
        return RenderContext(w=1920, h=1080, fps=30)

    def test_intro_title_card(self, tmp_path, ctx):
        """Returns path when title exists."""
        edl = minimal_edl(title="My Trip")
        path = tmp_path / "intro.mp4"
        with patch("pipeline.assemble._assemble.render_title_card") as m:

            def _create(*a, **kw):
                path.write_bytes(b"\x00" * 100)

            m.side_effect = _create
            result = _render_title_card_if_needed(edl, "intro", path, ctx)
        assert result == path

    def test_intro_none_style_returns_none(self, tmp_path, ctx):
        edl = minimal_edl(title="")
        path = tmp_path / "intro.mp4"
        result = _render_title_card_if_needed(edl, "intro", path, ctx)
        assert result is None

    def test_outro_fade_title(self, tmp_path, ctx):
        edl = minimal_edl()
        path = tmp_path / "outro.mp4"
        with patch("pipeline.assemble._assemble.render_title_card") as m:

            def _create(*a, **kw):
                path.write_bytes(b"\x00" * 100)

            m.side_effect = _create
            result = _render_title_card_if_needed(edl, "outro", path, ctx)
        assert result == path

    def test_always_re_renders(self, tmp_path, ctx):
        """Title card is always re-rendered (no caching)."""
        edl = minimal_edl()
        path = tmp_path / "intro.mp4"
        path.write_bytes(b"\x00" * 100)
        with patch("pipeline.assemble._assemble.render_title_card") as m:
            m.side_effect = lambda *a, **kw: path.write_bytes(b"\x00" * 100)
            _render_title_card_if_needed(edl, "intro", path, ctx)
            m.assert_called_once()

    def test_render_failure_raises(self, tmp_path, ctx):
        """If render doesn't create the file, raise RuntimeError."""
        edl = minimal_edl()
        path = tmp_path / "intro.mp4"
        with patch("pipeline.assemble._assemble.render_title_card"):
            with pytest.raises(RuntimeError, match="title card render failed"):
                _render_title_card_if_needed(edl, "intro", path, ctx)

    def test_unknown_kind_returns_none(self, tmp_path, ctx):
        edl = minimal_edl(title="My Trip")
        result = _render_title_card_if_needed(
            edl, "unknown", tmp_path / "x.mp4", ctx
        )
        assert result is None
