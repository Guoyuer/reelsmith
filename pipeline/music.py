"""Music generation for vlogs — dispatcher + local MusicGen backend.

Supports two backends:
  - "local": Meta's MusicGen (facebook/musicgen-medium) via HuggingFace transformers
  - "gemini": Google's Lyria RealTime API via WebSocket streaming

Falls back gracefully if model/API unavailable — vlog renders without music.
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


def generate_music(
    trip_type: str,
    style: str,
    target_duration: int,
    cache_dir: Path,
    mood: str = "",
    backend: str = "local",
    log_fn=None,
) -> Path | None:
    """Generate background music using the specified backend.

    backend: "local" (MusicGen) or "gemini" (Lyria RealTime API)
    """
    if backend == "gemini":
        from .music_gemini import fetch_music_gemini
        return fetch_music_gemini(
            trip_type=trip_type, style=style, target_duration=target_duration,
            cache_dir=cache_dir, mood=mood, log_fn=log_fn,
        )
    return fetch_music(
        trip_type=trip_type, style=style, target_duration=target_duration,
        cache_dir=cache_dir, mood=mood, log_fn=log_fn,
    )


def generate_music_for_edl(
    cfg,
    backend: str = "local",
    log_fn=None,
) -> Path | None:
    """Generate music for the latest EDL and update it with the track path.

    Called by the generate_music Dagster asset. Uses edl.estimated_duration()
    so music generation can run independently of video rendering.

    Returns the music file path, or None if skipped/failed.
    """
    _log = log_fn or print
    from .iterate import _load_latest_edl, _save_edl
    from .edl import MusicTrack

    edl, version = _load_latest_edl(cfg)

    if edl.music_mode != "auto":
        _log(f"Music mode is '{edl.music_mode}', skipping generation")
        return None

    if edl.music and Path(edl.music.file).exists():
        _log(f"Music file already exists: {edl.music.file}")
        return Path(edl.music.file)

    target_dur = int(edl.estimated_duration())
    ws = cfg.workspace
    music_cache = ws.parent.parent / "music" if ws.parent.name == "runs" else ws / "music"

    segment_moods = [s.music_mood for s in edl.segments if s.music_mood]
    combined_mood = (
        f"travel vlog background music: {'; then '.join(segment_moods)}"
        if segment_moods else ""
    )

    _log(f"Generating music: backend={backend}, duration={target_dur}s, "
         f"trip_type={edl.trip_type}, style={edl.style}")

    track_path = generate_music(
        trip_type=edl.trip_type, style=edl.style,
        target_duration=target_dur,
        cache_dir=music_cache, mood=combined_mood,
        backend=backend, log_fn=_log,
    )

    if track_path:
        edl.music = MusicTrack(file=str(track_path))
        _save_edl(cfg, edl, version)
        _log(f"Music track saved to EDL v{version}: {track_path}")

    return track_path
