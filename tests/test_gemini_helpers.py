"""Tests for pipeline.plan._gemini — helper functions (no API calls)."""

from __future__ import annotations

from unittest.mock import MagicMock

from pipeline.plan._gemini import _parse_response, _short_enum


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
