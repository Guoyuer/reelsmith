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

# Per-model pricing (USD per million tokens, paid tier)
_PRICING: dict[str, tuple[float, float]] = {
    "gemini-3.1-flash-lite-preview": (0.25, 1.50),
    "gemini-3-flash-preview": (0.50, 3.00),
    "gemini-3.1-pro-preview": (2.00, 12.00),
    "gemini-3-pro-preview": (2.00, 12.00),
}


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
                    logger.info("  [Thinking] %s", line)
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

    # --- Collect all response metadata into a structured summary ---
    def _short_enum(val) -> str:
        """Strip enum class prefix: 'HarmCategory.HARM_CATEGORY_X' → 'X'."""
        s = str(val)
        return s.rsplit(".", 1)[-1].removeprefix("HARM_CATEGORY_")

    usage = response.usage_metadata
    prompt_tokens = (usage.prompt_token_count or 0) if usage else 0
    content_tokens = (usage.candidates_token_count or 0) if usage else 0
    thinking_tokens = (usage.thoughts_token_count or 0) if usage else 0
    total_tokens = (usage.total_token_count or 0) if usage else 0
    cached_tokens = (usage.cached_content_token_count or 0) if usage else 0
    tool_use_tokens = (usage.tool_use_prompt_token_count or 0) if usage else 0

    in_rate, out_rate = _PRICING.get(model, (0.50, 3.00))
    cost_est = (
        prompt_tokens * in_rate / 1_000_000
        + (content_tokens + thinking_tokens) * out_rate / 1_000_000
    )

    cand = response.candidates[0] if response.candidates else None
    finish = cand.finish_reason if cand else "NO_CANDIDATES"

    # --- Log file output (structured text block) ---
    logger.info("--- Gemini API Response Summary ---")
    logger.info(
        "  Model: %s | Thinking: %s | Time: %.1fs", model, thinking_level, elapsed
    )
    logger.info("  Finish: %s | Response: %d chars", finish, len(content))
    if cand and cand.finish_message:
        logger.info("  Finish detail: %s", cand.finish_message)
    if finish != "STOP":
        logger.warning("  ⚠ Non-STOP finish — output may be truncated or filtered")
    if cand and cand.avg_logprobs is not None:
        logger.info("  Confidence: avg_logprobs=%.4f", cand.avg_logprobs)
    logger.info("  Tokens:")
    logger.info(
        "    Prompt:   %8s  ($%.3f)",
        f"{prompt_tokens:,}",
        prompt_tokens * in_rate / 1e6,
    )
    logger.info(
        "    Content:  %8s  ($%.3f)",
        f"{content_tokens:,}",
        content_tokens * out_rate / 1e6,
    )
    logger.info(
        "    Thinking: %8s  ($%.3f)",
        f"{thinking_tokens:,}",
        thinking_tokens * out_rate / 1e6,
    )
    logger.info("    Total:    %8s   $%.3f", f"{total_tokens:,}", cost_est)
    if cached_tokens:
        logger.info(
            "    Cached:   %8s  (prompt tokens from cache)", f"{cached_tokens:,}"
        )
    if tool_use_tokens:
        logger.info(
            "    Tool use: %8s  (prompt tokens for tools)", f"{tool_use_tokens:,}"
        )
    # Per-modality breakdown
    if usage:
        for breakdown_label, details in [
            ("Prompt", usage.prompt_tokens_details),
            ("Output", usage.candidates_tokens_details),
            ("Cache", usage.cache_tokens_details),
        ]:
            if details:
                parts = [
                    f"{_short_enum(d.modality)}={d.token_count:,}"
                    for d in details
                    if d.token_count
                ]
                if parts:
                    logger.info(
                        "    %s by modality: %s", breakdown_label, ", ".join(parts)
                    )
        if usage.traffic_type:
            logger.info("  Traffic type: %s", usage.traffic_type)
    if cand and cand.safety_ratings:
        parts = [
            f"{_short_enum(r.category)}={_short_enum(r.probability)}"
            for r in cand.safety_ratings
        ]
        logger.info("  Safety: %s", ", ".join(parts))
    logger.info("--- End Response Summary ---")

    # --- Rich terminal display ---
    try:
        import sys

        if sys.stderr.isatty():
            from rich.console import Console
            from rich.table import Table

            console = Console(stderr=True)

            # Token & cost table
            t = Table(
                title=f"\U0001f4ca Gemini API — {label}",
                border_style="dim",
                title_style="bold",
                caption=f"Model: {model} | Thinking: {thinking_level} | {elapsed:.0f}s",
                caption_style="dim",
            )
            t.add_column("", style="dim", min_width=10)
            t.add_column("Tokens", justify="right")
            t.add_column("$/M", justify="right", style="dim")
            t.add_column("Cost", justify="right")
            t.add_column("Modality", style="dim")

            # Prompt row with modality breakdown
            prompt_modality = ""
            if usage and usage.prompt_tokens_details:
                parts = [
                    f"{_short_enum(d.modality)}: {d.token_count:,}"
                    for d in usage.prompt_tokens_details
                    if d.token_count
                ]
                prompt_modality = " | ".join(parts)
            t.add_row(
                "Prompt",
                f"{prompt_tokens:,}",
                f"${in_rate}",
                f"${prompt_tokens * in_rate / 1e6:.3f}",
                prompt_modality,
            )
            if cached_tokens:
                t.add_row("  cached", f"[dim]{cached_tokens:,}", "", "", "")
            if tool_use_tokens:
                t.add_row("  tool use", f"[dim]{tool_use_tokens:,}", "", "", "")

            # Output rows
            output_modality = ""
            if usage and usage.candidates_tokens_details:
                parts = [
                    f"{_short_enum(d.modality)}: {d.token_count:,}"
                    for d in usage.candidates_tokens_details
                    if d.token_count
                ]
                output_modality = " | ".join(parts)
            t.add_row(
                "Content",
                f"{content_tokens:,}",
                f"${out_rate}",
                f"${content_tokens * out_rate / 1e6:.3f}",
                output_modality,
            )
            t.add_row(
                "Thinking",
                f"{thinking_tokens:,}",
                f"${out_rate}",
                f"${thinking_tokens * out_rate / 1e6:.3f}",
                "",
            )

            t.add_section()
            t.add_row(
                "[bold]Total",
                f"[bold]{total_tokens:,}",
                "",
                f"[bold]${cost_est:.3f}",
                "",
            )

            console.print(t)

            # Status line: finish + confidence + safety
            status_parts = []
            finish_style = "green" if finish == "STOP" else "red bold"
            status_parts.append(f"Finish: [{finish_style}]{finish}[/{finish_style}]")
            if cand and cand.avg_logprobs is not None:
                lp = cand.avg_logprobs
                conf_style = "green" if lp > -0.5 else "yellow" if lp > -1.0 else "red"
                status_parts.append(
                    f"Confidence: [{conf_style}]{lp:.4f}[/{conf_style}]"
                )
            status_parts.append(f"Output: {len(content):,} chars")
            if cand and cand.safety_ratings:
                blocked = [
                    r for r in cand.safety_ratings if getattr(r, "blocked", False)
                ]
                if blocked:
                    status_parts.append(
                        f"[red bold]BLOCKED: {len(blocked)} categories[/red bold]"
                    )
            console.print("  " + "  │  ".join(status_parts), highlight=False)

            if cand and cand.safety_ratings:
                safety_parts = [
                    f"{_short_enum(r.category)}=[dim]{_short_enum(r.probability)}[/dim]"
                    for r in cand.safety_ratings
                ]
                console.print(f"  Safety: {', '.join(safety_parts)}", highlight=False)
    except ImportError:
        pass

    # Report cost via callback metadata
    if progress_callback:
        thinking_suffix = f", {thinking_tokens:,} thinking" if thinking_tokens else ""
        progress_callback(
            0,
            0,
            f"~${cost_est:.2f} ({prompt_tokens:,} prompt, {content_tokens:,} content{thinking_suffix})",
        )

    return content
