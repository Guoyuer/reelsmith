"""Tests for pipeline.assemble._filters — escape, color grade, ken burns, drawtext."""

from __future__ import annotations

from pipeline.assemble._filters import (
    color_grade,
    escape_drawtext,
    hdr_to_sdr_filter,
    is_hdr_transfer,
    ken_burns_filter,
)


class TestHdrToSdr:
    def test_pq_is_hdr(self):
        assert is_hdr_transfer("smpte2084")

    def test_hlg_is_hdr(self):
        assert is_hdr_transfer("arib-std-b67")

    def test_sdr_not_hdr(self):
        assert not is_hdr_transfer("bt709")
        assert not is_hdr_transfer("")

    def test_pq_filter_builds_tonemap(self):
        vf = hdr_to_sdr_filter("smpte2084")
        assert "zscale" in vf and "tonemap" in vf
        assert "bt709" in vf

    def test_hlg_filter_builds_tonemap(self):
        # HLG (DJI) must tone-map too — one chain handles both via zscale.
        assert "tonemap" in hdr_to_sdr_filter("arib-std-b67")

    def test_sdr_returns_empty(self):
        # SDR clips must pass through untouched (no tone-map).
        assert hdr_to_sdr_filter("bt709") == ""
        assert hdr_to_sdr_filter("") == ""


class TestEscapeDrawtext:
    def test_colon(self):
        assert "\\:" in escape_drawtext("a: b")

    def test_brackets(self):
        result = escape_drawtext("[hello]")
        assert "\\[" in result
        assert "\\]" in result

    def test_equals(self):
        assert "\\=" in escape_drawtext("a=b")

    def test_backslash(self):
        assert "\\\\" in escape_drawtext("a\\b")

    def test_apostrophe_replaced(self):
        result = escape_drawtext("it's")
        assert "'" not in result
        assert "\u2019" in result

    def test_plain_text_unchanged(self):
        assert escape_drawtext("hello world") == "hello world"


class TestColorGrade:
    def test_neutral(self):
        result = color_grade("neutral")
        assert "eq=contrast=1.02" in result
        assert "colorbalance" not in result

    def test_warm(self):
        result = color_grade("warm")
        assert "colorbalance=rs=0.02" in result
        assert "bs=-0.02" in result

    def test_cool(self):
        result = color_grade("cool")
        assert "colorbalance=rs=-0.02" in result
        assert "bs=0.02" in result

    def test_unknown_defaults_to_neutral(self):
        result = color_grade("sepia")
        assert "colorbalance" not in result


class TestKenBurnsFilter:
    def test_zoom_in(self):
        result = ken_burns_filter(120, 1920, 1080, 30, direction="in")
        assert "crop=" in result
        assert "scale=1920:1080:flags=lanczos" in result
        assert "fps=30" in result

    def test_zoom_out(self):
        result = ken_burns_filter(120, 1920, 1080, 30, direction="out")
        assert "1.3-0.3" in result  # out starts zoomed

    def test_pan_left(self):
        result = ken_burns_filter(120, 1920, 1080, 30, direction="left")
        assert "1.15" in result  # constant zoom for pan

    def test_pan_right(self):
        result = ken_burns_filter(120, 1920, 1080, 30, direction="right")
        assert "1.15" in result

    def test_static(self):
        result = ken_burns_filter(120, 1920, 1080, 30, direction="static")
        # Static zoom = 1 (no movement)
        assert "trunc(iw/(1)/2)*2" in result

    def test_unknown_direction_defaults_to_in(self):
        result = ken_burns_filter(120, 1920, 1080, 30, direction="diagonal")
        # Should fall back to "in" zoom
        assert "1+0.3" in result
