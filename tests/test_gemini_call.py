"""Tests for pipeline.plan._gemini — Gemini API call helper (mocked)."""

from __future__ import annotations

import importlib
import json
from unittest.mock import MagicMock, patch

import pytest


def _mock_genai_module(
    response_text="response text", input_tokens=100, output_tokens=50
):
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
    candidate.finish_message = None
    candidate.avg_logprobs = None
    candidate.token_count = output_tokens
    candidate.safety_ratings = []
    response.candidates = [candidate]
    usage = MagicMock()
    usage.prompt_token_count = input_tokens
    usage.candidates_token_count = output_tokens
    usage.thoughts_token_count = 0
    usage.total_token_count = input_tokens + output_tokens
    usage.cached_content_token_count = 0
    usage.tool_use_prompt_token_count = 0
    usage.prompt_tokens_details = None
    usage.candidates_tokens_details = None
    usage.cache_tokens_details = None
    usage.traffic_type = None
    response.usage_metadata = usage

    client = MagicMock()
    client.models.generate_content.return_value = response

    genai = MagicMock()
    genai.Client.return_value = client
    genai.types = types

    google_mock = MagicMock()
    google_mock.genai = genai

    return google_mock, genai, types, client


@pytest.fixture
def gemini_env(monkeypatch):
    """Fixture providing a mocked genai environment and reloaded module."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    def _setup(response_text="response text", **kwargs):
        google_mock, genai, types, client = _mock_genai_module(response_text, **kwargs)
        ctx = patch.dict(
            "sys.modules",
            {"google": google_mock, "google.genai": genai, "google.genai.types": types},
        )
        ctx.start()

        import pipeline.plan._gemini as mod

        importlib.reload(mod)
        return mod, client, types

    return _setup


@pytest.fixture(autouse=True)
def _cleanup_genai_module():
    """Ensure sys.modules patches are cleaned up after each test."""
    yield
    # Restore by reloading if the module was loaded
    try:
        import pipeline.plan._gemini as mod

        importlib.reload(mod)
    except Exception:
        pass


class TestGeminiCall:
    def test_returns_response_text(self, gemini_env):
        mod, client, _ = gemini_env("hello world")
        result = mod._gemini_call(
            system="sys", user_parts=["hi"], label="test", model="m"
        )
        assert result == "hello world"
        client.models.generate_content.assert_called_once()

    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        from pipeline.plan._gemini import _gemini_call

        with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
            _gemini_call(system="test", user_parts=["hello"])

    def test_empty_response(self, gemini_env):
        mod, _, _ = gemini_env("")
        assert mod._gemini_call(system="s", user_parts=["h"], model="m") == ""

    def test_handles_image_parts(self, gemini_env):
        mod, _, _ = gemini_env("ok")
        result = mod._gemini_call(
            system="s",
            user_parts=[
                "text part",
                {
                    "type": "image_bytes",
                    "mime_type": "image/jpeg",
                    "data": b"\xff\xd8" * 10,
                },
            ],
            model="m",
        )
        assert result == "ok"

    def test_handles_video_parts_with_upload(self, gemini_env):
        mod, client, _ = gemini_env("ok")

        uploaded = MagicMock()
        uploaded.state.name = "ACTIVE"
        uploaded.name = "files/abc123"
        uploaded.uri = "https://genai.google.com/files/abc123"
        client.files.upload.return_value = uploaded

        result = mod._gemini_call(
            system="s",
            user_parts=[
                {
                    "type": "video_bytes",
                    "mime_type": "video/mp4",
                    "data": b"\x00" * 1000,
                },
            ],
            model="m",
        )
        assert result == "ok"
        client.files.upload.assert_called_once()

    def test_config_includes_response_schema(self, gemini_env):
        """Structured output: response_mime_type and response_schema are passed."""
        mod, _, types = gemini_env("ok")

        config_kwargs_captured = {}
        original_config = types.GenerateContentConfig

        def capture_config(**kwargs):
            config_kwargs_captured.update(kwargs)
            return original_config(**kwargs)

        types.GenerateContentConfig = capture_config
        mod._gemini_call(system="s", user_parts=["h"], model="m")

        assert config_kwargs_captured.get("response_mime_type") == "application/json"
        assert "response_schema" in config_kwargs_captured
        schema = config_kwargs_captured["response_schema"]
        assert schema["type"] == "OBJECT"
        assert "segments" in schema["properties"]


class TestEdlResponseSchema:
    """Tests for _edl_response_schema — structured output schema validation."""

    @pytest.fixture
    def schema(self):
        from pipeline.plan._gemini import _edl_response_schema

        return _edl_response_schema()

    def test_top_level_structure(self, schema):
        assert schema["type"] == "OBJECT"
        assert "title" in schema["properties"]
        assert "target_duration" in schema["properties"]
        assert "segments" in schema["properties"]
        assert set(schema["required"]) == {"title", "target_duration", "segments"}

    def test_segment_has_required_fields(self, schema):
        seg_schema = schema["properties"]["segments"]["items"]
        assert seg_schema["type"] == "OBJECT"
        for field in ("name", "items", "music_mood", "mode", "color_temp"):
            assert field in seg_schema["properties"]
        assert set(seg_schema["required"]) == {"name", "items"}

    def test_item_uses_preview_timestamps(self, schema):
        """Schema uses preview_start/preview_end (not start_time/end_time)."""
        item_schema = schema["properties"]["segments"]["items"]["properties"]["items"][
            "items"
        ]
        assert "preview_start" in item_schema["properties"]
        assert "preview_end" in item_schema["properties"]
        assert item_schema["properties"]["preview_start"]["nullable"] is True
        assert "start_time" not in item_schema["properties"]
        assert "end_time" not in item_schema["properties"]

    def test_item_has_all_creative_fields(self, schema):
        item_schema = schema["properties"]["segments"]["items"]["properties"]["items"][
            "items"
        ]
        props = item_schema["properties"]
        for field in (
            "source_file",
            "media_type",
            "display_duration",
            "effect",
            "playback_speed",
            "keep_audio",
            "text_overlay",
        ):
            assert field in props

    def test_effect_enum_matches_code(self, schema):
        item_schema = schema["properties"]["segments"]["items"]["properties"]["items"][
            "items"
        ]
        effect_enum = item_schema["properties"]["effect"]["enum"]
        for effect in (
            "ken_burns_in",
            "ken_burns_out",
            "ken_burns_left",
            "ken_burns_right",
            "none",
        ):
            assert effect in effect_enum

    def test_transition_enum_simplified(self, schema):
        seg_schema = schema["properties"]["segments"]["items"]
        assert seg_schema["properties"]["transition"]["enum"] == ["crossfade", "cut"]

    def test_text_overlay_nullable_with_position_enum(self, schema):
        item_schema = schema["properties"]["segments"]["items"]["properties"]["items"][
            "items"
        ]
        assert item_schema["properties"]["text_overlay"]["nullable"] is True
        to_props = item_schema["properties"]["text_overlay"]["properties"]
        assert to_props["position"]["enum"] == ["bottom", "center", "top"]


class TestStructuredOutputParsing:
    """Test that schema-conforming JSON parses through postprocessing."""

    def test_structured_response_parses_to_edl(self):
        from pipeline.plan._postprocess import parse_and_convert_timestamps

        response = json.dumps(
            {
                "title": "Test Trip",
                "target_duration": 120,
                "intro_duration": 3.0,
                "outro_duration": 3.0,
                "segments": [
                    {
                        "name": "Chapter 1",
                        "narrative_rationale": "Opening",
                        "music_mood": "gentle guitar",
                        "mode": "narrative",
                        "color_temp": "warm",
                        "segment_transition": "crossfade",
                        "segment_transition_duration": 1.0,
                        "items": [
                            {
                                "source_file": "IMG_001.jpg",
                                "media_type": "photo",
                                "display_duration": 4.0,
                                "preview_start": None,
                                "preview_end": None,
                                "effect": "ken_burns_in",
                                "playback_speed": 1.0,
                                "keep_audio": False,
                                "text_overlay": None,
                            },
                            {
                                "source_file": "VID_001.mp4",
                                "media_type": "video",
                                "display_duration": 7.0,
                                "preview_start": "01:15",
                                "preview_end": "01:22",
                                "effect": "none",
                                "playback_speed": 1.0,
                                "keep_audio": True,
                                "text_overlay": {
                                    "text": "Hello",
                                    "position": "bottom",
                                    "font_size": 48,
                                },
                            },
                        ],
                        "transition": "crossfade",
                        "transition_duration": 0.4,
                    }
                ],
            }
        )

        offset_table = [(1, 30.0, 0.0)]
        edl = parse_and_convert_timestamps(response, offset_table)

        assert edl.title == "Test Trip"
        assert edl.target_duration == 120
        assert len(edl.segments) == 1
        assert len(edl.segments[0].items) == 2
        assert edl.segments[0].items[0].source_file == "IMG_001.jpg"
        assert edl.segments[0].items[0].start_time is None
        vid = edl.segments[0].items[1]
        assert vid.source_file == "VID_001.mp4"
        assert vid.keep_audio is True
        assert vid.text_overlay is not None
        assert vid.text_overlay.text == "Hello"

    def test_structured_response_with_montage(self):
        from pipeline.plan._postprocess import parse_and_convert_timestamps

        response = json.dumps(
            {
                "title": "Montage Test",
                "target_duration": 60,
                "segments": [
                    {
                        "name": "Quick Cuts",
                        "mode": "montage",
                        "color_temp": "warm",
                        "items": [
                            {
                                "source_file": "IMG_A.jpg",
                                "media_type": "photo",
                                "display_duration": 2.5,
                            },
                            {
                                "source_file": "IMG_B.jpg",
                                "media_type": "photo",
                                "display_duration": 2.0,
                            },
                        ],
                        "transition": "cut",
                        "transition_duration": 0.0,
                    }
                ],
            }
        )

        edl = parse_and_convert_timestamps(response, [])
        assert edl.segments[0].mode == "montage"
        assert edl.segments[0].transition == "cut"
        assert edl.segments[0].items[0].display_duration == 2.5
