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
from typing import Any

from ..config import ProgressCallback

logger = logging.getLogger("vlog.plan")


def _prepare_parts(
    user_parts: list, client, progress_callback: ProgressCallback = None
) -> tuple[list, int]:
    """Convert *user_parts* (str/dict) into Gemini API Part objects.

    Videos are uploaded via the Files API; images are inlined.
    Returns ``(parts, n_uploaded)`` where *n_uploaded* is the number of
    files uploaded via the Files API.
    """
    from google.genai import types

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
                    logger.info("  Uploading video (%.1fMB) to Files API...", size_mb)
                    if progress_callback:
                        progress_callback(0, 0, f"uploading video ({size_mb:.0f}MB)...")
                    uploaded = client.files.upload(file=tf_path)
                    while (uploaded.state.name if uploaded.state else None) != "ACTIVE":  # type: ignore[union-attr]
                        time.sleep(2)
                        uploaded = client.files.get(name=uploaded.name or "")
                    logger.info("  Video uploaded and ACTIVE: %s", uploaded.name)
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

    return parts, n_uploaded


def _parse_response(response) -> str:
    """Extract text content from a Gemini response.

    Logs thinking, executable_code, and code_execution_result parts.
    Renders thinking to terminal via Rich when available.
    Returns the text content string (may be empty).
    """
    # Log thinking, code execution, and other non-text parts
    if response.candidates:
        cand_content = response.candidates[0].content
        for part in (cand_content.parts if cand_content else None) or []:
            if getattr(part, "thought", False) and part.text:
                logger.info("  [Thinking] %d chars", len(part.text))
                for line in part.text.split("\n"):
                    logger.debug("  \U0001f4ad %s", line)
                # Rich Markdown to terminal
                try:
                    import sys

                    if sys.stderr.isatty():
                        from rich.console import Console
                        from rich.markdown import Markdown
                        from rich.panel import Panel

                        Console(stderr=True).print(
                            Panel(
                                Markdown(part.text),
                                title="\U0001f4ad Thinking",
                                border_style="dim",
                            )
                        )
                except ImportError:
                    pass
            if getattr(part, "executable_code", None):
                ec = part.executable_code  # type: ignore[union-attr]
                code = ec.code or "" if ec else ""
                for line in code.split("\n"):
                    logger.info("  [Code] %s", line)
            if getattr(part, "code_execution_result", None):
                cer = part.code_execution_result  # type: ignore[union-attr]
                if cer:
                    for line in (cer.output or "").split("\n"):
                        logger.info("  [CodeResult] %s: %s", cer.outcome, line)

    content = response.text or ""
    # Log finish reason if response is empty or blocked
    if not content and response.candidates:
        c = response.candidates[0]
        logger.warning(
            "Empty response. finish_reason=%s, safety=%s",
            c.finish_reason,
            c.safety_ratings,
        )
    elif not content:
        logger.warning(
            "Empty response with no candidates. prompt_feedback=%s",
            response.prompt_feedback,
        )
    return content


def _edl_response_schema() -> dict[str, Any]:
    """JSON schema for Gemini's EDL response (structured output).

    Uses preview_start/preview_end (MM:SS strings) which postprocessing
    converts to start_time/end_time (float seconds).
    """
    text_overlay_schema = {
        "type": "OBJECT",
        "properties": {
            "text": {"type": "STRING"},
            "position": {"type": "STRING", "enum": ["bottom", "center", "top"]},
            "font_size": {"type": "INTEGER"},
        },
        "required": ["text"],
    }
    item_schema = {
        "type": "OBJECT",
        "properties": {
            "source_file": {"type": "STRING"},
            "media_type": {"type": "STRING", "enum": ["photo", "video"]},
            "display_duration": {"type": "NUMBER"},
            "preview_start": {"type": "STRING", "nullable": True},
            "preview_end": {"type": "STRING", "nullable": True},
            "effect": {
                "type": "STRING",
                "enum": [
                    "ken_burns_in",
                    "ken_burns_out",
                    "ken_burns_left",
                    "ken_burns_right",
                    "static",
                    "none",
                ],
            },
            "playback_speed": {"type": "NUMBER"},
            "keep_audio": {"type": "BOOLEAN"},
            "text_overlay": {**text_overlay_schema, "nullable": True},
        },
        "required": ["source_file", "media_type", "display_duration"],
    }
    segment_schema = {
        "type": "OBJECT",
        "properties": {
            "name": {"type": "STRING"},
            "narrative_rationale": {"type": "STRING"},
            "music_mood": {"type": "STRING"},
            "mode": {"type": "STRING", "enum": ["narrative", "montage"]},
            "color_temp": {"type": "STRING", "enum": ["neutral", "warm", "cool"]},
            "segment_transition": {"type": "STRING", "enum": ["crossfade", "cut"]},
            "segment_transition_duration": {"type": "NUMBER"},
            "items": {"type": "ARRAY", "items": item_schema},
            "transition": {"type": "STRING", "enum": ["crossfade", "cut"]},
            "transition_duration": {"type": "NUMBER"},
        },
        "required": ["name", "items"],
    }
    return {
        "type": "OBJECT",
        "properties": {
            "title": {"type": "STRING"},
            "target_duration": {"type": "NUMBER"},
            "intro_duration": {"type": "NUMBER"},
            "outro_duration": {"type": "NUMBER"},
            "segments": {"type": "ARRAY", "items": segment_schema},
        },
        "required": ["title", "target_duration", "segments"],
    }


def _gemini_call(
    system: str,
    user_parts: list,
    label: str = "",
    model: str = "",
    thinking_level: str = "HIGH",
    progress_callback: ProgressCallback = None,
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

    # Convert user_parts into Gemini Part objects
    parts, n_uploaded = _prepare_parts(user_parts, client, progress_callback)

    # Log call details
    n_images = sum(
        1 for p in user_parts if isinstance(p, dict) and p.get("type") == "image_bytes"
    )
    n_videos_count = sum(
        1 for p in user_parts if isinstance(p, dict) and p.get("type") == "video_bytes"
    )
    img_mb = (
        sum(
            len(p.get("data", b""))
            for p in user_parts
            if isinstance(p, dict) and p.get("type") == "image_bytes"
        )
        / 1024
        / 1024
    )
    vid_mb = (
        sum(
            len(p.get("data", b""))
            for p in user_parts
            if isinstance(p, dict) and p.get("type") == "video_bytes"
        )
        / 1024
        / 1024
    )
    n_text_parts = sum(1 for p in user_parts if isinstance(p, str))
    text_chars = sum(len(p) for p in user_parts if isinstance(p, str))
    logger.info("Gemini API call: %s", label)
    logger.info("  Model: %s, thinking: %s", model, thinking_level)
    logger.info(
        "  %d text (%d chars), %d photos (%.0fMB), %d video (%.0fMB)",
        n_text_parts,
        text_chars,
        n_images,
        img_mb,
        n_videos_count,
        vid_mb,
    )
    # System prompt at DEBUG
    logger.debug("  --- SYSTEM PROMPT ---")
    for line in system.split("\n"):
        logger.debug("    | %s", line)
    logger.debug("  --- END SYSTEM PROMPT ---")
    # Full part details at DEBUG
    logger.debug("  --- USER PARTS ---")
    for i, p in enumerate(user_parts):
        if isinstance(p, str):
            for line in p.split("\n"):
                logger.debug("    | %s", line)
        elif isinstance(p, dict):
            logger.debug(
                "  [part %d] %s %s (%dKB)",
                i,
                p.get("type", "?"),
                p.get("mime_type", "?"),
                len(p.get("data", b"")) // 1024,
            )
    logger.debug("  --- END USER PARTS ---")

    if progress_callback:
        progress_callback(0, 0, "calling Gemini API...")
    t0 = time.monotonic()

    # Build config and call API
    config_kwargs: dict[str, Any] = {
        "system_instruction": system,
        "max_output_tokens": 65536,
        "temperature": 1.0,  # Gemini 3 default; lower values degrade reasoning
        "media_resolution": types.MediaResolution.MEDIA_RESOLUTION_LOW,
        "response_mime_type": "application/json",
        "response_schema": _edl_response_schema(),
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

    # Parse response content
    content = _parse_response(response)

    # Log token usage and cost
    usage = response.usage_metadata
    input_tokens = (usage.prompt_token_count or 0) if usage else 0
    output_tokens = (usage.candidates_token_count or 0) if usage else 0

    # Per-model pricing (USD per million tokens, paid tier)
    _PRICING = {
        "gemini-3.1-flash-lite-preview": (0.25, 1.50),
        "gemini-3-flash-preview": (0.50, 3.00),
        "gemini-3.1-pro-preview": (2.00, 12.00),
        "gemini-3-pro-preview": (2.00, 12.00),
    }
    in_rate, out_rate = _PRICING.get(model, (0.50, 3.00))
    cost_est = input_tokens * in_rate / 1_000_000 + output_tokens * out_rate / 1_000_000

    logger.info(
        "  Response: %s input tokens, %s output tokens, %.1fs, ~$%.2f",
        f"{input_tokens:,}",
        f"{output_tokens:,}",
        elapsed,
        cost_est,
    )
    logger.info("  Output: %d chars", len(content))
    logger.info("=== [Gemini] End %s ===", label)

    # Rich cost breakdown table to terminal
    try:
        import sys

        if sys.stderr.isatty():
            from rich.console import Console
            from rich.table import Table

            t = Table(
                title=f"Gemini API \u2014 {label}",
                border_style="dim",
                title_style="bold",
            )
            t.add_column("", style="dim")
            t.add_column("Tokens", justify="right")
            t.add_column("Rate", justify="right")
            t.add_column("Cost", justify="right")
            t.add_row(
                "Input",
                f"{input_tokens:,}",
                f"${in_rate}/M",
                f"${input_tokens * in_rate / 1e6:.3f}",
            )
            t.add_row(
                "Output",
                f"{output_tokens:,}",
                f"${out_rate}/M",
                f"${output_tokens * out_rate / 1e6:.3f}",
            )
            t.add_section()
            t.add_row(
                "[bold]Total",
                f"[bold]{input_tokens + output_tokens:,}",
                f"[dim]{elapsed:.0f}s",
                f"[bold]${cost_est:.3f}",
            )
            Console(stderr=True).print(t)
    except ImportError:
        pass

    # Report cost via callback metadata
    if progress_callback:
        progress_callback(
            0, 0, f"~${cost_est:.2f} ({input_tokens:,} in, {output_tokens:,} out)"
        )

    return content
