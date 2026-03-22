"""Music generation for vlogs — dispatcher + local MusicGen backend.

Supports two backends:
  - "local": Meta's MusicGen (facebook/musicgen-medium) via HuggingFace transformers
  - "gemini": Google's Lyria RealTime API via WebSocket streaming

Falls back gracefully if model/API unavailable — vlog renders without music.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("vlog.music")

from .music_prompts import MUSIC_PROMPTS, get_prompt as _get_prompt


def fetch_music(
    trip_type: str,
    style: str,
    target_duration: int,
    cache_dir: Path,
    mood: str = "",
) -> Path | None:
    """Generate background music via MusicGen (local).

    Returns path to generated wav, or None if unavailable.
    Caches tracks in cache_dir to avoid regenerating.
    """

    # Check cache — keyed by trip_type, style, AND duration
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = f"{trip_type}_{style}_{target_duration}s"
    cache_meta = cache_dir / f"{cache_key}.json"
    if cache_meta.exists():
        meta = json.loads(cache_meta.read_text())
        cached_path = Path(meta.get("path", ""))
        if cached_path.exists():
            logger.info("Using cached music: %s", cached_path.name)
            return cached_path

    # Use Gemini's music_mood if available, otherwise fall back to template
    prompt = mood if mood else _get_prompt(trip_type, style)
    gen_duration = target_duration
    model_name = "facebook/musicgen-medium"
    logger.info("=== Music Generation ===")
    logger.info("Model: %s", model_name)
    logger.info("Prompt: '%s'", prompt)
    logger.info("Target duration: %ds", gen_duration)
    logger.info("Cache key: %s", cache_key)

    try:
        import time
        import scipy.io.wavfile
        import torch
        from transformers import AutoProcessor, AutoConfig

        logger.info("Loading MusicGen model (this may download ~6GB on first run)...")
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
        logger.info("Model loaded in %.0fs (%.0fM params, sr=%dHz)", time.time()-t0, params_m, sr)

        # ~50 tokens per second of audio, capped at model's max_position_embeddings
        max_positions = model.config.decoder.max_position_embeddings
        max_tokens = min(int(gen_duration * 50), max_positions - 10)
        actual_dur = max_tokens / 50
        if actual_dur < gen_duration:
            logger.warning("Model max position limit: capping at %.0fs "
                           "(requested %ds, max_positions=%d)", actual_dur, gen_duration, max_positions)
        logger.info("Generating %ds audio (%d tokens)... "
                    "Estimated time: ~%dmin", gen_duration, max_tokens, gen_duration * 20 // 60)
        inputs = processor(text=[prompt], padding=True, return_tensors="pt")
        t0 = time.time()
        with torch.no_grad():
            audio = model.generate(**inputs, max_new_tokens=max_tokens)
        gen_time = time.time() - t0

        dur = audio.shape[-1] / sr
        logger.info("Generated %.1fs of audio in %.0fs (%.1fx realtime)", dur, gen_time, gen_time/dur)

        out_path = cache_dir / f"{cache_key}.wav"
        logger.info("Saving to %s...", out_path)
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

        logger.info("Music saved: %s (%dKB)", out_path.name, out_path.stat().st_size // 1024)
        return out_path

    except Exception as e:
        import traceback
        logger.error("Music generation failed: %s", e)
        logger.error(traceback.format_exc())
        return None


def generate_music(
    trip_type: str,
    style: str,
    target_duration: int,
    cache_dir: Path,
    mood: str = "",
    backend: str = "local",
) -> Path | None:
    """Generate background music using the specified backend.

    backend: "local" (MusicGen) or "gemini" (Lyria RealTime API)
    """
    if backend == "gemini":
        from .music_gemini import fetch_music_gemini
        return fetch_music_gemini(
            trip_type=trip_type, style=style, target_duration=target_duration,
            cache_dir=cache_dir, mood=mood,
        )
    return fetch_music(
        trip_type=trip_type, style=style, target_duration=target_duration,
        cache_dir=cache_dir, mood=mood,
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
) -> bool:
    """Build composite music from per-segment tracks with crossfades.

    segment_tracks: [(segment_duration, music_wav_path), ...]
    Returns True on success.
    """
    from .media_utils import run_subprocess
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
        logger.error("Composite music build failed: %s", result.stderr[-200:])
        return False

    logger.info("Composite music: %d segments crossfaded into %s", len(segment_tracks), output_path.name)
    return True


def generate_music_for_edl(
    cfg,
    backend: str = "local",
) -> Path | None:
    """Generate per-segment music and build a composite track with crossfades.

    Called by the generate_music stage. Generates one Lyria track per
    segment based on its music_mood, then crossfades them into one file.

    Returns the composite music file path, or None if skipped/failed.
    """
    from .edl import load_latest_edl, save_edl
    from .edl import MusicTrack

    edl, version = load_latest_edl(cfg)

    if edl.music_mode != "auto":
        logger.info("Music mode is '%s', skipping generation", edl.music_mode)
        return None

    if edl.music and Path(edl.music.file).exists():
        logger.info("Music file already exists: %s", edl.music.file)
        return Path(edl.music.file)

    music_cache = cfg.music_dir

    # Generate per-segment music tracks
    logger.info("Generating per-segment music: %d segments, backend=%s", len(edl.segments), backend)
    segment_tracks: list[tuple[float, Path]] = []

    for i, seg in enumerate(edl.segments):
        seg_dur = int(_segment_duration(seg))
        mood = seg.music_mood or f"{edl.style} travel vlog background music"
        logger.info("  Segment %d/%d: \"%s\" (%ds)", i+1, len(edl.segments), seg.name, seg_dur)
        logger.info("    Mood: %s", mood)

        track = generate_music(
            trip_type=edl.trip_type, style=edl.style,
            target_duration=seg_dur,
            cache_dir=music_cache, mood=mood,
            backend=backend,
        )
        if track:
            seg.music_file = str(track)
            segment_tracks.append((seg_dur, track))
            logger.info("    Generated: %s", track.name)
        else:
            logger.warning("    FAILED — segment will be silent")

    if not segment_tracks:
        logger.warning("No music generated for any segment")
        return None

    # Build composite with crossfades
    music_cache.mkdir(parents=True, exist_ok=True)
    composite_path = music_cache / f"composite_{edl.trip_type}_{edl.style}_{int(edl.estimated_duration())}s.wav"
    if not _build_composite_music(segment_tracks, composite_path, crossfade=2.0):
        # Fallback: use first segment's track
        logger.warning("Composite build failed, using first segment track")
        composite_path = segment_tracks[0][1]

    edl.music = MusicTrack(file=str(composite_path))
    save_edl(cfg, edl, version)
    logger.info("Per-segment music saved to EDL v%d: %s", version, composite_path)

    return composite_path
