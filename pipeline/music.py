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


def _segment_duration(seg) -> float:
    """Calculate a segment's screen time from its items and transitions."""
    total = sum(item.display_duration for item in seg.items)
    if seg.transition != "cut" and len(seg.items) > 1:
        total -= (len(seg.items) - 1) * seg.transition_duration
    return max(total, 5)  # at least 5s per segment


def _build_composite_music(
    segment_tracks: list[tuple[float, Path]],
    output_path: Path,
    crossfade: float = 2.0,
    log_fn=None,
) -> bool:
    """Build composite music from per-segment tracks with crossfades.

    segment_tracks: [(segment_duration, music_wav_path), ...]
    Returns True on success.
    """
    from .media_utils import run_subprocess

    _log = log_fn or print
    if not segment_tracks:
        return False

    if len(segment_tracks) == 1:
        # Single segment — just copy
        import shutil
        shutil.copy(str(segment_tracks[0][1]), str(output_path))
        return True

    # Trim each segment's music to its duration + crossfade overlap, then chain acrossfade
    trimmed: list[Path] = []
    inputs: list[str] = []
    for i, (dur, track) in enumerate(segment_tracks):
        trim_dur = dur + (crossfade if i < len(segment_tracks) - 1 else 0)
        trimmed_path = output_path.parent / f"_seg_music_{i}.wav"
        run_subprocess(
            ["ffmpeg", "-y", "-i", str(track), "-t", str(trim_dur),
             "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
             str(trimmed_path)],
            capture_output=True,
        )
        if trimmed_path.exists():
            trimmed.append(trimmed_path)
            inputs += ["-i", str(trimmed_path)]

    if len(trimmed) < 2:
        if trimmed:
            import shutil
            shutil.copy(str(trimmed[0]), str(output_path))
            for t in trimmed:
                t.unlink(missing_ok=True)
            return True
        return False

    # Chain acrossfade filters
    filter_parts = []
    for i in range(1, len(trimmed)):
        in_label = "[0:a]" if i == 1 else f"[a{i-1}]"
        out_label = f"[a{i}]" if i < len(trimmed) - 1 else "[out]"
        filter_parts.append(
            f"{in_label}[{i}:a]acrossfade=d={crossfade}:c1=tri:c2=tri{out_label}"
        )

    cmd = ["ffmpeg", "-y"] + inputs + [
        "-filter_complex", ";".join(filter_parts),
        "-map", "[out]",
        "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
        str(output_path),
    ]
    result = run_subprocess(cmd, capture_output=True, text=True)

    # Cleanup trimmed files
    for t in trimmed:
        t.unlink(missing_ok=True)

    if result.returncode != 0:
        _log(f"Composite music build failed: {result.stderr[-200:]}")
        return False

    _log(f"Composite music: {len(segment_tracks)} segments crossfaded into {output_path.name}")
    return True


def generate_music_for_edl(
    cfg,
    backend: str = "local",
    log_fn=None,
) -> Path | None:
    """Generate per-segment music and build a composite track with crossfades.

    Called by the generate_music Dagster asset. Generates one Lyria track per
    segment based on its music_mood, then crossfades them into one file.

    Returns the composite music file path, or None if skipped/failed.
    """
    _log = log_fn or print
    from .edl import load_latest_edl, save_edl
    from .edl import MusicTrack

    edl, version = load_latest_edl(cfg)

    if edl.music_mode != "auto":
        _log(f"Music mode is '{edl.music_mode}', skipping generation")
        return None

    if edl.music and Path(edl.music.file).exists():
        _log(f"Music file already exists: {edl.music.file}")
        return Path(edl.music.file)

    music_cache = cfg.music_dir

    # Generate per-segment music tracks
    _log(f"Generating per-segment music: {len(edl.segments)} segments, backend={backend}")
    segment_tracks: list[tuple[float, Path]] = []

    for i, seg in enumerate(edl.segments):
        seg_dur = int(_segment_duration(seg))
        mood = seg.music_mood or f"{edl.style} travel vlog background music"
        _log(f"  Segment {i+1}/{len(edl.segments)}: \"{seg.name}\" ({seg_dur}s)")
        _log(f"    Mood: {mood}")

        track = generate_music(
            trip_type=edl.trip_type, style=edl.style,
            target_duration=seg_dur,
            cache_dir=music_cache, mood=mood,
            backend=backend, log_fn=_log,
        )
        if track:
            seg.music_file = str(track)
            segment_tracks.append((seg_dur, track))
            _log(f"    Generated: {track.name}")
        else:
            _log(f"    FAILED — segment will be silent")

    if not segment_tracks:
        _log("No music generated for any segment")
        return None

    # Build composite with crossfades
    music_cache.mkdir(parents=True, exist_ok=True)
    composite_path = music_cache / f"composite_{edl.trip_type}_{edl.style}_{int(edl.estimated_duration())}s.wav"
    if not _build_composite_music(segment_tracks, composite_path, crossfade=2.0, log_fn=_log):
        # Fallback: use first segment's track
        _log("Composite build failed, using first segment track")
        composite_path = segment_tracks[0][1]

    edl.music = MusicTrack(file=str(composite_path))
    save_edl(cfg, edl, version)
    _log(f"Per-segment music saved to EDL v{version}: {composite_path}")

    return composite_path
