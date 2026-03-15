"""Shared Ollama LLM helpers for plan and iterate stages."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx

from .config import Config
from .media_utils import strip_markdown_fences


def ollama_chat(
    cfg: Config,
    *,
    model: str | None = None,
    system: str | None = None,
    prompt: str,
    images: list[Path] | None = None,
    temperature: float = 0.3,
    json_mode: bool = False,
    log_fn=None,
) -> str:
    """Send a chat request to Ollama and return the response text.

    Uses streaming so the request can be interrupted (KeyboardInterrupt)
    between token chunks — typically within 1-2 seconds.

    If *log_fn* is provided, progress is logged every ~50 tokens.
    """
    model = model or cfg.planning_model
    _log = log_fn

    messages = []
    if system:
        messages.append({"role": "system", "content": system})

    user_msg: dict = {"role": "user", "content": prompt}
    if images:
        user_msg["images"] = [
            base64.b64encode(img.read_bytes()).decode() for img in images
        ]
    messages.append(user_msg)

    payload: dict = {
        "model": model,
        "messages": messages,
        "stream": True,
        "options": {"temperature": temperature, "num_ctx": 32768},
    }
    if json_mode:
        payload["format"] = "json"

    with httpx.stream(
        "POST",
        f"{cfg.ollama_base}/api/chat",
        json=payload,
        timeout=600,
    ) as resp:
        resp.raise_for_status()
        chunks = []
        token_count = 0
        for line in resp.iter_lines():
            if not line:
                continue
            data = json.loads(line)
            content = data.get("message", {}).get("content", "")
            if content:
                chunks.append(content)
                token_count += 1
                if _log and token_count % 50 == 0:
                    _log(f"Generating... ({token_count} tokens)")
            if data.get("done"):
                if _log:
                    _log(f"Generation complete ({token_count} tokens)")
                break
        return "".join(chunks)


def ollama_json(
    cfg: Config,
    *,
    model: str | None = None,
    system: str | None = None,
    prompt: str,
    images: list[Path] | None = None,
) -> dict:
    """Call Ollama and parse the response as JSON."""
    text = ollama_chat(
        cfg, model=model, system=system, prompt=prompt,
        images=images, temperature=0.1, json_mode=True,
    )
    # Strip markdown code fences if present
    text = strip_markdown_fences(text)
    return json.loads(text)
