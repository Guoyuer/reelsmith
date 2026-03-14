"""Stage 2: Analyze media with local vision model (Ollama) and whisper."""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

import httpx
from PIL import Image

from .config import Config

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v"}

VISION_PROMPT = """\
Analyze this image from a family vacation. Respond in JSON only, no other text:
{
  "description": "one sentence describing the scene",
  "people_count": <int>,
  "activities": "what people are doing",
  "happiness_score": <1-10 int, how happy/joyful the moment looks>,
  "quality_score": <1-10 int, visual quality: focus, lighting, composition>,
  "scene_type": "<one of: food, landmark, nature, family, selfie, activity, transit, accommodation, nightlife, shopping, street, other>",
  "time_of_day": "<morning, afternoon, evening, night>",
  "vlog_worthy": <true/false, is this interesting enough for a highlight reel>
}"""


def analyze(cfg: Config) -> list[dict]:
    """Analyze all items in the manifest with vision model and whisper."""
    cfg.ensure_dirs()
    manifest_path = cfg.workspace / "manifest.json"
    analysis_path = cfg.workspace / "analysis.json"

    manifest = json.loads(manifest_path.read_text())
    print(f"Analyzing {len(manifest)} items...")

    # Load existing analysis to support resuming
    existing = {}
    if analysis_path.exists():
        for entry in json.loads(analysis_path.read_text()):
            existing[entry["id"]] = entry

    results = []
    for i, item in enumerate(manifest, 1):
        item_id = item["id"]
        if item_id in existing:
            print(f"  [{i}/{len(manifest)}] {item['filename']} (cached)")
            results.append(existing[item_id])
            continue

        print(f"  [{i}/{len(manifest)}] {item['filename']}...")
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
            "country": item.get("country"),
            "first_level": item.get("first_level"),
            "district": item.get("district"),
            "persons": item.get("metadata", {}).get("persons", []),
        }

        # Extract keyframes from video
        if is_video:
            keyframe_paths = _extract_keyframes(local_path, cfg.workspace / "keyframes", item_id)
            entry["keyframe_paths"] = [str(p) for p in keyframe_paths]
            analyze_path_for_vision = keyframe_paths[0] if keyframe_paths else None

            # Transcribe audio
            transcript = _transcribe(local_path, cfg)
            if transcript:
                entry["transcript"] = transcript
        else:
            analyze_path_for_vision = local_path

        # Vision analysis
        if analyze_path_for_vision and analyze_path_for_vision.exists():
            vision = _analyze_image(analyze_path_for_vision, cfg)
            if vision:
                entry["vision"] = vision

        results.append(entry)

        # Save incrementally
        analysis_path.write_text(json.dumps(results, indent=2))

    print(f"Analysis saved: {len(results)} items")
    return results


def _extract_keyframes(video_path: Path, keyframe_dir: Path, item_id: int, max_frames: int = 5) -> list[Path]:
    """Extract evenly-spaced frames from a video."""
    keyframe_dir.mkdir(parents=True, exist_ok=True)
    pattern = keyframe_dir / f"{item_id}_%02d.jpg"

    # Get duration first
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            str(video_path),
        ],
        capture_output=True, text=True,
    )
    try:
        duration = float(probe.stdout.strip())
    except (ValueError, AttributeError):
        duration = 10.0

    # Extract evenly-spaced frames
    interval = max(duration / (max_frames + 1), 0.5)
    select_expr = "+".join(f"eq(n\\,{int(i * interval * 30)})" for i in range(1, max_frames + 1))

    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(video_path),
            "-vf", f"select='{select_expr}',scale=1024:-1",
            "-vsync", "vfr",
            "-frames:v", str(max_frames),
            "-q:v", "3",
            str(pattern),
        ],
        capture_output=True,
    )

    return sorted(keyframe_dir.glob(f"{item_id}_*.jpg"))


def _analyze_image(image_path: Path, cfg: Config) -> dict | None:
    """Send image to Ollama vision model for analysis."""
    try:
        # Resize large images to save memory
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
                "messages": [
                    {
                        "role": "user",
                        "content": VISION_PROMPT,
                        "images": [img_b64],
                    }
                ],
                "stream": False,
                "options": {"temperature": 0.1},
            },
            timeout=120,
        )
        resp.raise_for_status()
        content = resp.json()["message"]["content"]

        # Parse JSON from response (handle markdown code blocks)
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0]
        return json.loads(content)
    except Exception as e:
        print(f"    Vision analysis failed: {e}")
        return None


def _transcribe(video_path: Path, cfg: Config) -> str | None:
    """Transcribe audio from video using mlx-whisper or whisper CLI."""
    # Extract audio first
    audio_path = video_path.parent / f"_audio_{video_path.stem}.wav"
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(video_path),
            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            str(audio_path),
        ],
        capture_output=True,
    )
    if not audio_path.exists():
        return None

    transcript = None

    # Try mlx-whisper (Python)
    try:
        import mlx_whisper
        result = mlx_whisper.transcribe(
            str(audio_path),
            path_or_hf_repo=f"mlx-community/whisper-{cfg.whisper_model}-mlx",
        )
        transcript = result.get("text", "").strip()
    except ImportError:
        # Try whisper CLI (whisper.cpp via Homebrew)
        try:
            result = subprocess.run(
                ["whisper-cpp", "-m", f"models/ggml-{cfg.whisper_model}.bin", "-f", str(audio_path), "--no-timestamps"],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                transcript = result.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    except Exception as e:
        print(f"    Transcription failed: {e}")

    # Cleanup
    audio_path.unlink(missing_ok=True)

    return transcript if transcript else None
