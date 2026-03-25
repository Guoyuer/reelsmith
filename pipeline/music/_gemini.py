"""Generate background music via Google's Lyria RealTime API (Gemini).

Uses the Lyria RealTime experimental model via WebSocket streaming.
Generates instrumental background music from text prompts.
Requires GEMINI_API_KEY in .env.

Falls back gracefully if API unavailable — vlog renders without music.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import time
from pathlib import Path

logger = logging.getLogger("vlog.music.gemini")


def generate_music_gemini(
    trip_type: str,
    style: str,
    target_duration: int,
    cache_dir: Path,
    mood: str = "",
) -> Path | None:
    """Generate background music via Lyria RealTime API.

    Returns path to generated wav, or None if unavailable.
    Caches tracks in cache_dir to avoid regenerating.
    """
    # Check cache — include mood hash so different moods don't collide
    import hashlib

    cache_dir.mkdir(parents=True, exist_ok=True)
    mood_hash = hashlib.md5(mood.encode()).hexdigest()[:8] if mood else "default"
    cache_key = f"gemini_{trip_type}_{style}_{target_duration}s_{mood_hash}"
    cache_meta = cache_dir / f"{cache_key}.json"
    if cache_meta.exists():
        meta = json.loads(cache_meta.read_text())
        cached_path = Path(meta.get("path", ""))
        if cached_path.exists():
            logger.info("Using cached music: %s", cached_path.name)
            return cached_path

    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        logger.warning("GEMINI_API_KEY not set — cannot use Gemini music backend")
        return None

    # Use mood if provided, otherwise fall back to template
    from ._prompts import get_prompt

    prompt = mood if mood else get_prompt(trip_type, style)

    logger.info("=== Music Generation (Gemini Lyria RealTime) ===")
    logger.info("Model: lyria-realtime-exp")
    logger.info("Prompt: '%s'", prompt)
    logger.info("Target duration: %ds", target_duration)
    logger.info("Cache key: %s", cache_key)

    try:
        out_path = cache_dir / f"{cache_key}.wav"
        t0 = time.time()

        pcm_data = asyncio.run(_generate_music(api_key, prompt, target_duration))

        if not pcm_data:
            logger.warning("No audio data received from Lyria RealTime")
            return None

        gen_time = time.time() - t0

        # Write WAV file (48kHz, 16-bit, stereo)
        sample_rate = 48000
        channels = 2
        bits_per_sample = 16
        _write_wav(out_path, pcm_data, sample_rate, channels, bits_per_sample)

        bytes_per_second = sample_rate * channels * (bits_per_sample // 8)
        dur = len(pcm_data) / bytes_per_second
        logger.info(
            "Generated %.1fs of audio in %.1fs via Lyria RealTime", dur, gen_time
        )

        cache_meta.write_text(
            json.dumps(
                {
                    "path": str(out_path),
                    "prompt": prompt,
                    "duration": round(dur, 1),
                    "trip_type": trip_type,
                    "style": style,
                    "model": "lyria-realtime-exp",
                    "gen_time_s": round(gen_time, 1),
                    "backend": "gemini",
                }
            )
        )

        logger.info(
            "Music saved: %s (%dKB)", out_path.name, out_path.stat().st_size // 1024
        )
        return out_path

    except Exception:
        logger.error("Gemini music generation failed", exc_info=True)
        return None


async def _generate_music(
    api_key: str,
    prompt: str,
    duration: int,
) -> bytes:
    """Stream music from Lyria RealTime and collect PCM chunks."""
    from google import genai
    from google.genai import types

    client = genai.Client(
        api_key=api_key,
        http_options={"api_version": "v1alpha"},
    )

    # 48kHz stereo 16-bit = 192,000 bytes per second
    bytes_per_second = 48000 * 2 * 2
    target_bytes = duration * bytes_per_second

    audio_chunks: list[bytes] = []
    total_bytes = 0

    logger.info("Streaming %ds of music from Lyria RealTime...", duration)

    async with client.aio.live.music.connect(
        model="models/lyria-realtime-exp",
    ) as session:
        await session.set_weighted_prompts(
            prompts=[types.WeightedPrompt(text=prompt, weight=1.0)]
        )
        await session.set_music_generation_config(
            config=types.LiveMusicGenerationConfig(
                guidance=4.0,
                temperature=1.1,
            )
        )
        await session.play()

        async for message in session.receive():
            if not hasattr(message, "server_content") or not message.server_content:
                continue
            if not hasattr(message.server_content, "audio_chunks"):
                continue
            for chunk in message.server_content.audio_chunks:
                audio_chunks.append(chunk.data)
                total_bytes += len(chunk.data)

            if total_bytes >= target_bytes:
                break

    logger.info("Received %.0fKB of audio data", total_bytes / 1024)
    return b"".join(audio_chunks)


def _write_wav(
    path: Path,
    pcm_data: bytes,
    sample_rate: int,
    channels: int,
    bits_per_sample: int,
) -> None:
    """Write raw PCM data as a WAV file."""
    byte_rate = sample_rate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8
    data_size = len(pcm_data)

    with open(path, "wb") as f:
        # RIFF header
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + data_size))
        f.write(b"WAVE")
        # fmt chunk
        f.write(b"fmt ")
        f.write(struct.pack("<I", 16))  # chunk size
        f.write(struct.pack("<H", 1))  # PCM format
        f.write(struct.pack("<H", channels))
        f.write(struct.pack("<I", sample_rate))
        f.write(struct.pack("<I", byte_rate))
        f.write(struct.pack("<H", block_align))
        f.write(struct.pack("<H", bits_per_sample))
        # data chunk
        f.write(b"data")
        f.write(struct.pack("<I", data_size))
        f.write(pcm_data)
