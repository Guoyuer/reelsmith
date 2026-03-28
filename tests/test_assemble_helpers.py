"""Tests for pipeline.assemble._assemble — _find_first_frame and _render_title_card_if_needed."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from pipeline.assemble._assemble import (
    _find_first_frame,
    _render_title_card_if_needed,
)
from pipeline.assemble._encoder import RenderContext
from pipeline.edl import EDL, EditItem, Segment


# ---------------------------------------------------------------------------
# _find_first_frame — video case (photo + empty covered in test_assemble_orchestrate)
# ---------------------------------------------------------------------------


class TestFindFirstFrame:
    def test_finds_video(self):
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


# ---------------------------------------------------------------------------
# _render_title_card_if_needed
# ---------------------------------------------------------------------------


class TestRenderTitleCardIfNeeded:
    @pytest.fixture
    def ctx(self):
        return RenderContext(w=1920, h=1080, fps=30)

    def test_intro_title_card(self, tmp_path, ctx):
        """Returns path when title exists."""
        edl = EDL(
            title="My Trip",
            target_duration=60,
            trip_type="family",
            style="upbeat",
            segments=[
                Segment(
                    name="S",
                    items=[
                        EditItem(
                            source_file="/fake/p.jpg",
                            media_type="photo",
                            display_duration=4.0,
                        )
                    ],
                    transition="cut",
                )
            ],
        )
        path = tmp_path / "intro.mp4"
        with patch("pipeline.assemble._assemble.render_title_card") as m:
            # Simulate render creating the file
            def _create(*a, **kw):
                path.write_bytes(b"\x00" * 100)

            m.side_effect = _create
            result = _render_title_card_if_needed(edl, "intro", path, ctx, "1080p30")
        assert result == path

    def test_intro_none_style_returns_none(self, tmp_path, ctx):
        edl = EDL(
            title="",
            target_duration=60,
            trip_type="family",
            style="upbeat",
            segments=[
                Segment(
                    name="S",
                    items=[
                        EditItem(
                            source_file="/f.jpg",
                            media_type="photo",
                            display_duration=4.0,
                        )
                    ],
                    transition="cut",
                )
            ],
        )
        path = tmp_path / "intro.mp4"
        result = _render_title_card_if_needed(edl, "intro", path, ctx, "1080p30")
        assert result is None

    def test_outro_fade_title(self, tmp_path, ctx):
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
                            source_file="/f.jpg",
                            media_type="photo",
                            display_duration=4.0,
                        )
                    ],
                    transition="cut",
                )
            ],
        )
        path = tmp_path / "outro.mp4"
        with patch("pipeline.assemble._assemble.render_title_card") as m:

            def _create(*a, **kw):
                path.write_bytes(b"\x00" * 100)

            m.side_effect = _create
            result = _render_title_card_if_needed(edl, "outro", path, ctx, "1080p30")
        assert result == path

    def test_always_re_renders(self, tmp_path, ctx):
        """Title card is always re-rendered (no caching)."""
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
                            source_file="/f.jpg",
                            media_type="photo",
                            display_duration=4.0,
                        )
                    ],
                    transition="cut",
                )
            ],
        )
        path = tmp_path / "intro.mp4"
        path.write_bytes(b"\x00" * 100)
        with patch("pipeline.assemble._assemble.render_title_card") as m:
            m.side_effect = lambda *a, **kw: path.write_bytes(b"\x00" * 100)
            _render_title_card_if_needed(edl, "intro", path, ctx, "1080p30")
            m.assert_called_once()

    def test_render_failure_raises(self, tmp_path, ctx):
        """If render doesn't create the file, raise RuntimeError."""
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
                            source_file="/f.jpg",
                            media_type="photo",
                            display_duration=4.0,
                        )
                    ],
                    transition="cut",
                )
            ],
        )
        path = tmp_path / "intro.mp4"
        with patch("pipeline.assemble._assemble.render_title_card"):
            with pytest.raises(RuntimeError, match="title card render failed"):
                _render_title_card_if_needed(edl, "intro", path, ctx, "1080p30")
