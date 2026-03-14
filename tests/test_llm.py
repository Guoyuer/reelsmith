"""Tests for pipeline.llm — Ollama LLM helpers."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pipeline.config import Config
from pipeline.llm import ollama_chat, ollama_json


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """Config with default Ollama settings."""
    with patch.dict(os.environ, {}, clear=True), \
         patch("pipeline.config.load_dotenv"):
        return Config.load(workspace=str(tmp_path / "workspace"))


def _mock_response(content: str) -> MagicMock:
    """Create a mock httpx response with the given content."""
    resp = MagicMock()
    resp.json.return_value = {"message": {"content": content}}
    resp.raise_for_status = MagicMock()
    return resp


# -----------------------------------------------------------------------
# ollama_chat tests
# -----------------------------------------------------------------------


class TestOllamaChat:
    def test_sends_correct_payload(self, cfg: Config):
        """ollama_chat posts correct model, messages, and options."""
        mock_resp = _mock_response("Hello!")

        with patch("pipeline.llm.httpx.post", return_value=mock_resp) as mock_post:
            result = ollama_chat(cfg, prompt="Say hi")

        assert result == "Hello!"
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args[0][0] == f"{cfg.ollama_base}/api/chat"
        payload = call_args[1]["json"]
        assert payload["model"] == cfg.planning_model
        assert payload["stream"] is False
        assert payload["options"]["temperature"] == 0.3
        assert payload["options"]["num_ctx"] == 32768
        assert len(payload["messages"]) == 1
        assert payload["messages"][0]["role"] == "user"
        assert payload["messages"][0]["content"] == "Say hi"

    def test_system_prompt_prepended(self, cfg: Config):
        """When system is provided, it appears as the first message."""
        mock_resp = _mock_response("OK")

        with patch("pipeline.llm.httpx.post", return_value=mock_resp) as mock_post:
            ollama_chat(cfg, system="You are helpful", prompt="Help me")

        payload = mock_post.call_args[1]["json"]
        assert len(payload["messages"]) == 2
        assert payload["messages"][0] == {"role": "system", "content": "You are helpful"}
        assert payload["messages"][1]["role"] == "user"
        assert payload["messages"][1]["content"] == "Help me"

    def test_images_base64_encoded(self, cfg: Config, tmp_path: Path):
        """Images should be base64-encoded and included in the user message."""
        img_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 50
        img_path = tmp_path / "test.png"
        img_path.write_bytes(img_data)

        mock_resp = _mock_response("I see an image")

        with patch("pipeline.llm.httpx.post", return_value=mock_resp) as mock_post:
            ollama_chat(cfg, prompt="Describe this", images=[img_path])

        payload = mock_post.call_args[1]["json"]
        user_msg = payload["messages"][0]
        assert "images" in user_msg
        assert len(user_msg["images"]) == 1
        # Verify base64 roundtrip
        decoded = base64.b64decode(user_msg["images"][0])
        assert decoded == img_data

    def test_custom_model(self, cfg: Config):
        """Explicit model parameter overrides the config default."""
        mock_resp = _mock_response("Hi")

        with patch("pipeline.llm.httpx.post", return_value=mock_resp) as mock_post:
            ollama_chat(cfg, model="llama3:70b", prompt="test")

        payload = mock_post.call_args[1]["json"]
        assert payload["model"] == "llama3:70b"


# -----------------------------------------------------------------------
# ollama_json tests
# -----------------------------------------------------------------------


class TestOllamaJson:
    def test_parses_plain_json(self, cfg: Config):
        """Plain JSON string is parsed correctly."""
        data = {"key": "value", "count": 42}
        mock_resp = _mock_response(json.dumps(data))

        with patch("pipeline.llm.httpx.post", return_value=mock_resp):
            result = ollama_json(cfg, prompt="Give JSON")

        assert result == data

    def test_strips_markdown_fences(self, cfg: Config):
        """Markdown code fences (```json ... ```) are stripped before parsing."""
        data = {"segments": [1, 2, 3]}
        fenced = f"```json\n{json.dumps(data)}\n```"
        mock_resp = _mock_response(fenced)

        with patch("pipeline.llm.httpx.post", return_value=mock_resp):
            result = ollama_json(cfg, prompt="Give JSON")

        assert result == data

    def test_raises_on_invalid_json(self, cfg: Config):
        """Invalid JSON should raise json.JSONDecodeError."""
        mock_resp = _mock_response("This is not JSON at all")

        with patch("pipeline.llm.httpx.post", return_value=mock_resp):
            with pytest.raises(json.JSONDecodeError):
                ollama_json(cfg, prompt="Give JSON")

    def test_uses_low_temperature(self, cfg: Config):
        """ollama_json should use temperature=0.1 for deterministic output."""
        mock_resp = _mock_response('{"ok": true}')

        with patch("pipeline.llm.httpx.post", return_value=mock_resp) as mock_post:
            ollama_json(cfg, prompt="Give JSON")

        payload = mock_post.call_args[1]["json"]
        assert payload["options"]["temperature"] == 0.1
