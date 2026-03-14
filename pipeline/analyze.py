"""Stage 2b: Analyze media with local vision model — tiered by preprocess results."""

from __future__ import annotations

import base64
import json
import os
import signal
import subprocess
from pathlib import Path

import httpx
from PIL import Image

from .config import Config

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v"}

# Prompt tuned for family trip happiness — concrete anchoring for a 7B model
VISION_PROMPT_FAMILY = """\
This is a family vacation photo. Analyze it and respond in JSON only, no other text:
{
  "description": "one sentence describing the scene",
  "togetherness": <1-10 integer>,
  "genuine_emotion": <1-10 integer>,
  "story_beat": "<one of: arrival, discovery, meal, activity, landmark, candid, posed, scenery, selfie, other>",
  "visual_quality": <1-10 integer>,
  "vlog_worthy": <true or false>
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

Scoring guide for composition:
  9-10: great framing, sharp focus, good lighting, interesting angle
  5-8: decent photo, clear subject, acceptable lighting
  1-4: blurry, dark, overexposed, bad framing, obstructed"""

VISION_PROMPT_SCENE = """\
This is a travel photo. Analyze it briefly. Respond in JSON only:
{
  "description": "one sentence",
  "scene_type": "<landmark, nature, food, street, building, transport, other>",
  "visual_quality": <1-10>,
  "vlog_worthy": <true or false>
}"""


def analyze(cfg: Config) -> list[dict]:
    """Analyze items from preprocessed.json — tier A+B with full prompt, tier C quick scan."""
    cfg.ensure_dirs()
    preprocessed_path = cfg.workspace / "preprocessed.json"
    analysis_path = cfg.workspace / "analysis.json"
    pid_path = cfg.workspace / "analyze.pid"

    # Kill any previous analyze process
    if pid_path.exists():
        try:
            old_pid = int(pid_path.read_text().strip())
            if old_pid != os.getpid():
                os.kill(old_pid, signal.SIGTERM)
                print(f"Stopped previous analyze (PID {old_pid})")
        except (ProcessLookupError, ValueError, PermissionError):
            pass
    pid_path.write_text(str(os.getpid()))

    preprocessed = json.loads(preprocessed_path.read_text())
    items = preprocessed["items"]

    # Split by tier
    tier_a = [x for x in items if x["tier"] == "A"]
    tier_b = [x for x in items if x["tier"] == "B"]
    tier_c = [x for x in items if x["tier"] == "C"]
    tier_d = [x for x in items if x["tier"] == "D"]
    to_analyze = tier_a + tier_b + tier_c
    print(f"Analyzing: {len(tier_a)} tier-A + {len(tier_b)} tier-B + {len(tier_c)} tier-C "
          f"= {len(to_analyze)} items (skipping {len(tier_d)} tier-D)")

    # Load existing analysis to support resuming
    existing: dict[int, dict] = {}
    if analysis_path.exists():
        for entry in json.loads(analysis_path.read_text()):
            existing[entry["id"]] = entry

    results = []
    for i, item in enumerate(to_analyze, 1):
        item_id = item["id"]

        # Resume: skip if already analyzed WITH vision results
        if item_id in existing and existing[item_id].get("vision"):
            results.append(existing[item_id])
            continue

        local_path = Path(item["local_path"])
        suffix = local_path.suffix.lower()
        is_video = suffix in VIDEO_EXTENSIONS

        print(f"  [{i}/{len(to_analyze)}] {item['tier']} {item['filename']} "
              f"(family={item['family_count']})...")

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

        # For video, extract keyframes
        if is_video:
            kf_paths = _extract_keyframes(local_path, cfg.workspace / "keyframes", item_id)
            entry["keyframe_paths"] = [str(p) for p in kf_paths]
            vision_target = kf_paths[0] if kf_paths else None

            transcript = _transcribe(local_path, cfg)
            if transcript:
                entry["transcript"] = transcript
        else:
            vision_target = local_path

        # Vision analysis — use family prompt for A+B, scene prompt for C
        if vision_target and vision_target.exists():
            prompt = VISION_PROMPT_FAMILY if item["tier"] in ("A", "B") else VISION_PROMPT_SCENE
            vision = _analyze_image(vision_target, cfg, prompt)
            if vision:
                entry["vision"] = vision

        results.append(entry)
        # Save incrementally
        analysis_path.write_text(json.dumps(results, indent=2))

    pid_path.unlink(missing_ok=True)

    ok = sum(1 for r in results if r.get("vision"))
    print(f"Analysis complete: {ok}/{len(results)} with vision results")
    return results


def _extract_keyframes(video_path: Path, keyframe_dir: Path, item_id: int, max_frames: int = 5) -> list[Path]:
    """Extract evenly-spaced frames from a video."""
    keyframe_dir.mkdir(parents=True, exist_ok=True)
    pattern = keyframe_dir / f"{item_id}_%02d.jpg"

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(video_path)],
        capture_output=True, text=True,
    )
    try:
        duration = float(probe.stdout.strip())
    except (ValueError, AttributeError):
        duration = 10.0

    interval = max(duration / (max_frames + 1), 0.5)
    select_expr = "+".join(
        f"eq(n\\,{int(i * interval * 30)})" for i in range(1, max_frames + 1)
    )
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(video_path),
         "-vf", f"select='{select_expr}',scale=1024:-1",
         "-vsync", "vfr", "-frames:v", str(max_frames), "-q:v", "3",
         str(pattern)],
        capture_output=True,
    )
    return sorted(keyframe_dir.glob(f"{item_id}_*.jpg"))


def _analyze_image(image_path: Path, cfg: Config, prompt: str) -> dict | None:
    """Send image to Ollama vision model for analysis."""
    try:
        suffix = image_path.suffix.lower()
        if suffix in {".heic", ".heif"}:
            jpeg_path = image_path.parent / f"_converted_{image_path.stem}.jpg"
            if not jpeg_path.exists():
                subprocess.run(
                    ["ffmpeg", "-y", "-i", str(image_path),
                     "-vf", "scale='min(1536,iw)':-1", "-q:v", "3",
                     str(jpeg_path)],
                    capture_output=True,
                )
            if jpeg_path.exists():
                image_path = jpeg_path
            else:
                return None

        img = Image.open(image_path)
        if max(img.size) > 1536:
            img.thumbnail((1536, 1536))
            resized = image_path.parent / f"_resized_{image_path.name}"
            img.save(resized, "JPEG", quality=85)
            image_path = resized

        img_b64 = base64.b64encode(image_path.read_bytes()).decode()

        resp = httpx.post(
            f"{cfg.ollama_base}/api/chat",
            json={
                "model": cfg.vision_model,
                "messages": [{"role": "user", "content": prompt, "images": [img_b64]}],
                "stream": False,
                "options": {"temperature": 0.1},
            },
            timeout=120,
        )
        resp.raise_for_status()
        content = resp.json()["message"]["content"]

        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0]
        return json.loads(content)
    except Exception as e:
        print(f"    Vision failed: {e}")
        return None


def _transcribe(video_path: Path, cfg: Config) -> str | None:
    """Transcribe audio from video using mlx-whisper or whisper CLI."""
    audio_path = video_path.parent / f"_audio_{video_path.stem}.wav"
    subprocess.run(
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
        try:
            result = subprocess.run(
                ["whisper-cpp", "-m", f"models/ggml-{cfg.whisper_model}.bin",
                 "-f", str(audio_path), "--no-timestamps"],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                transcript = result.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    except Exception as e:
        print(f"    Transcription failed: {e}")

    audio_path.unlink(missing_ok=True)
    return transcript if transcript else None
