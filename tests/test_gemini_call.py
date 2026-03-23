"""Tests for pipeline.plan._gemini — Gemini API call helper (mocked)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _mock_genai_module(response_text="response text", input_tokens=100, output_tokens=50):
    """Create a mock google.genai module with Client that returns given response."""
    types = MagicMock()
    types.Part = MagicMock
    types.Content = MagicMock
    types.GenerateContentConfig = MagicMock
    types.Blob = MagicMock
    types.FileData = MagicMock
    types.MediaResolution.MEDIA_RESOLUTION_MEDIUM = "MEDIUM"

    response = MagicMock()
    response.text = response_text
    candidate = MagicMock()
    candidate.content.parts = []
    candidate.finish_reason = "STOP"
    candidate.safety_ratings = []
    response.candidates = [candidate]
    usage = MagicMock()
    usage.prompt_token_count = input_tokens
    usage.candidates_token_count = output_tokens
    response.usage_metadata = usage

    client = MagicMock()
    client.models.generate_content.return_value = response

    genai = MagicMock()
    genai.Client.return_value = client
    genai.types = types

    google_mock = MagicMock()
    google_mock.genai = genai

    return google_mock, genai, types, client


class TestGeminiCall:
    def test_returns_response_text(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        google_mock, genai, types, client = _mock_genai_module("hello world")

        with patch.dict("sys.modules", {"google": google_mock, "google.genai": genai, "google.genai.types": types}):
            # Force re-import so the mock is used
            import importlib
            import pipeline.plan._gemini as mod
            importlib.reload(mod)
            result = mod._gemini_call(system="sys", user_parts=["hi"], label="test", model="m")

        assert result == "hello world"
        client.models.generate_content.assert_called_once()

    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        from pipeline.plan._gemini import _gemini_call

        with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
            _gemini_call(system="test", user_parts=["hello"])

    def test_empty_response(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        google_mock, genai, types, client = _mock_genai_module("")

        with patch.dict("sys.modules", {"google": google_mock, "google.genai": genai, "google.genai.types": types}):
            import importlib
            import pipeline.plan._gemini as mod
            importlib.reload(mod)
            result = mod._gemini_call(system="s", user_parts=["h"], model="m")

        assert result == ""

    def test_handles_image_parts(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        google_mock, genai, types, client = _mock_genai_module("ok")

        with patch.dict("sys.modules", {"google": google_mock, "google.genai": genai, "google.genai.types": types}):
            import importlib
            import pipeline.plan._gemini as mod
            importlib.reload(mod)
            result = mod._gemini_call(
                system="s",
                user_parts=[
                    "text part",
                    {"type": "image_bytes", "mime_type": "image/jpeg", "data": b"\xff\xd8" * 10},
                ],
                model="m",
            )

        assert result == "ok"

    def test_handles_video_parts_with_upload(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        google_mock, genai, types, client = _mock_genai_module("ok")

        # Mock Files API upload
        uploaded = MagicMock()
        uploaded.state.name = "ACTIVE"
        uploaded.name = "files/abc123"
        uploaded.uri = "https://genai.google.com/files/abc123"
        client.files.upload.return_value = uploaded

        with patch.dict("sys.modules", {"google": google_mock, "google.genai": genai, "google.genai.types": types}):
            import importlib
            import pipeline.plan._gemini as mod
            importlib.reload(mod)
            result = mod._gemini_call(
                system="s",
                user_parts=[
                    {"type": "video_bytes", "mime_type": "video/mp4", "data": b"\x00" * 1000},
                ],
                model="m",
            )

        assert result == "ok"
        client.files.upload.assert_called_once()

    def test_uses_vlog_model_env_var(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("VLOG_MODEL", "custom-model")
        google_mock, genai, types, client = _mock_genai_module("ok")

        with patch.dict("sys.modules", {"google": google_mock, "google.genai": genai, "google.genai.types": types}):
            import importlib
            import pipeline.plan._gemini as mod
            importlib.reload(mod)
            mod._gemini_call(system="s", user_parts=["h"])

        call_args = client.models.generate_content.call_args
        assert call_args.kwargs.get("model") == "custom-model" or call_args[1].get("model") == "custom-model"
