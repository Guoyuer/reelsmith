"""Tests for pipeline.plan._gemini — helper functions (no API calls)."""

from __future__ import annotations

from unittest.mock import MagicMock

from pipeline.plan._gemini import _edl_response_schema, _parse_response, _short_enum


# ---------------------------------------------------------------------------
# _short_enum
# ---------------------------------------------------------------------------


class TestShortEnum:
    def test_strips_prefix(self):
        assert _short_enum("HarmCategory.HARM_CATEGORY_DANGEROUS") == "DANGEROUS"

    def test_plain_string(self):
        assert _short_enum("STOP") == "STOP"

    def test_enum_with_dot(self):
        assert _short_enum("FinishReason.MAX_TOKENS") == "MAX_TOKENS"


# ---------------------------------------------------------------------------
# _edl_response_schema
# ---------------------------------------------------------------------------


class TestEdlResponseSchema:
    def test_returns_dict(self):
        schema = _edl_response_schema()
        assert isinstance(schema, dict)
        assert schema["type"] == "OBJECT"

    def test_has_required_fields(self):
        schema = _edl_response_schema()
        assert "title" in schema["properties"]
        assert "segments" in schema["properties"]
        assert "title" in schema["required"]
        assert "segments" in schema["required"]

    def test_segment_has_items(self):
        schema = _edl_response_schema()
        segment = schema["properties"]["segments"]["items"]
        assert "items" in segment["properties"]
        assert "name" in segment["properties"]

    def test_item_has_source_file(self):
        schema = _edl_response_schema()
        item = schema["properties"]["segments"]["items"]["properties"]["items"]["items"]
        assert "source_file" in item["properties"]
        assert "media_type" in item["properties"]
        assert "display_duration" in item["properties"]
        assert "source_file" in item["required"]

    def test_item_has_preview_timestamps(self):
        schema = _edl_response_schema()
        item = schema["properties"]["segments"]["items"]["properties"]["items"]["items"]
        assert "preview_start" in item["properties"]
        assert "preview_end" in item["properties"]

    def test_item_effect_enum(self):
        schema = _edl_response_schema()
        item = schema["properties"]["segments"]["items"]["properties"]["items"]["items"]
        effect = item["properties"]["effect"]
        assert "enum" in effect
        assert "ken_burns_in" in effect["enum"]
        assert "none" in effect["enum"]

    def test_text_overlay_schema(self):
        schema = _edl_response_schema()
        item = schema["properties"]["segments"]["items"]["properties"]["items"]["items"]
        overlay = item["properties"]["text_overlay"]
        assert "text" in overlay["properties"]
        assert "position" in overlay["properties"]


# ---------------------------------------------------------------------------
# _parse_response
# ---------------------------------------------------------------------------


class TestParseResponse:
    def test_extracts_text(self):
        response = MagicMock()
        response.text = '{"title": "test"}'
        response.candidates = [MagicMock()]
        response.candidates[0].content.parts = []
        result = _parse_response(response)
        assert result == '{"title": "test"}'

    def test_empty_response_with_candidates(self):
        response = MagicMock()
        response.text = ""
        candidate = MagicMock()
        candidate.content.parts = []
        candidate.finish_reason = "SAFETY"
        candidate.safety_ratings = []
        response.candidates = [candidate]
        result = _parse_response(response)
        assert result == ""

    def test_empty_response_no_candidates(self):
        response = MagicMock()
        response.text = ""
        response.candidates = []
        response.prompt_feedback = "blocked"
        result = _parse_response(response)
        assert result == ""

    def test_thinking_parts_logged(self):
        response = MagicMock()
        response.text = "output"
        thinking_part = MagicMock()
        thinking_part.thought = True
        thinking_part.text = "thinking about the problem..."
        candidate = MagicMock()
        candidate.content.parts = [thinking_part]
        response.candidates = [candidate]
        result = _parse_response(response)
        assert result == "output"
