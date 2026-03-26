"""Tests for pipeline.plan._gemini — Gemini API call helper (mocked)."""

from __future__ import annotations

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


class TestGeminiCall:
    def test_returns_response_text(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        google_mock, genai, types, client = _mock_genai_module("hello world")

        with patch.dict(
            "sys.modules",
            {"google": google_mock, "google.genai": genai, "google.genai.types": types},
        ):
            # Force re-import so the mock is used
            import importlib

            import pipeline.plan._gemini as mod

            importlib.reload(mod)
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

    def test_empty_response(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        google_mock, genai, types, client = _mock_genai_module("")

        with patch.dict(
            "sys.modules",
            {"google": google_mock, "google.genai": genai, "google.genai.types": types},
        ):
            import importlib

            import pipeline.plan._gemini as mod

            importlib.reload(mod)
            result = mod._gemini_call(system="s", user_parts=["h"], model="m")

        assert result == ""

    def test_handles_image_parts(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        google_mock, genai, types, client = _mock_genai_module("ok")

        with patch.dict(
            "sys.modules",
            {"google": google_mock, "google.genai": genai, "google.genai.types": types},
        ):
            import importlib

            import pipeline.plan._gemini as mod

            importlib.reload(mod)
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

    def test_handles_video_parts_with_upload(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        google_mock, genai, types, client = _mock_genai_module("ok")

        # Mock Files API upload
        uploaded = MagicMock()
        uploaded.state.name = "ACTIVE"
        uploaded.name = "files/abc123"
        uploaded.uri = "https://genai.google.com/files/abc123"
        client.files.upload.return_value = uploaded

        with patch.dict(
            "sys.modules",
            {"google": google_mock, "google.genai": genai, "google.genai.types": types},
        ):
            import importlib

            import pipeline.plan._gemini as mod

            importlib.reload(mod)
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

    def test_config_includes_response_schema(self, monkeypatch):
        """Structured output: response_mime_type and response_schema are passed."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        google_mock, genai, types, client = _mock_genai_module("ok")

        # Track kwargs passed to GenerateContentConfig
        config_kwargs_captured = {}
        original_config = types.GenerateContentConfig

        def capture_config(**kwargs):
            config_kwargs_captured.update(kwargs)
            return original_config(**kwargs)

        types.GenerateContentConfig = capture_config

        with patch.dict(
            "sys.modules",
            {"google": google_mock, "google.genai": genai, "google.genai.types": types},
        ):
            import importlib

            import pipeline.plan._gemini as mod

            importlib.reload(mod)
            mod._gemini_call(system="s", user_parts=["h"], model="m")

        assert config_kwargs_captured.get("response_mime_type") == "application/json"
        assert "response_schema" in config_kwargs_captured
        schema = config_kwargs_captured["response_schema"]
        assert schema["type"] == "OBJECT"
        assert "segments" in schema["properties"]

    def test_uses_vlog_model_env_var(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("VLOG_MODEL", "custom-model")
        google_mock, genai, types, client = _mock_genai_module("ok")

        with patch.dict(
            "sys.modules",
            {"google": google_mock, "google.genai": genai, "google.genai.types": types},
        ):
            import importlib

            import pipeline.plan._gemini as mod

            importlib.reload(mod)
            mod._gemini_call(system="s", user_parts=["h"])

        call_args = client.models.generate_content.call_args
        assert (
            call_args.kwargs.get("model") == "custom-model"
            or call_args[1].get("model") == "custom-model"
        )


class TestEdlResponseSchema:
    """Tests for _edl_response_schema — structured output schema validation."""

    def test_schema_is_valid_structure(self):
        from pipeline.plan._gemini import _edl_response_schema

        schema = _edl_response_schema()
        assert schema["type"] == "OBJECT"
        assert "title" in schema["properties"]
        assert "target_duration" in schema["properties"]
        assert "segments" in schema["properties"]
        assert set(schema["required"]) == {"title", "target_duration", "segments"}

    def test_segment_schema_has_required_fields(self):
        from pipeline.plan._gemini import _edl_response_schema

        schema = _edl_response_schema()
        seg_schema = schema["properties"]["segments"]["items"]
        assert seg_schema["type"] == "OBJECT"
        assert "name" in seg_schema["properties"]
        assert "items" in seg_schema["properties"]
        assert "music_mood" in seg_schema["properties"]
        assert "mode" in seg_schema["properties"]
        assert "color_temp" in seg_schema["properties"]
        assert set(seg_schema["required"]) == {"name", "items"}

    def test_item_schema_has_preview_timestamps(self):
        """Schema uses preview_start/preview_end (not start_time/end_time)."""
        from pipeline.plan._gemini import _edl_response_schema

        schema = _edl_response_schema()
        item_schema = schema["properties"]["segments"]["items"]["properties"]["items"][
            "items"
        ]
        assert "preview_start" in item_schema["properties"]
        assert "preview_end" in item_schema["properties"]
        assert item_schema["properties"]["preview_start"]["nullable"] is True
        # Should NOT have start_time/end_time — those are postprocessing fields
        assert "start_time" not in item_schema["properties"]
        assert "end_time" not in item_schema["properties"]

    def test_item_schema_has_all_creative_fields(self):
        from pipeline.plan._gemini import _edl_response_schema

        schema = _edl_response_schema()
        item_schema = schema["properties"]["segments"]["items"]["properties"]["items"][
            "items"
        ]
        props = item_schema["properties"]
        assert "source_file" in props
        assert "media_type" in props
        assert "display_duration" in props
        assert "effect" in props
        assert "playback_speed" in props
        assert "keep_audio" in props
        assert "text_overlay" in props

    def test_effect_enum_matches_code(self):
        from pipeline.plan._gemini import _edl_response_schema

        schema = _edl_response_schema()
        item_schema = schema["properties"]["segments"]["items"]["properties"]["items"][
            "items"
        ]
        effect_enum = item_schema["properties"]["effect"]["enum"]
        assert "ken_burns_in" in effect_enum
        assert "ken_burns_out" in effect_enum
        assert "ken_burns_left" in effect_enum
        assert "ken_burns_right" in effect_enum
        assert "static" in effect_enum
        assert "none" in effect_enum

    def test_transition_enum_simplified(self):
        """Only crossfade and cut — matches prompt simplification."""
        from pipeline.plan._gemini import _edl_response_schema

        schema = _edl_response_schema()
        seg_schema = schema["properties"]["segments"]["items"]
        transition_enum = seg_schema["properties"]["transition"]["enum"]
        assert transition_enum == ["crossfade", "cut"]

    def test_text_overlay_nullable(self):
        from pipeline.plan._gemini import _edl_response_schema

        schema = _edl_response_schema()
        item_schema = schema["properties"]["segments"]["items"]["properties"]["items"][
            "items"
        ]
        assert item_schema["properties"]["text_overlay"]["nullable"] is True

    def test_text_overlay_position_enum(self):
        from pipeline.plan._gemini import _edl_response_schema

        schema = _edl_response_schema()
        item_schema = schema["properties"]["segments"]["items"]["properties"]["items"][
            "items"
        ]
        to_props = item_schema["properties"]["text_overlay"]["properties"]
        assert to_props["position"]["enum"] == ["bottom", "center", "top"]


class TestStructuredOutputParsing:
    """Test that schema-conforming JSON parses through postprocessing."""

    def test_structured_response_parses_to_edl(self):
        """A valid structured response can be parsed and converted."""
        from pipeline.plan._postprocess import parse_and_convert_timestamps

        # Simulate a clean JSON response (no markdown fences — structured output)
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

        # Offset table: item #1 at offset 0s, 30s duration
        offset_table = [(1, 30.0, 0.0)]

        edl = parse_and_convert_timestamps(response, offset_table)

        assert edl.title == "Test Trip"
        assert edl.target_duration == 120
        assert len(edl.segments) == 1
        assert len(edl.segments[0].items) == 2
        # Photo: no timestamp conversion
        assert edl.segments[0].items[0].source_file == "IMG_001.jpg"
        assert edl.segments[0].items[0].start_time is None
        # Video: preview timestamps converted
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
