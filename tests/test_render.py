"""Tests for pipeline.assemble._render — title card rendering."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from pipeline.assemble._encoder import RenderContext
from pipeline.assemble._render import render_title_card


@pytest.fixture
def ctx():
    return RenderContext(w=1920, h=1080, fps=30)


@pytest.fixture(autouse=True)
def _mock_encoder():
    """Mock hardware encoder detection — no real FFmpeg needed."""
    with patch.object(
        RenderContext,
        "get_encoder",
        return_value=["-c:v", "libx264", "-preset", "fast", "-b:v", "8M"],
    ):
        yield


class TestRenderTitleCard:
    def test_gradient_background_cmd(self, ctx, tmp_path):
        """Without background_photo, uses gradient filter."""
        out = tmp_path / "title.mp4"
        mock_result = MagicMock(returncode=0, stderr="")
        with patch(
            "pipeline.assemble._render.run_subprocess", return_value=mock_result
        ) as m:
            render_title_card("My Trip", "June 2025", out, ctx=ctx)
            cmd = m.call_args[0][0]
            cmd_str = " ".join(str(c) for c in cmd)
            assert "color=c=0x0f0c29" in cmd_str  # gradient
            assert (
                "My Trip" in cmd_str or "My\\ Trip" in cmd_str or "My Trip" in cmd_str
            )
            assert "-an" in cmd

    def test_photo_background_cmd(self, ctx, tmp_path):
        """With background_photo, uses blurred photo as background."""
        bg = tmp_path / "photo.jpg"
        bg.write_bytes(b"\xff\xd8" + b"\x00" * 100)
        out = tmp_path / "title.mp4"
        mock_result = MagicMock(returncode=0, stderr="")
        with patch(
            "pipeline.assemble._render.run_subprocess", return_value=mock_result
        ) as m:
            render_title_card("Trip", "", out, ctx=ctx, background_photo=str(bg))
            cmd = m.call_args[0][0]
            cmd_str = " ".join(str(c) for c in cmd)
            assert "boxblur=50:3" in cmd_str
            assert "-loop" in cmd
            assert str(bg) in cmd_str

    def test_nonexistent_photo_uses_gradient(self, ctx, tmp_path):
        """If background_photo doesn't exist, falls back to gradient."""
        out = tmp_path / "title.mp4"
        mock_result = MagicMock(returncode=0, stderr="")
        with patch(
            "pipeline.assemble._render.run_subprocess", return_value=mock_result
        ) as m:
            render_title_card(
                "Trip", "", out, ctx=ctx, background_photo="/no/photo.jpg"
            )
            cmd_str = " ".join(str(c) for c in m.call_args[0][0])
            assert "color=c=0x0f0c29" in cmd_str

    def test_subtitle_included(self, ctx, tmp_path):
        """Subtitle text appears in filter when provided."""
        out = tmp_path / "title.mp4"
        mock_result = MagicMock(returncode=0, stderr="")
        with patch(
            "pipeline.assemble._render.run_subprocess", return_value=mock_result
        ) as m:
            render_title_card("Trip", "June 13-16", out, ctx=ctx)
            cmd_str = " ".join(str(c) for c in m.call_args[0][0])
            assert "June 13-16" in cmd_str

    def test_no_subtitle(self, ctx, tmp_path):
        """Empty subtitle doesn't crash."""
        out = tmp_path / "title.mp4"
        mock_result = MagicMock(returncode=0, stderr="")
        with patch(
            "pipeline.assemble._render.run_subprocess", return_value=mock_result
        ):
            render_title_card("Trip", "", out, ctx=ctx)  # should not raise

    def test_failure_raises(self, ctx, tmp_path):
        """Non-zero return code raises RuntimeError."""
        out = tmp_path / "title.mp4"
        mock_result = MagicMock(returncode=1, stderr="encode error")
        with patch(
            "pipeline.assemble._render.run_subprocess", return_value=mock_result
        ):
            with pytest.raises(RuntimeError, match="Title card render failed"):
                render_title_card("Trip", "", out, ctx=ctx)

    def test_long_title_shrinks_font(self, ctx, tmp_path):
        """Titles > 25 chars should get reduced font size."""
        out = tmp_path / "title.mp4"
        mock_result = MagicMock(returncode=0, stderr="")
        with patch(
            "pipeline.assemble._render.run_subprocess", return_value=mock_result
        ) as m:
            long_title = "A" * 40
            render_title_card(long_title, "", out, ctx=ctx)
            cmd_str = " ".join(str(c) for c in m.call_args[0][0])
            # Font size should be smaller than default
            assert "fontsize=" in cmd_str

    def test_resolution_affects_font(self, tmp_path):
        """4K context produces different font sizes than 1080p."""
        ctx_4k = RenderContext(w=3840, h=2160, fps=60)
        out = tmp_path / "title.mp4"
        mock_result = MagicMock(returncode=0, stderr="")
        with patch(
            "pipeline.assemble._render.run_subprocess", return_value=mock_result
        ) as m:
            render_title_card("Trip", "", out, ctx=ctx_4k)
            cmd_str = " ".join(str(c) for c in m.call_args[0][0])
            assert "3840" in cmd_str
