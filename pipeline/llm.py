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
) -> str:
    """Send a chat request to Ollama and return the response text.

    Uses streaming so the request can be interrupted (KeyboardInterrupt)
    between token chunks — typically within 1-2 seconds.
    """
    model = model or cfg.planning_model

    messages = []
    if system:
        messages.append({"role": "system", "content": system})

    user_msg: dict = {"role": "user", "content": prompt}
    if images:
        user_msg["images"] = [
            base64.b64encode(img.read_bytes()).decode() for img in images
        ]
    messages.append(user_msg)

    with httpx.stream(
        "POST",
        f"{cfg.ollama_base}/api/chat",
        json={
            "model": model,
            "messages": messages,
            "stream": True,
            "options": {"temperature": temperature, "num_ctx": 32768},
        },
        timeout=600,
    ) as resp:
        resp.raise_for_status()
        chunks = []
        for line in resp.iter_lines():
            # Each line is a JSON object with a "message" field
            if not line:
                continue
            data = json.loads(line)
            content = data.get("message", {}).get("content", "")
            if content:
                chunks.append(content)
            if data.get("done"):
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
        images=images, temperature=0.1,
    )
    # Strip markdown code fences if present
    text = strip_markdown_fences(text)
    return json.loads(text)
