"""Shared Ollama LLM helpers for plan and iterate stages."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx

from .config import Config


def ollama_chat(
    cfg: Config,
    *,
    model: str | None = None,
    system: str | None = None,
    prompt: str,
    images: list[Path] | None = None,
    temperature: float = 0.3,
) -> str:
    """Send a chat request to Ollama and return the response text."""
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

    resp = httpx.post(
        f"{cfg.ollama_base}/api/chat",
        json={
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature, "num_ctx": 32768},
        },
        timeout=600,
    )
    resp.raise_for_status()
    return resp.json()["message"]["content"]


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
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(text)
