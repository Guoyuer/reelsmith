"""Stage 5: Self-critique, variations, and human-feedback iteration (all via Ollama)."""

from __future__ import annotations

import json
from pathlib import Path

from .assemble import assemble
from .config import Config
from .edl import EDL
from .llm import ollama_chat
from .media_utils import extract_frames, strip_markdown_fences

CRITIQUE_PROMPT = """\
You are reviewing a vlog you edited. Here are evenly-spaced frames from the
rendered video. Critique the vlog and suggest improvements. Consider:

1. Pacing — are any clips too long or too short?
2. Story flow — does the sequence make narrative sense?
3. Variety — is there enough visual variety, or too many similar shots?
4. Opening/closing — do they feel strong?
5. Overall mood — does it feel {style}?

Current EDL:
{edl_json}

Based on your critique, output an improved EDL as JSON (same schema).
Only output the JSON, no other text."""

FEEDBACK_PROMPT = """\
You previously created this vlog EDL:

{edl_json}

The viewer gave this feedback:
"{feedback}"

Revise the EDL to address the feedback. Keep the same JSON schema.
Only output the JSON, no other text."""

VARIATION_PROMPT = """\
You previously created this vlog EDL:

{edl_json}

Available media items:
{media_summary}

Create a variation with style: {variation_style}
- "energetic": faster pacing, shorter clips (2-4s), more cuts, upbeat feel
- "reflective": slower pacing, longer clips (4-8s), more crossfades, warm feel
- "cinematic": dramatic pacing, varied shot lengths, fade_black transitions, epic feel

Only output the JSON EDL, no other text."""


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


def _revise_and_render(cfg: Config, edl: EDL, prompt: str, **chat_kwargs) -> EDL:
    """Call LLM, parse revised EDL, save version, clear clips, and re-render."""
    content = strip_markdown_fences(ollama_chat(cfg, prompt=prompt, json_mode=True, **chat_kwargs))
    new_edl = EDL.model_validate_json(content)

    version = _find_latest_version(cfg) + 1
    _save_edl(cfg, new_edl, version)

    # Clear old clips to force re-render
    clips_dir = cfg.workspace / "clips"
    if clips_dir.exists():
        for f in clips_dir.iterdir():
            f.unlink(missing_ok=True)

    assemble(cfg, version=version)
    print(f"  Rendered v{version}")
    return new_edl


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def self_critique(cfg: Config, *, style: str = "upbeat", max_rounds: int = 2) -> EDL:
    """Extract frames from the rendered vlog, send to vision model for critique, regenerate EDL."""
    edl, current_version = _load_latest_edl(cfg)

    for round_num in range(1, max_rounds + 1):
        print(f"Self-critique round {round_num}/{max_rounds}...")

        current_video = cfg.workspace / "output" / f"vlog_v{current_version}.mp4"
        if not current_video.exists():
            assemble(cfg, version=current_version)

        # Extract review frames (clean old ones first)
        review_dir = cfg.workspace / "review_frames"
        review_dir.mkdir(parents=True, exist_ok=True)
        for f in review_dir.glob("frame_*.jpg"):
            f.unlink()
        frames = extract_frames(current_video, review_dir, prefix="frame", count=8)

        prompt = CRITIQUE_PROMPT.format(style=style, edl_json=edl.model_dump_json(indent=2))

        try:
            edl = _revise_and_render(cfg, edl, prompt, model=cfg.vision_model, images=frames)
            current_version = _find_latest_version(cfg)
        except Exception as e:
            print(f"  Failed to parse critique EDL: {e}")
            break

    return edl


def apply_feedback(cfg: Config, feedback: str) -> EDL:
    """Apply human feedback to the current EDL and re-render."""
    edl, _ = _load_latest_edl(cfg)
    print(f"Applying feedback: {feedback[:80]}...")
    prompt = FEEDBACK_PROMPT.format(edl_json=edl.model_dump_json(indent=2), feedback=feedback)
    return _revise_and_render(cfg, edl, prompt)


def generate_variations(cfg: Config, styles: list[str] | None = None) -> list[Path]:
    """Generate multiple vlog variations with different styles."""
    styles = styles or ["energetic", "reflective", "cinematic"]

    original_edl, _ = _load_latest_edl(cfg)

    analysis = json.loads((cfg.workspace / "analysis.json").read_text())
    media_summary = json.dumps(
        [{"local_path": a["local_path"], "media_type": a["media_type"],
          "duration_sec": round(a["duration_ms"]/1000, 1) if a.get("duration_ms") else None,
          "description": a.get("vision", {}).get("description"),
          "happiness": a.get("vision", {}).get("happiness_score")}
         for a in analysis],
        indent=2,
    )

    outputs = []
    for variation_style in styles:
        print(f"Generating {variation_style} variation...")
        prompt = VARIATION_PROMPT.format(
            edl_json=original_edl.model_dump_json(indent=2),
            media_summary=media_summary,
            variation_style=variation_style,
        )
        try:
            _revise_and_render(cfg, original_edl, prompt)
            video = max(
                (cfg.workspace / "output").glob("vlog_v*.mp4"),
                key=lambda p: p.stat().st_mtime,
            )
            outputs.append(video)
            print(f"  → {video}")
        except Exception as e:
            print(f"  Failed: {e}")

    return outputs
