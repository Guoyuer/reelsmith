"""Tests for pipeline.assemble._graph — filter graph builder.

Covers: compute_fade_params, _fade_expr, _overlay_vf, _photo_filter,
_video_filter, build_segment_graph (with mocked probe_dimensions).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline.assemble._encoder import RenderContext
from pipeline.assemble._graph import (
    SegmentGraph,
    _blurred_bg,
    _fade_expr,
    _next_item,
    _overlay_vf,
    _photo_filter,
    _video_filter,
    build_segment_graph,
    compute_fade_params,
)
from pipeline.edl import EDL, EditItem, Segment, TextOverlay

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CTX = RenderContext(w=1920, h=1080, fps=30)


def _photo(duration: float = 4.0, effect: str = "ken_burns_in", **kw) -> EditItem:
    return EditItem(
        source_file="/fake/photo.jpg",
        media_type="photo",
        display_duration=duration,
        effect=effect,
        **kw,
    )


def _video(
    duration: float = 5.0,
    start: float | None = None,
    end: float | None = None,
    keep_audio: bool = False,
    speed: float = 1.0,
    source_file: str = "/fake/clip.mp4",
    **kw,
) -> EditItem:
    return EditItem(
        source_file=source_file,
        media_type="video",
        display_duration=duration,
        start_time=start,
        end_time=end,
        keep_audio=keep_audio,
        playback_speed=speed,
        effect="none",
        **kw,
    )


def _seg(items: list[EditItem], **kw) -> Segment:
    kw.setdefault("name", "Seg")
    kw.setdefault("transition", "crossfade")
    kw.setdefault("transition_duration", 0.5)
    kw.setdefault("segment_transition_duration", 1.0)
    return Segment(items=items, **kw)


def _edl(segments: list[Segment], **kw) -> EDL:
    kw.setdefault("title", "Test")
    kw.setdefault("target_duration", 60.0)
    kw.setdefault("trip_type", "family")
    kw.setdefault("style", "upbeat")
    return EDL(segments=segments, **kw)


# ---------------------------------------------------------------------------
# _fade_expr
# ---------------------------------------------------------------------------


class TestFadeExpr:
    def test_no_fades(self):
        result = _fade_expr(4.0, 0.0, 0.0)
        assert result.startswith(",")
        assert "format=yuv420p" in result
        assert "setsar=1" in result
        assert "trim=duration=4.000000" in result
        # No fade filters
        assert "fade=t=in" not in result
        assert "fade=t=out" not in result

    def test_fade_in_only(self):
        result = _fade_expr(5.0, 0.8, 0.0)
        assert "fade=t=in:d=0.8" in result
        assert "fade=t=out" not in result

    def test_fade_out_only(self):
        result = _fade_expr(5.0, 0.0, 1.0)
        assert "fade=t=in" not in result
        assert "fade=t=out:st=4.000:d=1.0" in result

    def test_both_fades(self):
        result = _fade_expr(6.0, 0.5, 1.0)
        assert "fade=t=in:d=0.5" in result
        assert "fade=t=out:st=5.000:d=1.0" in result

    def test_fade_out_start_clamps_to_zero(self):
        # fade_out longer than duration — st should be 0
        result = _fade_expr(1.0, 0.0, 3.0)
        assert "fade=t=out:st=0.000:d=3.0" in result

    def test_always_has_trim_and_final_setpts(self):
        result = _fade_expr(3.5, 0.0, 0.0)
        assert "trim=duration=3.500000" in result
        # setpts appears twice: once for reset, once after trim
        assert result.count("setpts=PTS-STARTPTS") == 2


# ---------------------------------------------------------------------------
# _overlay_vf
# ---------------------------------------------------------------------------


class TestOverlayVf:
    def test_no_overlay(self):
        item = _photo()
        assert _overlay_vf(item, "en", 1080) == ""

    def test_with_overlay(self):
        item = _photo(text_overlay=TextOverlay(text="Hello", position="bottom"))
        result = _overlay_vf(item, "en", 1080)
        assert result.startswith(",")
        assert "drawtext=" in result
        assert "Hello" in result

    def test_overlay_escapes_special_chars(self):
        item = _photo(text_overlay=TextOverlay(text="A: B [C]"))
        result = _overlay_vf(item, "en", 1080)
        assert "\\:" in result
        assert "\\[" in result


# ---------------------------------------------------------------------------
# _photo_filter
# ---------------------------------------------------------------------------


class TestPhotoFilter:
    def test_basic_structure(self):
        item = _photo(duration=4.0)
        seg = _seg([item])
        result = _photo_filter(0, item, seg, _CTX, 0.0, 0.0, "en")
        # Blurred background pipeline
        assert "loop=loop=" in result
        assert "split [bg0][fg0]" in result
        assert "boxblur=50:3" in result
        assert "overlay=(W-w)/2:(H-h)/2" in result
        # Ken Burns
        assert "crop=" in result
        assert "scale=1920:1080:flags=lanczos" in result
        # Output label
        assert result.endswith("[v0]")
        # Color grade
        assert "eq=contrast=" in result
        # No unsharp (removed for performance; imperceptible on compressed output)

    def test_with_fades(self):
        item = _photo()
        seg = _seg([item])
        result = _photo_filter(0, item, seg, _CTX, 0.5, 1.0, "en")
        assert "fade=t=in:d=0.5" in result
        assert "fade=t=out" in result

    def test_ken_burns_directions(self):
        for effect, expected_dir in [
            ("ken_burns_in", "in"),
            ("ken_burns_out", "out"),
            ("ken_burns_left", "left"),
            ("ken_burns_right", "right"),
            ("none", "static"),
        ]:
            item = _photo(effect=effect)
            seg = _seg([item])
            result = _photo_filter(0, item, seg, _CTX, 0.0, 0.0, "en")
            # "none" has zoom=1, others have zoom expressions
            if expected_dir == "static":
                # For static, ken_burns_filter uses zoom="1"
                assert "crop=w='trunc(iw/(1)/2)*2'" in result
            else:
                assert "crop=" in result

    def test_color_temp_warm(self):
        item = _photo()
        seg = _seg([item], color_temp="warm")
        result = _photo_filter(0, item, seg, _CTX, 0.0, 0.0, "en")
        assert "colorbalance=rs=0.02" in result

    def test_color_temp_cool(self):
        item = _photo()
        seg = _seg([item], color_temp="cool")
        result = _photo_filter(0, item, seg, _CTX, 0.0, 0.0, "en")
        assert "colorbalance=rs=-0.02" in result

    def test_with_text_overlay(self):
        item = _photo(text_overlay=TextOverlay(text="Dawn"))
        seg = _seg([item])
        result = _photo_filter(0, item, seg, _CTX, 0.0, 0.0, "en")
        assert "drawtext=" in result
        assert "Dawn" in result

    def test_index_propagated(self):
        """Second item in segment uses idx=1 for all labels."""
        item = _photo()
        seg = _seg([item])
        result = _photo_filter(3, item, seg, _CTX, 0.0, 0.0, "en")
        assert "split [bg3][fg3]" in result
        assert "[blurred3]" in result
        assert "[sharp3]" in result
        assert result.endswith("[v3]")

    def test_trim_duration_matches_frame_aligned(self):
        """Display duration is frame-aligned (frames / fps)."""
        item = _photo(duration=3.7)
        seg = _seg([item])
        result = _photo_filter(0, item, seg, _CTX, 0.0, 0.0, "en")
        frames = int(3.7 * 30)  # 111 frames
        exact_dur = frames / 30  # 3.7
        assert f"trim=duration={exact_dur:.6f}" in result


# ---------------------------------------------------------------------------
# _video_filter
# ---------------------------------------------------------------------------


class TestVideoFilter:
    @pytest.fixture(autouse=True)
    def _mock_probe(self):
        """Mock probe_dimensions so no FFmpeg is needed."""
        with patch.object(
            RenderContext,
            "probe_dimensions",
            return_value=(1920, 1080),
        ):
            yield

    def test_landscape_16_9_no_blur(self):
        """16:9 landscape video uses simple scale+pad, no blurred bg."""
        item = _video(duration=5.0)
        seg = _seg([item])
        result = _video_filter(0, item, seg, _CTX, 0.0, 0.0, "en")
        assert "scale=1920:1080:force_original_aspect_ratio=decrease" in result
        assert "pad=1920:1080" in result
        assert "boxblur" not in result
        assert result.endswith("[v0]")

    def test_portrait_gets_blurred_bg(self):
        """Portrait video should get blurred background composite."""
        with patch.object(RenderContext, "probe_dimensions", return_value=(1080, 1920)):
            item = _video(duration=5.0)
            seg = _seg([item])
            result = _video_filter(0, item, seg, _CTX, 0.0, 0.0, "en")
            assert "split [bg0][fg0]" in result
            assert "boxblur=50:3" in result
            assert "overlay=(W-w)/2:(H-h)/2" in result

    def test_non_16_9_landscape_gets_blur(self):
        """Ultra-wide (2.35:1) should get blurred bg."""
        with patch.object(RenderContext, "probe_dimensions", return_value=(2560, 1080)):
            item = _video(duration=5.0)
            seg = _seg([item])
            result = _video_filter(0, item, seg, _CTX, 0.0, 0.0, "en")
            assert "boxblur=50:3" in result

    def test_trim_in_filter(self):
        """Video with start_time/end_time uses trim filter, not -ss/-t."""
        item = _video(duration=5.0, start=10.0, end=15.0)
        seg = _seg([item])
        result = _video_filter(0, item, seg, _CTX, 0.0, 0.0, "en")
        assert "trim=start=10.0:duration=5.0" in result
        assert "setpts=PTS-STARTPTS" in result

    def test_no_explicit_trim_uses_display_duration(self):
        """Video without start_time/end_time trims to display_duration."""
        item = _video(duration=5.0)
        seg = _seg([item])
        result = _video_filter(0, item, seg, _CTX, 0.0, 0.0, "en")
        assert "trim=start=0.0:duration=5.0" in result

    def test_speed_change(self):
        """Playback speed != 1.0 adds setpts filter."""
        item = _video(duration=10.0, start=0.0, end=10.0, speed=0.5)
        seg = _seg([item])
        result = _video_filter(0, item, seg, _CTX, 0.0, 0.0, "en")
        assert "setpts=2.0000*PTS" in result

    def test_normal_speed_no_setpts(self):
        """Speed 1.0 should NOT add speed setpts filter."""
        item = _video(duration=5.0, speed=1.0)
        seg = _seg([item])
        result = _video_filter(0, item, seg, _CTX, 0.0, 0.0, "en")
        # _fade_expr adds setpts=PTS-STARTPTS, but no multiplier
        assert "*PTS" not in result

    def test_fades(self):
        item = _video(duration=5.0)
        seg = _seg([item])
        result = _video_filter(0, item, seg, _CTX, 0.5, 1.0, "en")
        assert "fade=t=in:d=0.5" in result
        assert "fade=t=out" in result

    def test_fps_at_end(self):
        """Output always ends with fps=N before label."""
        item = _video(duration=5.0)
        seg = _seg([item])
        result = _video_filter(0, item, seg, _CTX, 0.0, 0.0, "en")
        assert "fps=30 [v0]" in result

    def test_color_temp(self):
        item = _video(duration=5.0)
        seg = _seg([item], color_temp="cool")
        result = _video_filter(0, item, seg, _CTX, 0.0, 0.0, "en")
        assert "colorbalance=rs=-0.02" in result

    def test_with_text_overlay(self):
        item = _video(
            duration=5.0,
            text_overlay=TextOverlay(text="Market"),
        )
        seg = _seg([item])
        result = _video_filter(0, item, seg, _CTX, 0.0, 0.0, "en")
        assert "drawtext=" in result
        assert "Market" in result

    def test_zero_dimensions_no_blur(self):
        """When probe returns (0, 0), no aspect fill (guard against division by zero)."""
        with patch.object(RenderContext, "probe_dimensions", return_value=(0, 0)):
            item = _video(duration=5.0)
            seg = _seg([item])
            result = _video_filter(0, item, seg, _CTX, 0.0, 0.0, "en")
            assert "boxblur" not in result


# ---------------------------------------------------------------------------
# _blurred_bg
# ---------------------------------------------------------------------------


class TestBlurredBg:
    def test_contains_blur_and_overlay(self):
        result = _blurred_bg(0, 1920, 1080, 50)
        assert "boxblur=50:3" in result
        assert "overlay=(W-w)/2:(H-h)/2" in result
        assert "[blurred0]" in result
        assert "[sharp0]" in result

    def test_blur_radius_parameterized(self):
        r50 = _blurred_bg(0, 1920, 1080, 50)
        r60 = _blurred_bg(0, 1920, 1080, 60)
        assert "boxblur=50:3" in r50
        assert "boxblur=60:3" in r60
        assert "boxblur=50:3" not in r60

    def test_index_propagated(self):
        result = _blurred_bg(3, 1920, 1080, 50)
        assert "[bg3]" in result
        assert "[fg3]" in result
        assert "[blurred3]" in result
        assert "[sharp3]" in result

    def test_no_trailing_comma(self):
        """Caller adds the comma — _blurred_bg should not end with one."""
        result = _blurred_bg(0, 1920, 1080, 50)
        assert not result.endswith(",")


# ---------------------------------------------------------------------------
# _next_item
# ---------------------------------------------------------------------------


class TestNextItem:
    def test_next_in_same_segment(self):
        s = _seg([_photo(), _photo(), _photo()])
        edl = _edl([s])
        seg, idx = _next_item(edl, 0, 0)
        assert seg is edl.segments[0]
        assert idx == 1

    def test_cross_segment_boundary(self):
        s1 = _seg([_photo()], name="S1")
        s2 = _seg([_photo()], name="S2")
        edl = _edl([s1, s2])
        seg, idx = _next_item(edl, 0, 0)
        assert seg is edl.segments[1]
        assert idx == 0

    def test_last_item_returns_none(self):
        s = _seg([_photo()])
        edl = _edl([s])
        seg, idx = _next_item(edl, 0, 0)
        assert seg is None
        assert idx == 0

    def test_middle_of_segment(self):
        s = _seg([_photo(), _photo(), _photo()])
        edl = _edl([s])
        seg, idx = _next_item(edl, 0, 1)
        assert seg is edl.segments[0]
        assert idx == 2


# ---------------------------------------------------------------------------
# compute_fade_params
# ---------------------------------------------------------------------------


class TestComputeFadeParams:
    def test_single_segment_single_item(self):
        """One segment, one item: no fades."""
        edl = _edl([_seg([_photo()])])
        fades = compute_fade_params(edl)
        assert fades == [[(0.0, 0.0)]]

    def test_single_segment_multiple_items_crossfade(self):
        """Within a crossfade segment, adjacent items get fade_out = transition_duration."""
        edl = _edl(
            [
                _seg(
                    [_photo(), _photo(), _photo()],
                    transition="crossfade",
                    transition_duration=0.5,
                )
            ]
        )
        fades = compute_fade_params(edl)
        assert len(fades) == 1
        assert len(fades[0]) == 3
        # First item: no fade_in (seg_idx=0), fade_out to next intra-segment item
        assert fades[0][0] == (0.0, 0.5)
        # Middle item: no fade_in (not first in segment), fade_out to next
        assert fades[0][1] == (0.0, 0.5)
        # Last item: no fade_in, no fade_out (no next item)
        assert fades[0][2] == (0.0, 0.0)

    def test_single_segment_cut_transition(self):
        """Cut transitions produce zero fade_out."""
        edl = _edl(
            [
                _seg(
                    [_photo(), _photo()],
                    transition="cut",
                    transition_duration=0.5,
                )
            ]
        )
        fades = compute_fade_params(edl)
        # Cut means fade_out = 0
        assert fades[0][0] == (0.0, 0.0)
        assert fades[0][1] == (0.0, 0.0)

    def test_two_segments_boundary_fades(self):
        """First item of second segment gets fade_in = segment_transition_duration.
        Last item of first segment gets fade_out = next segment's segment_transition_duration.
        """
        seg1 = _seg(
            [_photo(), _photo()],
            name="S1",
            transition="crossfade",
            transition_duration=0.4,
        )
        seg2 = _seg(
            [_photo(), _photo()],
            name="S2",
            segment_transition_duration=1.0,
            transition="crossfade",
            transition_duration=0.3,
        )
        edl = _edl([seg1, seg2])
        fades = compute_fade_params(edl)
        assert len(fades) == 2

        # S1 item[0]: fade_out = intra-segment transition_duration of S1
        assert fades[0][0] == (0.0, 0.4)
        # S1 item[1] (last in S1): fade_out = S2's segment_transition_duration
        assert fades[0][1] == (0.0, 1.0)
        # S2 item[0] (first in S2, seg_idx > 0): fade_in = S2's segment_transition_duration
        assert fades[1][0] == (1.0, 0.3)
        # S2 item[1]: last overall, no fade_out
        assert fades[1][1] == (0.0, 0.0)

    def test_montage_segment_no_fades(self):
        """Montage mode segments produce zero fades for all items."""
        edl = _edl(
            [
                _seg(
                    [_photo(), _photo(), _photo()],
                    mode="montage",
                    transition="crossfade",
                    transition_duration=0.5,
                )
            ]
        )
        fades = compute_fade_params(edl)
        assert all(f == (0.0, 0.0) for f in fades[0])

    def test_montage_after_narrative_no_fade_out(self):
        """Narrative item before a montage segment gets no fade_out."""
        seg1 = _seg([_photo()], name="S1")
        seg2 = _seg([_photo(), _photo()], name="S2", mode="montage")
        edl = _edl([seg1, seg2])
        fades = compute_fade_params(edl)
        # S1's last item: next segment is montage, so fade_out = 0
        assert fades[0][0][1] == 0.0

    def test_narrative_after_montage_gets_fade_in(self):
        """First item of a narrative segment after montage gets normal fade_in."""
        seg1 = _seg([_photo()], name="S1", mode="montage")
        seg2 = _seg(
            [_photo()],
            name="S2",
            mode="narrative",
            segment_transition_duration=0.8,
        )
        edl = _edl([seg1, seg2])
        fades = compute_fade_params(edl)
        # S1 (montage): no fades
        assert fades[0][0] == (0.0, 0.0)
        # S2 item[0]: fade_in from segment_transition, but S1 was montage
        # so fade_in still applies (it's this segment's property)
        assert fades[1][0][0] == 0.8

    def test_three_segments(self):
        """Three segments: verify correct segment grouping."""
        s1 = _seg([_photo()], name="S1")
        s2 = _seg([_photo()], name="S2", segment_transition_duration=1.0)
        s3 = _seg([_photo()], name="S3", segment_transition_duration=0.5)
        edl = _edl([s1, s2, s3])
        fades = compute_fade_params(edl)
        assert len(fades) == 3
        assert len(fades[0]) == 1
        assert len(fades[1]) == 1
        assert len(fades[2]) == 1

    def test_empty_edl(self):
        """EDL with no segments returns empty list."""
        edl = _edl([])
        fades = compute_fade_params(edl)
        assert fades == []

    def test_mixed_items_per_segment(self):
        """Verify fade counts match item counts per segment."""
        s1 = _seg([_photo(), _photo(), _photo()], name="S1")
        s2 = _seg([_photo()], name="S2")
        s3 = _seg([_photo(), _photo()], name="S3")
        edl = _edl([s1, s2, s3])
        fades = compute_fade_params(edl)
        assert len(fades[0]) == 3
        assert len(fades[1]) == 1
        assert len(fades[2]) == 2


# ---------------------------------------------------------------------------
# build_segment_graph
# ---------------------------------------------------------------------------


class TestBuildSegmentGraph:
    @pytest.fixture(autouse=True)
    def _mock_probe(self):
        with patch.object(
            RenderContext,
            "probe_dimensions",
            return_value=(1920, 1080),
        ):
            yield

    def test_single_photo(self):
        """Single photo produces: 1 input, concat=n=1, silence, no speech."""
        seg = _seg([_photo(duration=3.0)])
        graph = build_segment_graph(seg, _CTX, fade_params=[(0.0, 0.0)])
        assert len(graph.inputs) == 1
        assert "concat=n=1:v=1:a=1" in graph.script
        assert "aevalsrc=0" in graph.script
        assert "-i" in graph.inputs[0]
        assert "loop=loop=" in graph.script  # loop filter in filter chain

    def test_single_video_no_audio(self):
        """Video with keep_audio=False: silence track, no speech."""
        seg = _seg([_video(duration=5.0)])
        graph = build_segment_graph(seg, _CTX, fade_params=[(0.0, 0.0)])
        assert len(graph.inputs) == 1
        assert "aevalsrc=0" in graph.script
        assert "atrim" not in graph.script

    def test_video_keep_audio(self):
        """Video with keep_audio=True: atrim audio preserved."""
        seg = _seg([_video(duration=5.0, start=2.0, end=7.0, keep_audio=True)])
        graph = build_segment_graph(seg, _CTX, fade_params=[(0.0, 0.0)])
        assert "atrim=start=2.0:duration=5.0" in graph.script
        assert "asetpts=PTS-STARTPTS" in graph.script

    def test_video_keep_audio_with_speed(self):
        """keep_audio + speed != 1.0 adds atempo filter."""
        seg = _seg(
            [_video(duration=10.0, start=0.0, end=10.0, keep_audio=True, speed=1.5)]
        )
        graph = build_segment_graph(seg, _CTX, fade_params=[(0.0, 0.0)])
        assert "atempo=1.5" in graph.script

    def test_video_keep_audio_normal_speed_no_atempo(self):
        """keep_audio at speed=1.0 should NOT add atempo."""
        seg = _seg(
            [_video(duration=5.0, start=0.0, end=5.0, keep_audio=True, speed=1.0)]
        )
        graph = build_segment_graph(seg, _CTX, fade_params=[(0.0, 0.0)])
        assert "atempo" not in graph.script

    def test_multiple_items_concat(self):
        """Two items produce concat=n=2."""
        seg = _seg([_photo(), _video(duration=5.0)])
        graph = build_segment_graph(seg, _CTX, fade_params=[(0.0, 0.0), (0.0, 0.0)])
        assert len(graph.inputs) == 2
        assert "concat=n=2:v=1:a=1" in graph.script

    def test_title_card(self, tmp_path):
        """Title card adds an extra input and concat count increases."""
        title = tmp_path / "title.mp4"
        title.write_bytes(b"\x00" * 100)
        seg = _seg([_photo()])
        graph = build_segment_graph(
            seg,
            _CTX,
            fade_params=[(0.0, 0.0)],
            title_card_path=title,
            intro_duration=3.0,
        )
        assert len(graph.inputs) == 2  # title + photo
        assert "concat=n=2:v=1:a=1" in graph.script
        # Title card gets silence
        assert "aevalsrc=0:d=3.0" in graph.script

    def test_outro_card(self, tmp_path):
        """Outro card adds an extra input at the end."""
        outro = tmp_path / "outro.mp4"
        outro.write_bytes(b"\x00" * 100)
        seg = _seg([_photo()])
        graph = build_segment_graph(
            seg,
            _CTX,
            fade_params=[(0.0, 0.0)],
            outro_card_path=outro,
            outro_duration=2.5,
        )
        assert len(graph.inputs) == 2  # photo + outro
        assert "concat=n=2:v=1:a=1" in graph.script
        assert "aevalsrc=0:d=2.5" in graph.script

    def test_title_and_outro(self, tmp_path):
        """Both title and outro: 3 inputs total."""
        title = tmp_path / "title.mp4"
        title.write_bytes(b"\x00" * 100)
        outro = tmp_path / "outro.mp4"
        outro.write_bytes(b"\x00" * 100)
        seg = _seg([_photo()])
        graph = build_segment_graph(
            seg,
            _CTX,
            fade_params=[(0.0, 0.0)],
            title_card_path=title,
            intro_duration=3.0,
            outro_card_path=outro,
            outro_duration=2.0,
        )
        assert len(graph.inputs) == 3
        assert "concat=n=3:v=1:a=1" in graph.script

    def test_nonexistent_title_card_skipped(self):
        """Title card path that doesn't exist is ignored."""
        seg = _seg([_photo()])
        graph = build_segment_graph(
            seg,
            _CTX,
            fade_params=[(0.0, 0.0)],
            title_card_path=Path("/nonexistent/title.mp4"),
            intro_duration=3.0,
        )
        assert len(graph.inputs) == 1
        assert "concat=n=1:v=1:a=1" in graph.script

    def test_mixed_audio_and_silence(self):
        """Mix of keep_audio items: one gets atrim, other gets silence."""
        seg = _seg(
            [
                _video(duration=5.0, keep_audio=False),
                _video(
                    duration=5.0,
                    start=0.0,
                    end=5.0,
                    keep_audio=True,
                    source_file="/fake/clip2.mp4",
                ),
            ]
        )
        graph = build_segment_graph(seg, _CTX, fade_params=[(0.0, 0.0), (0.0, 0.0)])
        assert "atrim" in graph.script
        assert "aevalsrc=0" in graph.script

    def test_script_semicolon_separated(self):
        """Filter graph lines are joined with semicolons."""
        seg = _seg([_photo()])
        graph = build_segment_graph(seg, _CTX, fade_params=[(0.0, 0.0)])
        assert ";\n" in graph.script

    def test_output_labels_vout_aout(self):
        """Concat output labels are [vout][aout]."""
        seg = _seg([_photo()])
        graph = build_segment_graph(seg, _CTX, fade_params=[(0.0, 0.0)])
        assert "[vout][aout]" in graph.script

    def test_photo_input_uses_loop_filter(self):
        """Photo inputs use simple -i with loop filter in graph (no -loop 1)."""
        seg = _seg([_photo(duration=4.0)])
        graph = build_segment_graph(seg, _CTX, fade_params=[(0.0, 0.0)])
        inp = graph.inputs[0]
        assert "-i" in inp
        assert "-loop" not in inp  # loop filter, not input flag
        assert "loop=loop=119:size=1:start=0" in graph.script

    def test_video_input_no_ss_no_t(self):
        """Video inputs should NOT have -ss or -t (trim is in filter chain)."""
        seg = _seg([_video(duration=5.0, start=10.0, end=15.0)])
        graph = build_segment_graph(seg, _CTX, fade_params=[(0.0, 0.0)])
        inp = graph.inputs[0]
        assert "-ss" not in inp
        assert "-t" not in inp
        assert "-i" in inp
        assert "clip.mp4" in inp[-1]

    def test_video_trim_duration_from_start_end(self):
        """Video with start/end uses their difference as trim duration, not display_duration."""
        seg = _seg([_video(duration=5.0, start=10.0, end=18.0)])
        graph = build_segment_graph(seg, _CTX, fade_params=[(0.0, 0.0)])
        # Trim duration should be end - start = 8.0
        assert "trim=start=10.0:duration=8.0" in graph.script

    def test_heic_photo_no_conversion_needed(self):
        """HEIC photos work natively with loop filter (no convert_heic)."""
        item = _photo()
        item.source_file = "/fake/photo.heic"
        seg = _seg([item])
        graph = build_segment_graph(seg, _CTX, fade_params=[(0.0, 0.0)])
        assert "photo.heic" in str(graph.inputs[0])


# ---------------------------------------------------------------------------
# Integration: compute_fade_params + build_segment_graph together
# ---------------------------------------------------------------------------


class TestFadeParamsAndGraphIntegration:
    """Verify that compute_fade_params output is compatible with build_segment_graph."""

    @pytest.fixture(autouse=True)
    def _mock_probe(self):
        with patch.object(
            RenderContext,
            "probe_dimensions",
            return_value=(1920, 1080),
        ):
            yield

    def test_fade_params_shape_matches_segments(self):
        """compute_fade_params returns one list per segment, one tuple per item."""
        s1 = _seg([_photo(), _video(duration=5.0)], name="S1")
        s2 = _seg([_photo()], name="S2")
        edl = _edl([s1, s2])
        fades = compute_fade_params(edl)
        assert len(fades) == 2
        assert len(fades[0]) == 2
        assert len(fades[1]) == 1

    def test_fade_params_feed_into_graph(self):
        """End-to-end: compute fades -> build graph for each segment."""
        s1 = _seg([_photo(), _photo()], name="S1")
        s2 = _seg(
            [_video(duration=5.0, keep_audio=True, start=0.0, end=5.0)],
            name="S2",
            segment_transition_duration=1.0,
        )
        edl = _edl([s1, s2])
        fades = compute_fade_params(edl)

        # Build graph for each segment using its fade params
        for si, segment in enumerate(edl.segments):
            graph = build_segment_graph(segment, _CTX, fade_params=fades[si])
            assert isinstance(graph, SegmentGraph)
            assert len(graph.inputs) == len(segment.items)
