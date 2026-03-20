"""Tests for pipeline.plan — Gemini visual planner helpers."""

from __future__ import annotations

from pipeline.plan import _default_focus


# ---------------------------------------------------------------------------
# Default focus tests
# ---------------------------------------------------------------------------

class TestDefaultFocus:
    def test_family_default(self):
        assert "family" in _default_focus("family").lower() or "happiness" in _default_focus("family").lower()

    def test_solo_default(self):
        assert "journey" in _default_focus("solo").lower() or "discovery" in _default_focus("solo").lower()

    def test_unknown_falls_back(self):
        assert _default_focus("nonexistent") == _default_focus("general")
