"""Stage 5: Self-critique, variations, and human-feedback iteration (via Claude API)."""

from __future__ import annotations

import json
from pathlib import Path

from .assemble import assemble
from .config import Config
from .edl import EDL
from .llm import ollama_chat
from .media_utils import extract_frames, strip_markdown_fences


CRITIQUE_SYSTEM = """\
You are a professional vlog editor reviewing your own work. You will receive the
current EDL (Edit Decision List) for a travel vlog. Critique it and output an
improved version.

Evaluate:
1. **Pacing** — does duration vary enough? Fast moments 3s, emotional beats 5-6s.
2. **Story flow** — does the sequence build emotionally? Is there a clear arc?
3. **Variety** — too many similar consecutive shots? Mix wide/close, people/scenery.
4. **Opening** — does it grab attention? The first 2-3 items set the tone.
5. **Closing** — does it feel complete? End with warmth or nostalgia, not a random shot.
6. **Transitions** — fade_black between major scene changes, crossfade within.
7. **Text overlays** — are day/location labels at natural transition points?
8. **Video trim points** — are start_time/end_time set to select the best moments?
9. **Redundancy** — remove duplicate or near-duplicate shots.

Output the improved EDL as valid JSON (same schema). Update narrative_rationale
to explain your changes. Only output JSON, no other text."""

FEEDBACK_SYSTEM = """\
You are a professional vlog editor revising your work based on viewer feedback.
You will receive the current EDL and specific feedback. Make targeted changes
to address the feedback while preserving what works.

Output the revised EDL as valid JSON (same schema). Only output JSON, no other text."""

VARIATION_SYSTEM = """\
You are a professional vlog editor creating a style variation of an existing vlog.
You will receive the original EDL and available media items. Create a new EDL
with a different editorial approach while using the same media pool.

Output the variation EDL as valid JSON (same schema). Only output JSON, no other text."""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _find_latest_version(cfg: Config) -> int:
    """Find the latest version number from edl_v*.json files."""
    versions = []
    for f in cfg.workspace.glob("edl_v*.json"):
        try:
            versions.append(int(f.stem.split("_v")[1]))
        except (IndexError, ValueError):
            pass
    return max(versions) if versions else 0


def _save_edl(cfg: Config, edl: EDL, version: int) -> Path:
    """Save EDL as workspace/edl_v{version}.json."""
    path = cfg.workspace / f"edl_v{version}.json"
    path.write_text(edl.model_dump_json(indent=2))
    return path


def _load_latest_edl(cfg: Config) -> tuple[EDL, int]:
    """Load the latest edl_v{N}.json. Falls back to edl.json for migration."""
    version = _find_latest_version(cfg)
    if version > 0:
        path = cfg.workspace / f"edl_v{version}.json"
    else:
        # Migration: old-style edl.json
        path = cfg.workspace / "edl.json"
        if not path.exists():
            raise FileNotFoundError(f"No EDL found in {cfg.workspace}")
        version = 1
    return EDL.model_validate_json(path.read_text()), version


def _gemini_revise(system: str, user: str, log_fn=None, label: str = "iterate") -> str:
    """Call Gemini API to revise an EDL. Returns raw response text."""
    import os
    import time as _time

    from google import genai
    from google.genai import types

    _log = log_fn or print
    model = "gemini-3-flash-preview"

    _log(f"=== Gemini API Call: {label} ===")
    _log(f"  Model: {model}")
    _log(f"  System prompt: {len(system)} chars")
    _log(f"  User message: {len(user)} chars")

    t0 = _time.monotonic()
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY", ""))
    response = client.models.generate_content(
        model=model,
        contents=[types.Content(parts=[types.Part(text=user)])],
        config=types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=16000,
            temperature=0.7,
        ),
    )

    elapsed = _time.monotonic() - t0
    content = response.text or ""
    usage = response.usage_metadata
    _log(f"  Response: {usage.prompt_token_count} input tokens, "
         f"{usage.candidates_token_count} output tokens, {elapsed:.1f}s")
    _log(f"  Output: {len(content)} chars")
    preview = content[:300].replace("\n", " ")
    _log(f"  Preview: {preview}...")
    _log(f"=== End {label} ===")
    return content


def _revise_and_render(cfg: Config, edl: EDL, system: str, user: str,
                       log_fn=None) -> EDL:
    """Call Gemini API (or Ollama fallback), parse revised EDL, save and re-render."""
    _log = log_fn or print

    try:
        content = strip_markdown_fences(_gemini_revise(system, user, log_fn=_log, label="revise EDL"))
    except Exception as e:
        _log(f"Gemini failed ({e}), falling back to Ollama")
        prompt = f"{system}\n\n{user}"
        content = strip_markdown_fences(
            ollama_chat(cfg, prompt=prompt, json_mode=True)
        )

    new_edl = EDL.model_validate_json(content)

    version = _find_latest_version(cfg) + 1
    _save_edl(cfg, new_edl, version)

    # Clear old clips to force re-render
    clips_dir = cfg.workspace / "clips"
    if clips_dir.exists():
        for f in clips_dir.iterdir():
            f.unlink(missing_ok=True)

    assemble(cfg, version=version)
    _log(f"  Rendered v{version}: {len(new_edl.segments)} segments, "
         f"{len(new_edl.all_items())} items, ~{new_edl.estimated_duration():.0f}s")
    return new_edl


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def self_critique(cfg: Config, *, style: str = "upbeat", max_rounds: int = 2,
                  log_fn=None) -> EDL:
    """Extract frames from rendered vlog, send to Gemini for critique, re-edit."""
    _log = log_fn or print
    edl, current_version = _load_latest_edl(cfg)

    for round_num in range(1, max_rounds + 1):
        _log(f"=== Self-Critique Round {round_num}/{max_rounds} ===")

        # Find the rendered video
        current_video = cfg.workspace / "output" / f"vlog_v{current_version}.mp4"
        if not current_video.exists():
            _log(f"  No rendered video at {current_video}, rendering first...")
            assemble(cfg, version=current_version)

        if current_video.exists():
            # Scale frame count with video duration (~1 frame per 3 seconds)
            from .media_utils import run_subprocess
            probe = run_subprocess(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(current_video)],
                capture_output=True, text=True,
            )
            try:
                video_dur = float(probe.stdout.strip())
            except (ValueError, AttributeError):
                video_dur = 30.0
            n_frames = max(4, min(int(video_dur / 3), 20))

            _log(f"  Extracting {n_frames} frames from {video_dur:.0f}s video...")
            review_dir = cfg.workspace / "review_frames"
            review_dir.mkdir(parents=True, exist_ok=True)
            for f in review_dir.glob("frame_*.jpg"):
                f.unlink()
            extract_frames(current_video, review_dir, prefix="frame", count=n_frames)

        user = f"""\
Critique and improve this {style} travel vlog EDL (round {round_num}/{max_rounds}).

Current EDL (v{current_version}):
{edl.model_dump_json(indent=2)}

Make it feel more {style}. Tighten pacing, strengthen the arc, remove weak shots."""

        try:
            edl = _revise_and_render(cfg, edl, CRITIQUE_SYSTEM, user, log_fn=_log)
            current_version = _find_latest_version(cfg)
            _log(f"=== End Critique Round {round_num} → v{current_version} ===")
        except Exception as e:
            _log(f"  Critique round {round_num} failed: {e}")
            break

    return edl


def apply_feedback(cfg: Config, feedback: str, log_fn=None) -> EDL:
    """Apply human feedback to the current EDL via Claude API and re-render."""
    _log = log_fn or print
    edl, _ = _load_latest_edl(cfg)
    _log(f"Applying feedback via Claude API: {feedback[:80]}...")

    user = f"""\
Revise this vlog EDL based on viewer feedback.

Current EDL:
{edl.model_dump_json(indent=2)}

Viewer feedback: "{feedback}"

Address the feedback while preserving what works. Output improved JSON only."""

    return _revise_and_render(cfg, edl, FEEDBACK_SYSTEM, user, log_fn=_log)


def generate_variations(cfg: Config, styles: list[str] | None = None,
                        log_fn=None) -> list[Path]:
    """Generate multiple vlog variations with different styles via Claude API."""
    _log = log_fn or print
    styles = styles or ["energetic", "reflective", "cinematic"]

    original_edl, _ = _load_latest_edl(cfg)

    analysis = json.loads((cfg.workspace / "analysis.json").read_text())
    media_summary = json.dumps(
        [{"local_path": a["local_path"], "media_type": a["media_type"],
          "duration_sec": round(a["duration_ms"]/1000, 1) if a.get("duration_ms") else None,
          "description": a.get("vision", {}).get("description"),
          "setting": a.get("vision", {}).get("setting"),
          "mood": a.get("vision", {}).get("mood"),
          "quality": a.get("vision", {}).get("visual_quality")}
         for a in analysis if a.get("vision")],
        indent=2,
    )

    outputs = []
    for variation_style in styles:
        _log(f"Generating {variation_style} variation via Claude API...")

        user = f"""\
Create a {variation_style} variation of this vlog.

Original EDL:
{original_edl.model_dump_json(indent=2)}

Available media:
{media_summary}

Style guidelines:
- "energetic": faster pacing, shorter clips (2-4s), more cuts, upbeat feel
- "reflective": slower pacing, longer clips (4-8s), more crossfades, warm feel
- "cinematic": dramatic pacing, varied shot lengths, fade_black transitions, epic feel

Create a fresh take that feels distinctly {variation_style}. Output JSON only."""

        try:
            _revise_and_render(cfg, original_edl, VARIATION_SYSTEM, user, log_fn=_log)
            video = max(
                (cfg.workspace / "output").glob("vlog_v*.mp4"),
                key=lambda p: p.stat().st_mtime,
            )
            outputs.append(video)
            _log(f"  → {video}")
        except Exception as e:
            _log(f"  Failed: {e}")

    return outputs
