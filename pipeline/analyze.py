"""Stage 2b: Analyze media with local vision model — tiered by preprocess results."""

from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path

from PIL import Image
from tqdm import tqdm

from .config import Config
from .llm import ollama_json
from .media_utils import (
    convert_heic, extract_frames, run_subprocess,
    detect_scenes, extract_scene_keyframe, classify_motion,
    generate_thumbnail,
)

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v"}

# Rich prompts for vision analysis — detailed descriptions feed into Claude API planner
VISION_PROMPT_FAMILY = """\
This is a family travel photo. Describe it in detail for a vlog editor. Respond in JSON only:
{
  "description": "2-3 sentences: what's happening, who's visible, their expressions and body language, the background/setting, what makes this moment special or memorable",
  "setting": "specific place or setting, e.g. 'Marina Bay waterfront promenade at golden hour'",
  "mood": "emotional tone, e.g. 'joyful and playful', 'peaceful and reflective', 'excited discovery'",
  "activity": "what people are doing, e.g. 'posing together in front of the skyline', 'sharing a meal at a hawker stall'",
  "togetherness": <1-10 integer>,
  "genuine_emotion": <1-10 integer>,
  "story_beat": "<one of: arrival, discovery, meal, activity, landmark, candid, posed, scenery, selfie, other>",
  "visual_quality": <1-10 integer>,
  "vlog_worthy": <true or false>,
  "issues": "any problems: finger blocking lens, blurry, too dark, overexposed, bad framing, eyes closed/semi-closed, or empty string if none"
}

Scoring guide for togetherness:
  9-10: people hugging, arms around each other, heads touching, holding hands
  6-8: people standing close together, leaning in, sharing something
  3-5: people in same frame but separate, posed side by side
  1-2: one person or no people visible

Scoring guide for genuine_emotion:
  9-10: real laughter, open-mouth smiles, eyes crinkled with joy, delighted surprise
  6-8: warm genuine smiles, relaxed happy expressions
  3-5: polite smiles, neutral, looking at camera
  1-2: no faces visible, backs turned, or negative expressions

Scoring guide for visual_quality:
  9-10: great framing, sharp focus, good lighting, interesting angle
  5-8: decent photo, clear subject, acceptable lighting
  1-4: blurry, dark, overexposed, bad framing, obstructed by finger/object"""

VISION_PROMPT_VIDEO_SCENE = """\
This is a keyframe from a travel video clip. Describe what's happening for a vlog editor. Respond in JSON only:
{
  "description": "2-3 sentences: what's visible in this moment, the setting, any action or movement implied, what makes this worth including in a vlog",
  "setting": "specific place or setting visible",
  "mood": "emotional tone or visual atmosphere",
  "activity": "what's happening — any visible action, movement, or activity",
  "scene_type": "<landmark, nature, food, street, building, activity, people, transport, nightscape, beach, market, other>",
  "visual_quality": <1-10>,
  "vlog_worthy": <true or false>,
  "issues": "any problems: blurry, too dark, overexposed, obstructed, or empty string if none"
}"""

VISION_PROMPT_SCENE = """\
This is a travel/scenery photo. Describe it in detail for a vlog editor. Respond in JSON only:
{
  "description": "2-3 sentences: what place or scene is shown, time of day, lighting, atmosphere, notable landmarks or features visible",
  "setting": "specific place or setting, e.g. 'Gardens by the Bay Supertree Grove at night with light show'",
  "scene_type": "<landmark, nature, food, street, building, transport, nightscape, beach, market, other>",
  "mood": "visual atmosphere, e.g. 'dramatic golden hour', 'bustling and vibrant', 'serene and peaceful'",
  "visual_quality": <1-10>,
  "vlog_worthy": <true or false>,
  "issues": "any problems: finger blocking lens, blurry, too dark, overexposed, or empty string if none"
}"""


def _manage_pid(workspace: Path) -> None:
    """Write PID file and kill any previous analyze process."""
    pid_path = workspace / "analyze.pid"
    if pid_path.exists():
        try:
            old_pid = int(pid_path.read_text().strip())
            if old_pid != os.getpid():
                os.kill(old_pid, signal.SIGTERM)
                pass  # killed previous analyze process
        except (ProcessLookupError, ValueError, PermissionError, OSError):
            pass
    pid_path.write_text(str(os.getpid()))


def _load_existing_analysis(path: Path) -> dict[int, dict]:
    """Load existing analysis.json for resume support."""
    existing: dict[int, dict] = {}
    if path.exists():
        for entry in json.loads(path.read_text()):
            existing[entry["id"]] = entry
    return existing


def _check_ollama_model(cfg: Config, log_fn) -> None:
    """Verify the Ollama vision model is available. Logs clear error if not."""
    import httpx
    try:
        resp = httpx.post(
            f"{cfg.ollama_base}/api/show",
            json={"name": cfg.vision_model},
            timeout=10,
        )
        if resp.status_code == 404:
            log_fn(
                f"ERROR: Ollama model '{cfg.vision_model}' not found! "
                f"Run: ollama pull {cfg.vision_model}"
            )
            raise RuntimeError(
                f"Ollama model '{cfg.vision_model}' not found. "
                f"Pull it with: ollama pull {cfg.vision_model}"
            )
        resp.raise_for_status()
        log_fn(f"Vision model: {cfg.vision_model} (OK)")
    except httpx.ConnectError:
        log_fn(
            f"WARNING: Cannot connect to Ollama at {cfg.ollama_base}. "
            f"Vision analysis will be skipped."
        )


def analyze(cfg: Config, *, skip_vision: bool = False, progress_callback=None, log_fn=None) -> list[dict]:
    """Analyze items: generate thumbnails/keyframes, optionally run local vision model.

    When skip_vision=True (visual mode): thumbnails for photos, keyframes for videos.
    When skip_vision=False: full vision model analysis via Ollama (llava).
    """
    _log = log_fn or print
    cfg.ensure_dirs()
    preprocessed_path = cfg.workspace / "preprocessed.json"
    analysis_path = cfg.workspace / "analysis.json"

    _manage_pid(cfg.workspace)
    if skip_vision:
        _log("Visual planner mode — skipping local vision model, generating thumbnails only")
    else:
        _check_ollama_model(cfg, _log)

    preprocessed = json.loads(preprocessed_path.read_text())
    items = preprocessed["items"]

    # Split by tier
    tier_a = [x for x in items if x["tier"] == "A"]
    tier_b = [x for x in items if x["tier"] == "B"]
    tier_c = [x for x in items if x["tier"] == "C"]
    tier_d = [x for x in items if x["tier"] == "D"]
    if skip_vision:
        # Visual mode: include all items — let Gemini decide what's worth including
        to_analyze = tier_a + tier_b + tier_c + tier_d
        _log(f"Analyzing (visual mode): {len(to_analyze)} items (all tiers, Gemini decides)")
    else:
        to_analyze = tier_a + tier_b + tier_c
        _log(f"Analyzing: {len(tier_a)} tier-A + {len(tier_b)} tier-B + {len(tier_c)} tier-C "
             f"= {len(to_analyze)} items (skipping {len(tier_d)} tier-D)")

    # Load existing analysis to support resuming (run-level)
    existing = _load_existing_analysis(analysis_path)

    # Per-file analysis cache (shared across runs)
    cache_dir = cfg.cache_dir

    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    results = []
    # Phase 1: Collect entries, check caches, identify items needing vision
    needs_vision: list[tuple[int, dict, Path, str, dict | None]] = []  # (index, entry, target, prompt, scene_entry)

    for i, item in enumerate(to_analyze, 1):
        item_id = item["id"]

        if item_id in existing and existing[item_id].get("vision"):
            results.append(existing[item_id])
            continue

        local_path = Path(item["local_path"])
        suffix = local_path.suffix.lower()
        is_video = suffix in VIDEO_EXTENSIONS

        entry = {
            "id": item_id,
            "filename": item["filename"],
            "local_path": item["local_path"],
            "media_type": "video" if is_video else "photo",
            "item_type": item.get("item_type", 0),
            "takentime": item.get("takentime"),
            "taken_iso": item.get("taken_iso"),
            "duration_ms": item.get("duration"),
            "tier": item["tier"],
            "family_count": item.get("family_count", 0),
            "family_names": item.get("family_names", []),
            "country": item.get("country"),
            "first_level": item.get("first_level"),
            "district": item.get("district"),
            "persons": item.get("metadata", {}).get("persons", []),
            "cluster_size": item.get("cluster_size", 1),
        }

        # Check shared per-file cache
        cache_file = cache_dir / f"{item_id}.json"
        if cache_file.exists():
            try:
                cached = json.loads(cache_file.read_text())
                entry.update(cached)
                results.append(entry)
                _log(f"[{i}/{len(to_analyze)}] {item['filename']} — shared cache hit")
                continue
            except (json.JSONDecodeError, KeyError):
                pass

        # For video, extract keyframes and optionally detect scenes
        if is_video:
            if skip_vision:
                # Visual mode: extract 5 keyframes in ONE FFmpeg call + transcript.
                # Gemini sees filmstrip for scene selection, gets trim points from duration.
                _log(f"[{i}/{len(to_analyze)}] {item['filename']} — video keyframes...")
                probe = run_subprocess(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "csv=p=0", str(local_path)],
                    capture_output=True, text=True,
                )
                try:
                    total_dur = float(probe.stdout.strip())
                except (ValueError, AttributeError):
                    total_dur = 10.0

                # Extract 5 keyframes in a single FFmpeg pass
                kf_dir = cfg.keyframes_dir
                kf_dir.mkdir(parents=True, exist_ok=True)
                kf_pattern = kf_dir / f"{item_id}_%02d.jpg"
                fps_val = 5.0 / max(total_dur, 1.0)
                existing_kfs = sorted(kf_dir.glob(f"{item_id}_*.jpg"))
                if len(existing_kfs) < 3:
                    run_subprocess(
                        ["ffmpeg", "-y", "-i", str(local_path),
                         "-vf", f"fps={fps_val:.6f},scale=512:-1",
                         "-frames:v", "5", "-q:v", "3",
                         str(kf_pattern)],
                        capture_output=True,
                    )
                    existing_kfs = sorted(kf_dir.glob(f"{item_id}_*.jpg"))

                entry["keyframe_paths"] = [str(p) for p in existing_kfs]
                entry["video_duration"] = round(total_dur, 1)

                # Build scene entries from keyframes for filmstrip
                interval = total_dur / max(len(existing_kfs), 1)
                entry["scenes"] = [
                    {
                        "scene_index": idx,
                        "start": round(idx * interval, 1),
                        "end": round((idx + 1) * interval, 1),
                        "duration": round(interval, 1),
                        "motion": "unknown",
                        "keyframe": str(kf),
                    }
                    for idx, kf in enumerate(existing_kfs)
                ]

                transcript = _transcribe(local_path, cfg)
                if transcript:
                    entry["transcript"] = transcript

                # Save to shared cache so re-runs skip this video
                cache_entry = {k: v for k, v in entry.items()
                               if k in ("keyframe_paths", "transcript", "scenes", "video_duration", "thumbnail_path")}
                cache_file = cache_dir / f"{item_id}.json"
                cache_file.write_text(json.dumps(cache_entry, indent=2))

                results.append(entry)
                _log(f"[{i}/{len(to_analyze)}] {item['filename']} — {len(existing_kfs)} keyframes ({total_dur:.0f}s)")
            else:
                # Local vision mode: full scene detection + motion classification
                _log(f"[{i}/{len(to_analyze)}] {item['filename']} — detecting scenes...")
                scenes = detect_scenes(local_path)
                entry["scenes"] = []
                transcript = _transcribe(local_path, cfg)
                if transcript:
                    entry["transcript"] = transcript

                kf_paths = extract_frames(local_path, cfg.keyframes_dir, prefix=str(item_id), count=5)
                entry["keyframe_paths"] = [str(p) for p in kf_paths]

                for scene in scenes:
                    kf = extract_scene_keyframe(local_path, scene, cfg.keyframes_dir, str(item_id))
                    motion = classify_motion(local_path, scene["start"], min(scene["duration"], 5))
                    scene_entry = {
                        "scene_index": scene["scene_index"],
                        "start": scene["start"],
                        "end": scene["end"],
                        "duration": scene["duration"],
                        "motion": motion,
                        "keyframe": str(kf) if kf else None,
                    }
                    entry["scenes"].append(scene_entry)
                    if kf and kf.exists():
                        prompt = VISION_PROMPT_FAMILY if item["tier"] in ("A", "B") else VISION_PROMPT_VIDEO_SCENE
                        needs_vision.append((i, entry, kf, prompt, scene_entry))
                if not entry["scenes"]:
                    # Fallback: use first keyframe like before
                    vision_target = kf_paths[0] if kf_paths else None
                    prompt = VISION_PROMPT_FAMILY if item["tier"] in ("A", "B") else VISION_PROMPT_SCENE
                    if vision_target and vision_target.exists():
                        needs_vision.append((i, entry, vision_target, prompt, None))
                    else:
                        results.append(entry)
        else:
            if skip_vision:
                # Visual mode: generate thumbnail, no vision model
                thumb_dir = cfg.workspace / "thumbnails"
                thumb = generate_thumbnail(local_path, thumb_dir, size=512)
                entry["thumbnail_path"] = str(thumb)
                # Cache so re-runs are instant
                cache_file = cache_dir / f"{item_id}.json"
                cache_file.write_text(json.dumps({"thumbnail_path": str(thumb)}, indent=2))
                results.append(entry)
            else:
                vision_target = local_path
                prompt = VISION_PROMPT_FAMILY if item["tier"] in ("A", "B") else VISION_PROMPT_SCENE
                if vision_target and vision_target.exists():
                    needs_vision.append((i, entry, vision_target, prompt, None))
                else:
                    results.append(entry)

    _log(f"Cache resolved: {len(results)} cached, {len(needs_vision)} need vision analysis")

    # Phase 2: Run vision analysis with concurrent requests (2-4 workers)
    # This keeps the GPU busy — while one request waits for HTTP, another is doing inference
    n_workers = min(4, len(needs_vision)) if needs_vision else 1
    done_count = 0
    lock = threading.Lock()
    # Disable tqdm when stderr is not a TTY (e.g. inside Dagster compute logs)
    # — tqdm's status_printer flushes stderr which causes BrokenPipeError
    import sys
    use_tqdm = hasattr(sys.stderr, "fileno") and sys.stderr.isatty()
    pbar = tqdm(total=len(needs_vision), desc="Analyzing", unit="item",
                bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",
                disable=not use_tqdm)

    def _process_item(args):
        idx, entry, target, prompt, scene_entry = args
        vision = _analyze_image(target, cfg, prompt)
        if vision:
            if scene_entry is not None:
                # Per-scene vision — attach to the scene entry
                scene_entry["vision"] = vision
            else:
                # Photo or fallback video — attach to the main entry
                entry["vision"] = vision
        # Save to shared per-file cache
        cache_entry = {k: v for k, v in entry.items()
                       if k in ("vision", "keyframe_paths", "transcript", "scenes", "thumbnail_path")}
        if cache_entry:
            cf = cache_dir / f"{entry['id']}.json"
            cf.write_text(json.dumps(cache_entry, indent=2))
        return idx, entry

    seen_ids: set[int] = {r["id"] for r in results}  # track which entries already in results

    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = {executor.submit(_process_item, args): args for args in needs_vision}
        for future in as_completed(futures):
            idx, entry = future.result()
            # Only add entry once (videos may have multiple scene vision tasks)
            if entry["id"] not in seen_ids:
                results.append(entry)
                seen_ids.add(entry["id"])
            has_vision = entry.get("vision") or any(
                s.get("vision") for s in entry.get("scenes", [])
            )
            status = "analyzed" if has_vision else "pending scenes"
            _log(f"[{idx}/{len(to_analyze)}] {entry['filename']} — {status}")
            with lock:
                done_count += 1
                pbar.update(1)
                if progress_callback:
                    progress_callback(done_count, len(needs_vision), entry.get("filename", ""))
                # Save incrementally every 5 items
                if done_count % 5 == 0 or done_count == len(needs_vision):
                    analysis_path.write_text(json.dumps(results, indent=2))

    pbar.close()

    # For videos with per-scene vision, set top-level vision from best scene
    for entry in results:
        if entry.get("scenes") and not entry.get("vision"):
            best_scene = max(
                (s for s in entry["scenes"] if s.get("vision")),
                key=lambda s: s["vision"].get("visual_quality", 0),
                default=None,
            )
            if best_scene:
                entry["vision"] = best_scene["vision"]

    # Final save
    analysis_path.write_text(json.dumps(results, indent=2))
    (cfg.workspace / "analyze.pid").unlink(missing_ok=True)

    ok = sum(1 for r in results if r.get("vision"))
    _log(f"Analysis complete: {ok}/{len(results)} with vision results")
    return results


def _analyze_image(image_path: Path, cfg: Config, prompt: str) -> dict | None:
    """Send image to Ollama vision model for analysis."""
    try:
        if image_path.suffix.lower() in {".heic", ".heif"}:
            try:
                image_path = convert_heic(image_path)
            except RuntimeError:
                return None

        img = Image.open(image_path)
        if max(img.size) > 1536:
            img.thumbnail((1536, 1536))
            resized = image_path.parent / f"_resized_{image_path.name}"
            img.save(resized, "JPEG", quality=85)
            image_path = resized

        return ollama_json(cfg, model=cfg.vision_model, prompt=prompt, images=[image_path])
    except Exception as e:
        pass  # vision failed, entry will have no vision data
        return None


def _transcribe(video_path: Path, cfg: Config) -> str | None:
    """Transcribe audio from video using mlx-whisper or whisper CLI."""
    audio_path = video_path.parent / f"_audio_{video_path.stem}.wav"
    run_subprocess(
        ["ffmpeg", "-y", "-i", str(video_path),
         "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
         str(audio_path)],
        capture_output=True,
    )
    if not audio_path.exists():
        return None

    transcript = None
    try:
        import mlx_whisper
        result = mlx_whisper.transcribe(
            str(audio_path),
            path_or_hf_repo=f"mlx-community/whisper-{cfg.whisper_model}-mlx",
        )
        transcript = result.get("text", "").strip()
    except ImportError:
        # Try openai-whisper (cross-platform, GPU or CPU)
        try:
            import whisper as openai_whisper
            model = openai_whisper.load_model(cfg.whisper_model)
            result = model.transcribe(str(audio_path))
            transcript = result.get("text", "").strip()
        except ImportError:
            # Try whisper-cpp CLI (requires manual build)
            try:
                result = run_subprocess(
                    ["whisper-cpp", "-m", f"models/ggml-{cfg.whisper_model}.bin",
                     "-f", str(audio_path), "--no-timestamps"],
                    capture_output=True, text=True,
                )
                if result.returncode == 0:
                    transcript = result.stdout.strip()
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
    except Exception as e:
        pass  # transcription failed

    audio_path.unlink(missing_ok=True)
    return transcript if transcript else None
