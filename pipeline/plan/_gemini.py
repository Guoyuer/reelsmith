"""Gemini API call helper for the plan stage.

Handles multimodal content (text + inline images + Files API video upload),
logging, and token counting.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from pathlib import Path

logger = logging.getLogger("vlog.plan")


def _gemini_call(
    system: str,
    user_parts: list,
    label: str = "",
    model: str = "",
    thinking_level: str = "HIGH",
    progress_callback=None,
) -> str:
    """Make a Gemini API call with multimodal content. Returns response text.

    *user_parts*: list of strings and/or Part objects (text + images).
    *thinking_level*: OFF, LOW, or HIGH.
    """
    from google import genai
    from google.genai import types

    if not model:
        model = os.getenv("VLOG_MODEL", "gemini-3-flash-preview")

    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY not set. Add it to .env to use the visual/API planner. "
            "Get a key at https://ai.google.dev/gemini-api/docs/api-key"
        )
    client = genai.Client(api_key=api_key)

    # Validate model exists
    model_id = model if model.startswith("models/") else f"models/{model}"
    try:
        client.models.get(model=model_id)
    except Exception:
        available = sorted(
            (m.name or "").removeprefix("models/")
            for m in client.models.list()
            if "flash" in (m.name or "") or "pro" in (m.name or "")
        )
        raise RuntimeError(
            f"Model '{model}' not available. Options:\n  " + "\n  ".join(available)
        )

    # Validate thinking_level
    valid_thinking = ("OFF", "MINIMAL", "LOW", "MEDIUM", "HIGH")
    if thinking_level not in valid_thinking:
        raise ValueError(
            f"Invalid thinking_level '{thinking_level}'. Must be one of: {', '.join(valid_thinking)}"
        )

    # First pass: calculate total media size to decide inline vs Files API
    n_text = 0
    n_media = 0
    text_chars = 0
    media_bytes_total = 0
    for p in user_parts:
        if isinstance(p, str):
            n_text += 1
            text_chars += len(p)
        elif isinstance(p, dict) and p.get("type") in (
            "image_bytes",
            "audio_bytes",
            "video_bytes",
        ):
            n_media += 1
            media_bytes_total += len(p.get("data", b""))

    # Videos: single mega-preview uploaded via Files API (1 file, not 100+)
    # Images: inline (individual thumbnails, ~44MB base64 ~59MB, within 100MB limit)
    n_uploaded = 0
    parts = []
    for p in user_parts:
        if isinstance(p, str):
            parts.append(types.Part(text=p))
        elif isinstance(p, dict) and p.get("type") in (
            "image_bytes",
            "audio_bytes",
            "video_bytes",
        ):
            is_video = p.get("type") == "video_bytes"
            mime = p.get("mime_type", "image/jpeg")

            if is_video:
                # Upload video to Files API (typically 1 mega-preview)
                with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
                    tf.write(p["data"])
                    tf_path = tf.name
                try:
                    size_mb = len(p["data"]) / 1024 / 1024
                    logger.info(f"  Uploading video ({size_mb:.1f}MB) to Files API...")
                    if progress_callback:
                        progress_callback(0, 0, f"uploading video ({size_mb:.0f}MB)...")
                    uploaded = client.files.upload(file=tf_path)
                    while (uploaded.state.name if uploaded.state else None) != "ACTIVE":  # type: ignore[union-attr]
                        time.sleep(2)
                        uploaded = client.files.get(name=uploaded.name or "")
                    logger.info(f"  Video uploaded and ACTIVE: {uploaded.name}")
                    n_uploaded += 1
                finally:
                    Path(tf_path).unlink(missing_ok=True)
                parts.append(
                    types.Part(
                        file_data=types.FileData(file_uri=uploaded.uri, mime_type=mime),
                    )
                )
            else:
                parts.append(
                    types.Part(
                        inline_data=types.Blob(mime_type=mime, data=p["data"]),
                    )
                )
        elif isinstance(p, types.Part):
            parts.append(p)

    logger.info(f"=== [Gemini] API Call: {label} ===")
    logger.info(f"  Model: {model}")
    logger.info(f"  System prompt: {len(system)} chars")
    logger.info(
        f"  Input: {n_text} text parts ({text_chars} chars), "
        f"{n_media} media files ({media_bytes_total / 1024 / 1024:.1f}MB)"
    )
    if n_uploaded:
        logger.info(f"  Videos: {n_uploaded} uploaded via Files API")
    logger.info(f"  Images: {n_media - n_uploaded} inline")
    # Log system prompt (truncated for readability)
    for line in system.split("\n")[:5]:
        logger.info(f"  [system] {line}")
    logger.info(f"  [system] ... ({len(system.split(chr(10)))} lines total)")
    # Log text parts sent to Gemini
    for i, p in enumerate(user_parts):
        if isinstance(p, str):
            for line in p.split("\n"):
                logger.info(f"  [text #{i}] {line}")
        elif isinstance(p, dict):
            ptype = p.get("type", "?")
            size_kb = len(p.get("data", b"")) // 1024
            logger.info(
                f"  [media #{i}] {ptype} {p.get('mime_type', '?')} ({size_kb}KB)"
            )

    if progress_callback:
        progress_callback(0, 0, "request sent, waiting for gemini...")
    t0 = time.monotonic()

    config_kwargs: dict = {
        "system_instruction": system,
        "max_output_tokens": 32000,
        "temperature": 0.7,
        "media_resolution": types.MediaResolution.MEDIA_RESOLUTION_MEDIUM,
    }
    if thinking_level != "OFF":
        config_kwargs["thinking_config"] = types.ThinkingConfig(
            thinking_level=thinking_level,  # type: ignore[arg-type]
            include_thoughts=True,
        )
    config = types.GenerateContentConfig(**config_kwargs)  # type: ignore[arg-type]

    response = client.models.generate_content(
        model=model,
        contents=[types.Content(parts=parts)],
        config=config,
    )

    elapsed = time.monotonic() - t0
    if progress_callback:
        progress_callback(0, 0, f"response received ({elapsed:.0f}s)")

    # Log thinking, code execution, and other non-text parts
    if response.candidates:
        cand_content = response.candidates[0].content
        for part in (cand_content.parts if cand_content else None) or []:
            if getattr(part, "thought", False) and part.text:
                logger.info(f"  [Thinking] ({len(part.text)} chars)")
                for line in part.text.split("\n"):
                    logger.info(f"  | {line}")
            if getattr(part, "executable_code", None):
                ec = part.executable_code  # type: ignore[union-attr]
                code = ec.code or "" if ec else ""
                for line in code.split("\n"):
                    logger.info(f"  [Code] {line}")
            if getattr(part, "code_execution_result", None):
                cer = part.code_execution_result  # type: ignore[union-attr]
                if cer:
                    for line in (cer.output or "").split("\n"):
                        logger.info(f"  [CodeResult] {cer.outcome}: {line}")

    content = response.text or ""
    # Log finish reason if response is empty or blocked
    if not content and response.candidates:
        c = response.candidates[0]
        logger.warning(
            f"Empty response. finish_reason={c.finish_reason}, safety={c.safety_ratings}"
        )
    elif not content:
        logger.warning(
            f"Empty response with no candidates. prompt_feedback={response.prompt_feedback}"
        )
    usage = response.usage_metadata
    input_tokens = (usage.prompt_token_count or 0) if usage else 0
    output_tokens = (usage.candidates_token_count or 0) if usage else 0
    # Gemini 3.1 Flash Lite pricing: $0.075/M input, $0.30/M output
    cost_est = input_tokens * 0.075 / 1_000_000 + output_tokens * 0.30 / 1_000_000
    logger.info(
        f"  Response: {input_tokens:,} input tokens, "
        f"{output_tokens:,} output tokens, {elapsed:.1f}s"
    )
    logger.info(f"  Estimated cost: ${cost_est:.4f}")
    logger.info(f"  Output: {len(content)} chars")
    logger.info(f"=== [Gemini] End {label} ===")

    return content
