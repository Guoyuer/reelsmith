"""Stage 5: Self-critique, variations, and human-feedback iteration."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import anthropic

from .assemble import assemble, _probe_duration
from .config import Config
from .edl import EDL

CRITIQUE_PROMPT = """\
You are reviewing a vlog you edited. I'll show you the current EDL and
evenly-spaced frames from the rendered video. Critique the vlog and suggest
improvements. Consider:

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


def self_critique(cfg: Config, *, style: str = "upbeat", max_rounds: int = 2) -> EDL:
    """Extract frames from the rendered vlog, critique, and regenerate EDL."""
    edl_path = cfg.workspace / "edl.json"
    edl = EDL.model_validate_json(edl_path.read_text())

    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
    current_version = _find_latest_version(cfg)

    for round_num in range(1, max_rounds + 1):
        print(f"Self-critique round {round_num}/{max_rounds}...")

        current_video = cfg.workspace / "output" / f"vlog_v{current_version}.mp4"
        if not current_video.exists():
            print(f"  No video at {current_video}, assembling first...")
            assemble(cfg, version=current_version)

        # Extract review frames
        frames = _extract_review_frames(current_video, cfg.workspace / "review_frames", count=8)

        # Build multimodal critique message
        content: list[dict] = [
            {"type": "text", "text": CRITIQUE_PROMPT.format(
                style=style,
                edl_json=edl.model_dump_json(indent=2),
            )}
        ]
        for frame in frames:
            import base64
            img_b64 = base64.b64encode(frame.read_bytes()).decode()
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64},
            })

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=8192,
            messages=[{"role": "user", "content": content}],
        )

        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]

        try:
            edl = EDL.model_validate_json(text)
        except Exception as e:
            print(f"  Failed to parse critique EDL: {e}")
            break

        # Save and render new version
        current_version += 1
        edl_path.write_text(edl.model_dump_json(indent=2))
        _save_edl_version(cfg, edl, current_version)
        assemble(cfg, version=current_version)
        print(f"  Rendered v{current_version}")

    return edl


def apply_feedback(cfg: Config, feedback: str) -> EDL:
    """Apply human feedback to the current EDL and re-render."""
    edl_path = cfg.workspace / "edl.json"
    edl = EDL.model_validate_json(edl_path.read_text())

    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)

    print(f"Applying feedback: {feedback[:80]}...")
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8192,
        messages=[{
            "role": "user",
            "content": FEEDBACK_PROMPT.format(
                edl_json=edl.model_dump_json(indent=2),
                feedback=feedback,
            ),
        }],
    )

    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]

    edl = EDL.model_validate_json(text)

    version = _find_latest_version(cfg) + 1
    edl_path.write_text(edl.model_dump_json(indent=2))
    _save_edl_version(cfg, edl, version)

    # Clear old clips to force re-render
    clips_dir = cfg.workspace / "clips"
    if clips_dir.exists():
        for f in clips_dir.iterdir():
            f.unlink(missing_ok=True)

    assemble(cfg, version=version)
    print(f"Rendered v{version} with feedback applied")
    return edl


def generate_variations(
    cfg: Config,
    styles: list[str] | None = None,
) -> list[Path]:
    """Generate multiple vlog variations with different styles."""
    if styles is None:
        styles = ["energetic", "reflective", "cinematic"]

    edl_path = cfg.workspace / "edl.json"
    edl = EDL.model_validate_json(edl_path.read_text())

    analysis_path = cfg.workspace / "analysis.json"
    analysis = json.loads(analysis_path.read_text())
    media_summary = json.dumps(
        [{"local_path": a["local_path"], "media_type": a["media_type"],
          "duration_sec": round(a["duration_ms"]/1000, 1) if a.get("duration_ms") else None,
          "description": a.get("vision", {}).get("description"),
          "happiness": a.get("vision", {}).get("happiness_score")}
         for a in analysis],
        indent=2,
    )

    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
    base_version = _find_latest_version(cfg)
    outputs = []

    for i, variation_style in enumerate(styles, 1):
        print(f"Generating {variation_style} variation...")

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=8192,
            messages=[{
                "role": "user",
                "content": VARIATION_PROMPT.format(
                    edl_json=edl.model_dump_json(indent=2),
                    media_summary=media_summary,
                    variation_style=variation_style,
                ),
            }],
        )

        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]

        try:
            var_edl = EDL.model_validate_json(text)
            version = base_version + i
            _save_edl_version(cfg, var_edl, version)

            # Write as current EDL temporarily for assembly
            edl_path.write_text(var_edl.model_dump_json(indent=2))

            # Clear clips for fresh render
            clips_dir = cfg.workspace / "clips"
            if clips_dir.exists():
                for f in clips_dir.iterdir():
                    f.unlink(missing_ok=True)

            output = assemble(cfg, version=version)
            outputs.append(output)
            print(f"  → {output}")
        except Exception as e:
            print(f"  Failed: {e}")

    # Restore original EDL
    edl_path.write_text(edl.model_dump_json(indent=2))
    return outputs


def _extract_review_frames(video: Path, out_dir: Path, count: int = 8) -> list[Path]:
    """Extract evenly-spaced frames from the rendered vlog for review."""
    out_dir.mkdir(parents=True, exist_ok=True)
    # Clear old frames
    for f in out_dir.glob("frame_*.jpg"):
        f.unlink()

    duration = _probe_duration(video)
    interval = duration / (count + 1)

    for i in range(1, count + 1):
        t = interval * i
        out_path = out_dir / f"frame_{i:02d}.jpg"
        subprocess.run(
            [
                "ffmpeg", "-y", "-ss", str(t), "-i", str(video),
                "-frames:v", "1", "-q:v", "3",
                str(out_path),
            ],
            capture_output=True,
        )

    return sorted(out_dir.glob("frame_*.jpg"))


def _find_latest_version(cfg: Config) -> int:
    """Find the latest vlog version number in the output directory."""
    output_dir = cfg.workspace / "output"
    if not output_dir.exists():
        return 0
    versions = []
    for f in output_dir.glob("vlog_v*.mp4"):
        try:
            v = int(f.stem.split("_v")[1])
            versions.append(v)
        except (IndexError, ValueError):
            pass
    return max(versions) if versions else 0


def _save_edl_version(cfg: Config, edl: EDL, version: int) -> None:
    """Save a versioned copy of the EDL."""
    edl_dir = cfg.workspace / "edl_history"
    edl_dir.mkdir(parents=True, exist_ok=True)
    path = edl_dir / f"edl_v{version}.json"
    path.write_text(edl.model_dump_json(indent=2))
