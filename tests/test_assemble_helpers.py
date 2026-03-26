"""Tests for pipeline.assemble._assemble — helper functions and AssembleConfig."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from pipeline.assemble._assemble import (
    AssembleConfig,
    _find_first_photo,
    _render_title_card_if_needed,
)
from pipeline.assemble._encoder import RenderContext
from pipeline.edl import EDL, EditItem, Segment

# ---------------------------------------------------------------------------
# AssembleConfig validation
# ---------------------------------------------------------------------------


class TestAssembleConfig:
    def test_valid(self):
        ac = AssembleConfig(w=1920, h=1080, fps=30)
        assert ac.w == 1920

    def test_odd_resolution_rejected(self):
        with pytest.raises(ValueError, match="even"):
            AssembleConfig(w=1921, h=1080, fps=30)

    def test_zero_resolution_rejected(self):
        with pytest.raises(ValueError, match="Invalid resolution"):
            AssembleConfig(w=0, h=1080, fps=30)

    def test_negative_fps_rejected(self):
        with pytest.raises(ValueError, match="Invalid fps"):
            AssembleConfig(w=1920, h=1080, fps=0)

    def test_fps_over_120_rejected(self):
        with pytest.raises(ValueError, match="Invalid fps"):
            AssembleConfig(w=1920, h=1080, fps=121)

    def test_invalid_quality(self):
        with pytest.raises(ValueError, match="Invalid quality"):
            AssembleConfig(w=1920, h=1080, fps=30, quality=0)

    def test_quality_over_5_rejected(self):
        with pytest.raises(ValueError, match="Invalid quality"):
            AssembleConfig(w=1920, h=1080, fps=30, quality=6.0)

    def test_version_default_none(self):
        ac = AssembleConfig(w=1920, h=1080, fps=30)
        assert ac.version is None


# ---------------------------------------------------------------------------
# _find_first_photo
# ---------------------------------------------------------------------------


class TestFindFirstPhoto:
    def test_finds_jpg(self, tmp_path):
        photo = tmp_path / "photo.jpg"
        photo.write_bytes(b"\xff\xd8")
        edl = EDL(
            title="T",
            target_duration=60,
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
                    transition="cut",
                )
            ],
        )
        assert _find_first_photo(edl) == str(photo)

    def test_skips_videos(self):
        edl = EDL(
            title="T",
            target_duration=60,
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
        assert _find_first_photo(edl) is None

    def test_no_items(self):
        edl = EDL(title="T", target_duration=60, segments=[])
        assert _find_first_photo(edl) is None

    def test_heic_gets_converted(self, tmp_path):
        heic = tmp_path / "photo.heic"
        heic.write_bytes(b"\x00" * 100)
        edl = EDL(
            title="T",
            target_duration=60,
            segments=[
                Segment(
                    name="S",
                    items=[
                        EditItem(
                            source_file=str(heic),
                            media_type="photo",
                            display_duration=4.0,
                        )
                    ],
                    transition="cut",
                )
            ],
        )
        converted = tmp_path / "converted.jpg"
        with patch(
            "pipeline.utils.image.convert_heic", return_value=converted
        ):
            result = _find_first_photo(edl)
        assert result == str(converted)


# ---------------------------------------------------------------------------
# _render_title_card_if_needed
# ---------------------------------------------------------------------------


class TestRenderTitleCardIfNeeded:
    @pytest.fixture
    def ctx(self):
        return RenderContext(w=1920, h=1080, fps=30)

    def test_intro_title_card(self, tmp_path, ctx):
        """Returns path when intro_style=title_card and title exists."""
        edl = EDL(
            title="My Trip",
            target_duration=60,
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
            intro_style="title_card",
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
            title="T",
            target_duration=60,
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
            intro_style="none",
        )
        path = tmp_path / "intro.mp4"
        result = _render_title_card_if_needed(edl, "intro", path, ctx, "1080p30")
        assert result is None

    def test_outro_fade_title(self, tmp_path, ctx):
        edl = EDL(
            title="T",
            target_duration=60,
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
            outro_style="fade_title",
        )
        path = tmp_path / "outro.mp4"
        with patch("pipeline.assemble._assemble.render_title_card") as m:
            def _create(*a, **kw):
                path.write_bytes(b"\x00" * 100)

            m.side_effect = _create
            result = _render_title_card_if_needed(edl, "outro", path, ctx, "1080p30")
        assert result == path

    def test_skips_existing_file(self, tmp_path, ctx):
        """If title card already exists, don't re-render."""
        edl = EDL(
            title="T",
            target_duration=60,
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
            intro_style="title_card",
        )
        path = tmp_path / "intro.mp4"
        path.write_bytes(b"\x00" * 100)
        with patch("pipeline.assemble._assemble.render_title_card") as m:
            _render_title_card_if_needed(edl, "intro", path, ctx, "1080p30")
            m.assert_not_called()

    def test_render_failure_raises(self, tmp_path, ctx):
        """If render doesn't create the file, raise RuntimeError."""
        edl = EDL(
            title="T",
            target_duration=60,
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
            intro_style="title_card",
        )
        path = tmp_path / "intro.mp4"
        with patch("pipeline.assemble._assemble.render_title_card"):
            with pytest.raises(RuntimeError, match="title card render failed"):
                _render_title_card_if_needed(edl, "intro", path, ctx, "1080p30")
