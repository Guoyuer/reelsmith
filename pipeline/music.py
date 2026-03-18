"""Auto-generate background music for vlogs using MusicGen (Meta).

Uses facebook/musicgen-medium (300M params) via HuggingFace transformers.
Generates instrumental background music from text prompts.
No API keys needed — runs fully locally.

Falls back gracefully if model unavailable — vlog renders without music.
"""

from __future__ import annotations

import json
from pathlib import Path

# Prompt templates per trip_type + style
MUSIC_PROMPTS: dict[str, dict[str, str]] = {
    "family": {
        "upbeat": "happy upbeat acoustic travel vlog music, ukulele and light percussion, warm and joyful",
        "cinematic": "warm cinematic orchestral travel music, gentle strings and piano, emotional",
        "reflective": "gentle reflective acoustic guitar, peaceful and nostalgic, warm memories",
        "energetic": "fun energetic pop travel music, claps and whistles, happy adventure",
    },
    "solo": {
        "upbeat": "upbeat indie travel vlog music, acoustic guitar and light drums, adventure",
        "cinematic": "cinematic solo journey music, sweeping strings and piano, discovery",
        "reflective": "calm ambient travel music, soft piano and pads, introspective journey",
        "energetic": "energetic electronic travel music, driving beat, exploration",
    },
    "food": {
        "upbeat": "jazzy upbeat cafe background music, light swing, food vibes",
        "cinematic": "elegant restaurant ambiance, soft jazz piano and brushed drums",
        "reflective": "lo-fi chill background music, warm and cozy, cafe atmosphere",
        "energetic": "fun quirky cooking show music, playful and bouncy",
    },
    "adventure": {
        "upbeat": "epic upbeat adventure music, driving drums and bold brass, excitement",
        "cinematic": "cinematic epic adventure soundtrack, dramatic orchestra, exploration",
        "reflective": "ambient nature documentary music, peaceful and vast, wilderness",
        "energetic": "high energy extreme sports music, fast drums and electric guitar",
    },
    "architecture": {
        "upbeat": "modern minimal electronic music, clean beats and synth pads, urban",
        "cinematic": "cinematic ambient music, slow build, grand spaces and design",
        "reflective": "ambient piano and strings, contemplative, architectural beauty",
        "energetic": "tech house electronic music, modern city vibes, precise",
    },
    "general": {
        "upbeat": "upbeat travel vlog background music, acoustic and light, carefree",
        "cinematic": "cinematic travel montage music, emotional strings and piano",
        "reflective": "calm reflective travel music, gentle acoustic guitar, peaceful",
        "energetic": "energetic pop travel music, fun and lively, adventure vibes",
    },
}


def _get_prompt(trip_type: str, style: str) -> str:
    type_prompts = MUSIC_PROMPTS.get(trip_type, MUSIC_PROMPTS["general"])
    return type_prompts.get(style, type_prompts.get("upbeat", "upbeat travel vlog background music"))


def fetch_music(
    trip_type: str,
    style: str,
    target_duration: int,
    cache_dir: Path,
    mood: str = "",
    log_fn=None,
) -> Path | None:
    """Generate background music via MusicGen (local).

    Returns path to generated wav, or None if unavailable.
    Caches tracks in cache_dir to avoid regenerating.
    """
    _log = log_fn or print

    # Check cache — keyed by trip_type, style, AND duration
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = f"{trip_type}_{style}_{target_duration}s"
    cache_meta = cache_dir / f"{cache_key}.json"
    if cache_meta.exists():
        meta = json.loads(cache_meta.read_text())
        cached_path = Path(meta.get("path", ""))
        if cached_path.exists():
            _log(f"Using cached music: {cached_path.name}")
            return cached_path

    # Use Gemini's music_mood if available, otherwise fall back to template
    prompt = mood if mood else _get_prompt(trip_type, style)
    gen_duration = target_duration
    model_name = "facebook/musicgen-medium"
    _log(f"=== Music Generation ===")
    _log(f"Model: {model_name}")
    _log(f"Prompt: '{prompt}'")
    _log(f"Target duration: {gen_duration}s")
    _log(f"Cache key: {cache_key}")

    try:
        import time
        import scipy.io.wavfile
        import torch
        from transformers import AutoProcessor, AutoConfig

        _log("Loading MusicGen model (this may download ~6GB on first run)...")
        t0 = time.time()
        processor = AutoProcessor.from_pretrained(model_name)

        # Load config first, then model with explicit config to avoid
        # transformers bug where config_class=MusicgenDecoderConfig
        config = AutoConfig.from_pretrained(model_name)
        from transformers import MusicgenForConditionalGeneration
        from transformers.models.musicgen.configuration_musicgen import MusicgenConfig
        MusicgenForConditionalGeneration.config_class = MusicgenConfig
        model = MusicgenForConditionalGeneration.from_pretrained(model_name, config=config)
        sr = model.config.audio_encoder.sampling_rate
        params_m = sum(p.numel() for p in model.parameters()) / 1e6
        _log(f"Model loaded in {time.time()-t0:.0f}s ({params_m:.0f}M params, sr={sr}Hz)")

        # ~50 tokens per second of audio, capped at model's max_position_embeddings
        max_positions = model.config.decoder.max_position_embeddings
        max_tokens = min(int(gen_duration * 50), max_positions - 10)
        actual_dur = max_tokens / 50
        if actual_dur < gen_duration:
            _log(f"Model max position limit: capping at {actual_dur:.0f}s "
                 f"(requested {gen_duration}s, max_positions={max_positions})")
        _log(f"Generating {gen_duration}s audio ({max_tokens} tokens)... "
             f"Estimated time: ~{gen_duration * 20 // 60}min")
        inputs = processor(text=[prompt], padding=True, return_tensors="pt")
        t0 = time.time()
        with torch.no_grad():
            audio = model.generate(**inputs, max_new_tokens=max_tokens)
        gen_time = time.time() - t0

        dur = audio.shape[-1] / sr
        _log(f"Generated {dur:.1f}s of audio in {gen_time:.0f}s "
             f"({gen_time/dur:.1f}x realtime)")

        out_path = cache_dir / f"{cache_key}.wav"
        _log(f"Saving to {out_path}...")
        audio_np = audio[0, 0].cpu().numpy()
        scipy.io.wavfile.write(str(out_path), sr, audio_np)

        cache_meta.write_text(json.dumps({
            "path": str(out_path),
            "prompt": prompt,
            "duration": round(dur, 1),
            "trip_type": trip_type,
            "style": style,
            "model": model_name,
            "gen_time_s": round(gen_time, 1),
        }))

        _log(f"Music saved: {out_path.name} ({out_path.stat().st_size // 1024}KB)")
        return out_path

    except Exception as e:
        import traceback
        _log(f"Music generation failed: {e}")
        _log(traceback.format_exc())
        return None
