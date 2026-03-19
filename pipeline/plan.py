"""Stage 3: Generate EDL — select photos/videos and arrange into a narrative.

Uses the visual planner (Gemini 3 Flash): Gemini sees actual photos via
contact sheets + filmstrips and creates a cinematic edit decision list.

Requires GEMINI_API_KEY in .env.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass  # HEIC support unavailable; handled by convert_heic fallback chain

from .config import Config
from .edl import EDL, MusicTrack


# ---------------------------------------------------------------------------
# Trip-type configuration
# ---------------------------------------------------------------------------

TRIP_TYPES = {
    "family", "solo", "food", "adventure", "architecture", "general",
}

DEFAULT_FOCUS = {
    "family": "happiness with family",
    "solo": "personal journey and discovery",
    "food": "culinary experiences and flavors",
    "adventure": "action, awe, and exploration",
    "architecture": "design, structures, and spaces",
    "general": "highlights and memorable moments",
}


def _default_focus(trip_type: str) -> str:
    return DEFAULT_FOCUS.get(trip_type, DEFAULT_FOCUS["general"])


# ---------------------------------------------------------------------------
# Narrative guidance per trip type
# ---------------------------------------------------------------------------

_NARRATIVE_GUIDANCE = {
    "family": """\
2. **Family is the heart**: At least 30-40% of items MUST show family members
   (people with names in metadata, especially tier A/B items). Look for genuine
   laughter, hugs, play, shared meals — these are the emotional core of a family
   vlog. Scenic B-roll is important for atmosphere, but a family vlog with no
   family close-ups feels empty. Balance: every segment should have at least one
   family shot.""",
    "solo": """\
2. **Personal journey**: This is one person's story. Place over people — favor
   grand landscapes, intimate details, and moments of solitary wonder. Tier C
   (scenery) items are your stars; use quality scores to find the most striking.""",
    "food": """\
2. **Culinary narrative**: Food is the thread. Prioritize close-ups of dishes,
   restaurant ambiance, market stalls, and meal moments. Scene types "food" and
   "meal" are high value. Build appetite through visual variety.""",
    "adventure": """\
2. **Action and awe**: Dramatic pacing — movement, discovery, and scale.
   Favor high-emotion items, activity scenes, and nature. Build tension with
   establishing shots, release with the payoff moment.""",
    "architecture": """\
2. **Design and space**: Buildings, structures, and spatial beauty are the focus.
   Tier C items with scene_type "landmark" or "building" are the core.
   Visual quality matters most — favor striking compositions.""",
    "general": """\
2. **Balanced storytelling**: Mix people, places, and moments. No single element
   dominates — let the best items rise regardless of type. Variety and visual
   quality guide selection.""",
}


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_date_range(dates: list[str]) -> str:
    """Format a list of YYYY-MM-DD dates as 'June 13-16, 2025'."""
    try:
        dts = [datetime.strptime(d, "%Y-%m-%d") for d in dates]
        first, last = min(dts), max(dts)
        if first.month == last.month:
            return f"{first.strftime('%B')} {first.day}-{last.day}, {first.year}"
        return f"{first.strftime('%B %-d')} - {last.strftime('%B %-d')}, {first.year}"
    except (ValueError, TypeError):
        return ""


def _format_time_gap(seconds: int) -> str:
    """Format seconds as a human-readable gap string."""
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _format_item_line(a: dict, tier_prefix: str, prev_time: int | None = None) -> str:
    """Format a single analysis item as a prompt line with metadata."""
    v = a["vision"]
    desc = v.get("description", "")
    media = a.get("media_type", "photo")
    cluster = a.get("cluster_size", 1)
    taken = a.get("takentime", 0)

    line = tier_prefix
    if media == "video":
        dur_ms = a.get("duration_ms")
        if dur_ms:
            line += f" (video {dur_ms / 1000:.0f}s)"
        else:
            line += " (video)"
    if cluster > 1:
        line += f" (best of {cluster})"
    if prev_time and taken and taken > prev_time:
        gap = taken - prev_time
        line += f" [+{_format_time_gap(gap)}]"
    line += f" | {desc}"

    loc_detail = _location_detail(a)
    if loc_detail:
        line += f"\n      location: {loc_detail}"
    line += f"\n      path: {a['local_path']}"

    # Include time for Claude to reason about chronology
    taken_iso = a.get("taken_iso", "")
    if taken_iso:
        line += f"\n      time: {taken_iso}"

    # Person names for narrative context
    persons = a.get("persons", [])
    if persons:
        line += f"\n      people: {', '.join(persons)}"

    # Video scenes — show available moments for trim point selection
    scenes = a.get("scenes", [])
    if scenes and media == "video":
        scene_lines = []
        for s in scenes:
            sv = s.get("vision", {})
            s_desc = sv.get("description", "")[:120] if sv else ""
            motion = s.get("motion", "?")
            s_qual = sv.get("visual_quality", "?") if sv else "?"
            scene_lines.append(
                f"        scene {s['scene_index']}: {s['start']:.1f}-{s['end']:.1f}s "
                f"({s['duration']:.1f}s) motion={motion} qual={s_qual}"
                + (f" | {s_desc}" if s_desc else "")
            )
        line += "\n      scenes:\n" + "\n".join(scene_lines)

    return line


# ---------------------------------------------------------------------------
# Gemini API call helper
# ---------------------------------------------------------------------------

def _gemini_call(
    system: str,
    user_parts: list,
    log_fn,
    label: str = "",
    model: str = "gemini-3-flash",
) -> str:
    """Make a Gemini API call with multimodal content. Returns response text.

    *user_parts*: list of strings and/or Part objects (text + images).
    """
    import os

    from google import genai
    from google.genai import types

    _log = log_fn or print

    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY not set. Add it to .env to use the visual/API planner. "
            "Get a key at https://ai.google.dev/gemini-api/docs/api-key"
        )
    client = genai.Client(api_key=api_key)

    # Build content parts and count them for logging
    parts = []
    n_text = 0
    n_media = 0
    text_chars = 0
    media_bytes_total = 0
    for p in user_parts:
        if isinstance(p, str):
            parts.append(types.Part(text=p))
            n_text += 1
            text_chars += len(p)
        elif isinstance(p, dict) and p.get("type") in ("image_bytes", "audio_bytes"):
            parts.append(types.Part(
                inline_data=types.Blob(
                    mime_type=p.get("mime_type", "image/jpeg"),
                    data=p["data"],
                ),
            ))
            n_media += 1
            media_bytes_total += len(p["data"])
        elif isinstance(p, types.Part):
            parts.append(p)

    _log(f"=== Gemini API Call: {label} ===")
    _log(f"  Model: {model}")
    _log(f"  Input: {n_text} text parts ({text_chars} chars), {n_media} media ({media_bytes_total // 1024}KB)")
    _log(f"  System prompt: {len(system)} chars")

    import time as _time
    t0 = _time.monotonic()

    response = client.models.generate_content(
        model=model,
        contents=[types.Content(parts=parts)],
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
    # Log first 300 chars of response for debugging
    preview = content[:300].replace("\n", " ")
    _log(f"  Preview: {preview}...")
    _log(f"=== End {label} ===")

    return content


# ---------------------------------------------------------------------------
# Visual planner system prompt
# ---------------------------------------------------------------------------

def _visual_system_prompt(trip_type: str) -> str:
    """System prompt for visual planner — Claude sees contact sheets and filmstrips."""
    guidance = _NARRATIVE_GUIDANCE.get(trip_type, _NARRATIVE_GUIDANCE["general"])
    return f"""\
You are a professional travel vlog editor with full creative control. You will
see the actual photos and video filmstrips from a trip, organized as numbered
contact sheets by day/location.

Your job: select the best items, create your OWN chapter structure (ignore the
input groupings — they are just organizational), and arrange everything into an
EDL (Edit Decision List) that tells a compelling story.

You have complete autonomy over:
- Which items to include (ignore tier labels — judge quality with your own eyes)
- How to group items into segments (create narrative chapters, not location buckets)
- Pacing, duration, effects, transitions
- Text overlay content (write evocative titles, not just location names)

## How to read the input

- **Contact sheets**: Grid images with numbered cells (#01, #02, ...). Numbers match
  the text metadata below each sheet. Judge the VISUAL content — composition, emotion,
  lighting, quality — not just the metadata.
- **Video filmstrips**: Horizontal strips showing scene keyframes with timestamps.
  Select the best scene using start_time/end_time.
- **Metadata per item**: tier (A=family together, B=one family member, C=scenery),
  person names, location, time.

## Narrative principles

1. **Emotional arc**: Build from curiosity → joy → warmth → nostalgia.

{guidance}

3. **Video-first**: Prefer video clips over photos when both cover the same moment.
   Videos bring motion, atmosphere, and sound — they make a vlog feel alive, not like
   a slideshow. Aim for 40-60% video content by screen time.

4. **Rhythm**: Alternate photos (3-5s, Ken Burns) with video clips (5-10s, real motion).
   Vary pacing — fast cuts for energy, lingering shots for emotion.

5. **Visual judgment**: Use what you SEE in the photos. A photo with genuine laughter
   beats a posed shot with higher "tier" score. Trust your eyes over metadata.

6. **Text overlays**: Day labels and location names at natural transitions. Keep minimal.

7. **Music mood**: For each segment, suggest a music_mood — a natural language description
   of the background music tone (e.g., "warm acoustic guitar, uplifting",
   "gentle piano, reflective and slow", "upbeat tropical percussion").

## Technical rules

- display_duration: 3-5s per photo, 5-10s for video clips
- For videos: set start_time and end_time to select the best scene
- effect: ken_burns_in/out/left/right for photos, "none" for video clips
- Transitions: crossfade within segments, fade_black between major changes
- CRITICAL: source_file must be the EXACT path value from the text metadata

Output valid JSON only:
{{
  "title": "string",
  "target_duration": <seconds>,
  "resolution": [3840, 2160],
  "fps": 60,
  "segments": [
    {{
      "name": "Chapter Name",
      "narrative_rationale": "Why these items, what story beat this serves",
      "music_mood": "natural language music description for this segment",
      "items": [
        {{
          "source_file": "<exact path from metadata>",
          "media_type": "photo|video",
          "display_duration": 3.0-10.0,
          "start_time": null or <seconds for video trim start>,
          "end_time": null or <seconds for video trim end>,
          "effect": "ken_burns_in|ken_burns_out|ken_burns_left|ken_burns_right|static|none",
          "text_overlay": null or {{"text": "string", "position": "bottom", "font_size": 48}}
        }}
      ],
      "transition": "crossfade|fade_black",
      "transition_duration": 0.8
    }}
  ],
  "music": null
}}"""


# ---------------------------------------------------------------------------
# Visual planner content builders
# ---------------------------------------------------------------------------

def _build_visual_chapter_text(
    chapter: dict, day: dict, analysis_by_id: dict, start_idx: int,
) -> tuple[str, list[Path], list[dict]]:
    """Build text metadata for a chapter and collect image paths.

    Returns (text, photo_paths, video_items) where:
    - text: metadata lines with numbered items
    - photo_paths: ordered list of photo paths for contact sheet
    - video_items: list of video analysis dicts for filmstrips
    """
    lines = []
    photo_paths = []
    video_items = []
    idx = start_idx

    for item_id in chapter.get("item_ids", []):
        a = analysis_by_id.get(item_id)
        if not a:
            continue

        local_path = a.get("local_path", "")
        media = a.get("media_type", "photo")
        tier = a.get("tier", "?")
        persons = a.get("persons", [])
        taken_iso = a.get("taken_iso", "")
        time_str = taken_iso[11:16] if taken_iso and len(taken_iso) >= 16 else ""

        label = f"#{idx:02d}"
        # Describe who's in the photo instead of abstract tiers
        if a.get("family_count", 0) >= 2:
            who = f"family together ({','.join(persons[:3])})" if persons else "family together"
        elif a.get("family_count", 0) == 1:
            who = f"{persons[0]}" if persons else "one family member"
        elif persons:
            who = f"people: {','.join(persons[:3])}"
        else:
            who = "scenery/no people"
        parts = [f"{label}: {who}"]
        if time_str:
            parts.append(f"time={time_str}")

        if media == "video":
            dur_ms = a.get("duration_ms")
            dur_s = f"{dur_ms / 1000:.0f}s" if dur_ms else "?"
            n_scenes = len(a.get("scenes", []))
            parts.append(f"video={dur_s} scenes={n_scenes}")
            video_items.append(a)
        else:
            photo_paths.append(Path(local_path))

        parts.append(f"path={local_path}")
        lines.append(" ".join(parts))
        idx += 1

    loc = chapter.get("location", "unknown")
    block = chapter.get("time_block", "")
    # Present as context, not prescriptive chapters — Gemini creates its own narrative structure
    n_photos = len(photo_paths)
    n_vids = len(video_items)
    media_note = f"{n_photos} photos" + (f", {n_vids} videos" if n_vids else "")
    header = f"\n--- {day['day_name']} {day['date']}, {block} near {loc} ({media_note}) ---"
    text = header + "\n" + "\n".join(lines)
    return text, photo_paths, video_items


def _build_visual_content_blocks(
    preprocessed: dict, analysis_by_id: dict, cfg: Config, log_fn=None,
) -> list:
    """Build multimodal parts: interleaved text + contact sheets + filmstrips.

    Returns list of str and image dicts suitable for _gemini_call().
    """
    from .media_utils import make_contact_sheet, make_filmstrip

    _log = log_fn or print
    blocks: list = []
    sheets_dir = cfg.workspace / "contact_sheets"
    sheets_dir.mkdir(parents=True, exist_ok=True)

    global_idx = 1  # continuous numbering across chapters

    for day in preprocessed["timeline"]:
        for chapter in day["chapters"]:
            text, photo_paths, video_items = _build_visual_chapter_text(
                chapter, day, analysis_by_id, global_idx,
            )
            n_items = len(photo_paths) + len(video_items)
            if n_items == 0:
                continue

            blocks.append(text)

            # Contact sheet for photos
            if photo_paths:
                # Use thumbnail paths if available, otherwise original
                thumb_paths = []
                for p in photo_paths:
                    thumb = cfg.workspace / "thumbnails" / f"{p.stem}_thumb.jpg"
                    thumb_paths.append(thumb if thumb.exists() else p)

                loc_safe = chapter.get("location", "x").replace("/", "_")[:30]
                sheet_name = f"{day['date']}_{chapter.get('time_block', 'x')}_{loc_safe}.jpg"
                sheet_path = sheets_dir / sheet_name

                # Split large chapters into multiple sheets (max 2000px height limit)
                max_per_sheet = 28  # 7 rows x 4 cols = 1792px height, under 2000px
                sheet_idx = 0
                for chunk_start in range(0, len(thumb_paths), max_per_sheet):
                    chunk = thumb_paths[chunk_start:chunk_start + max_per_sheet]
                    chunk_labels = [f"#{global_idx + chunk_start + i:02d}" for i in range(len(chunk))]
                    s_path = sheets_dir / f"{sheet_name.replace('.jpg', '')}_{sheet_idx}.jpg" if len(thumb_paths) > max_per_sheet else sheet_path
                    make_contact_sheet(chunk, s_path, cell_size=256, columns=4, labels=chunk_labels)
                    _log(f"Contact sheet: {s_path.name} ({len(chunk)} photos)")

                    blocks.append({
                        "type": "image_bytes",
                        "mime_type": "image/jpeg",
                        "data": s_path.read_bytes(),
                    })
                    sheet_idx += 1

            # Filmstrips for videos
            for vi in video_items:
                scenes = vi.get("scenes", [])
                kf_paths = [Path(s["keyframe"]) for s in scenes if s.get("keyframe")]
                if not kf_paths:
                    continue

                vid_id = vi["id"]
                strip_path = sheets_dir / f"filmstrip_{vid_id}.jpg"
                time_labels = [f"{s['start']:.0f}-{s['end']:.0f}s" for s in scenes if s.get("keyframe")]
                # Limit to 5 scene keyframes to keep filmstrip under 2000px width
                kf_paths = kf_paths[:5]
                time_labels = time_labels[:5]
                make_filmstrip(kf_paths, strip_path, cell_height=256, labels=time_labels)

                blocks.append(f"Video filmstrip for #{global_idx + len(photo_paths) + video_items.index(vi):02d}:")
                blocks.append({
                    "type": "image_bytes",
                    "mime_type": "image/jpeg",
                    "data": strip_path.read_bytes(),
                })

            global_idx += n_items

    return blocks


def _build_review_blocks(edl: EDL, cfg: Config) -> list:
    """Build review parts: selected items at higher resolution for Gemini."""
    from .media_utils import generate_thumbnail

    blocks: list = [
        f"Review this EDL. Current version:\n{edl.model_dump_json(indent=2)}",
    ]
    review_dir = cfg.workspace / "review_thumbs"
    review_dir.mkdir(parents=True, exist_ok=True)

    for seg in edl.segments:
        blocks.append(f"\n--- {seg.name} ({len(seg.items)} items) ---")
        for item in seg.items:
            src = Path(item.source_file)
            if item.media_type == "video":
                kf_dir = cfg.workspace.parent.parent / "keyframes" if cfg.workspace.parent.name == "runs" else cfg.workspace / "keyframes"
                kf_pattern = f"{src.stem}_scene_*.jpg"
                kfs = sorted(kf_dir.glob(kf_pattern)) if kf_dir.exists() else []
                if kfs:
                    thumb = generate_thumbnail(kfs[0], review_dir, size=768)
                else:
                    continue
            else:
                thumb = generate_thumbnail(src, review_dir, size=768)

            if thumb.exists():
                blocks.append(f"{item.source_file} ({item.display_duration}s)")
                blocks.append({
                    "type": "image_bytes",
                    "mime_type": "image/jpeg",
                    "data": thumb.read_bytes(),
                })

    return blocks


# ---------------------------------------------------------------------------
# Audio assessment — Gemini listens to selected video clips
# ---------------------------------------------------------------------------

def _assess_audio(edl: EDL, log_fn) -> None:
    """Extract audio from selected video clips and ask Gemini which have meaningful speech.

    Modifies edl items in-place, setting keep_audio=True on clips worth preserving.
    Only processes video items with trim points (~20 clips). One Gemini API call.
    """
    video_items = [
        (seg_idx, item_idx, item)
        for seg_idx, seg in enumerate(edl.segments)
        for item_idx, item in enumerate(seg.items)
        if item.media_type == "video"
    ]
    if not video_items:
        log_fn("Audio assessment: no video clips, skipping")
        return

    log_fn(f"=== AUDIO ASSESSMENT: {len(video_items)} video clips ===")

    from .media_utils import run_subprocess
    import tempfile

    # Extract audio from each video clip (trimmed to start_time/end_time)
    audio_parts: list[dict] = []  # {"index": i, "seg_idx": ..., "item_idx": ..., "data": bytes}
    temp_dir = Path(tempfile.mkdtemp())

    for i, (seg_idx, item_idx, item) in enumerate(video_items):
        source = Path(item.source_file)
        if not source.exists():
            continue

        audio_path = temp_dir / f"clip_{i}.wav"
        cmd = ["ffmpeg", "-y"]
        if item.start_time is not None:
            cmd += ["-ss", str(item.start_time)]
        cmd += ["-i", str(source)]
        if item.end_time is not None and item.start_time is not None:
            cmd += ["-t", str(item.end_time - item.start_time)]
        cmd += ["-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", str(audio_path)]
        run_subprocess(cmd, capture_output=True)

        if audio_path.exists() and audio_path.stat().st_size > 1000:
            audio_parts.append({
                "index": i,
                "seg_idx": seg_idx,
                "item_idx": item_idx,
                "filename": source.name,
                "data": audio_path.read_bytes(),
            })

    if not audio_parts:
        log_fn("Audio assessment: no audio extracted, skipping")
        return

    log_fn(f"Extracted audio from {len(audio_parts)} clips, sending to Gemini...")

    # Build multimodal content: audio clips + prompt
    content_parts: list = []
    for ap in audio_parts:
        content_parts.append(f"Clip {ap['index']} ({ap['filename']}):")
        content_parts.append({
            "type": "audio_bytes",
            "mime_type": "audio/wav",
            "data": ap["data"],
        })

    system = """\
You are assessing audio from video clips selected for a family travel vlog.
For each clip, listen and determine if it contains meaningful speech worth
preserving in the final video — family conversations, reactions ("wow!",
laughter), a child's voice, narration, or any emotionally valuable audio.

Ambient noise (wind, traffic, crowd murmur) without clear speech = NOT worth keeping.

Respond with JSON only:
{
  "clips": [
    {"index": <clip number>, "keep_audio": true/false, "reason": "brief reason", "transcript": "exact words spoken, or empty string if no speech"}
  ]
}"""

    try:
        response = _gemini_call(system, content_parts, log_fn,
                                label="audio assessment", model="gemini-3-flash-preview")
        from .media_utils import strip_markdown_fences
        data = json.loads(strip_markdown_fences(response))

        kept = 0
        for clip_info in data.get("clips", []):
            idx = clip_info.get("index")
            if clip_info.get("keep_audio") and idx is not None:
                for ap in audio_parts:
                    if ap["index"] == idx:
                        item = edl.segments[ap["seg_idx"]].items[ap["item_idx"]]
                        item.keep_audio = True
                        transcript = clip_info.get("transcript", "")
                        if transcript:
                            item.transcript = transcript
                        log_fn(f"  Clip {idx} ({ap['filename']}): KEEP — {clip_info.get('reason', '')}")
                        if transcript:
                            log_fn(f"    Speech: \"{transcript[:100]}\"")
                        kept += 1
                        break

        log_fn(f"Audio assessment: {kept}/{len(audio_parts)} clips will keep audio")

    except Exception as e:
        log_fn(f"Audio assessment failed ({e}), continuing without audio preservation")

    # Cleanup temp files
    import shutil
    shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Visual planner — multi-pass Gemini planning with visual input
# ---------------------------------------------------------------------------

def _plan_visual(
    cfg: Config, preprocessed: dict, analysis_by_id: dict,
    analysis_items: list[dict],
    style: str, target_duration: int, focus: str,
    trip_type: str = "family", log_fn=None,
) -> EDL:
    """Multi-pass Gemini planning with visual input — sees actual photos.

    Uses Gemini Pro for text-only passes, Gemini Flash for token-heavy visual pass.
    """
    _log = log_fn or print

    # Trip-level summary
    days = preprocessed.get("timeline", [])
    locations: list[str] = []
    n_candidates = 0
    n_videos = 0
    for day in days:
        for ch in day.get("chapters", []):
            loc = ch.get("location", "")
            if loc and loc != "unknown" and loc not in locations:
                locations.append(loc)
            for item_id in ch.get("item_ids", []):
                a = analysis_by_id.get(item_id)
                if a:
                    n_candidates += 1
                    if a.get("media_type") == "video":
                        n_videos += 1

    trip_summary = (
        f"Trip overview: {len(days)} day{'s' if len(days) != 1 else ''}, "
        f"{len(locations)} locations, {n_candidates} candidates "
        f"({n_videos} videos, {n_candidates - n_videos} photos)."
    )

    n_items = target_duration // 4
    trip_label = f"{trip_type} trip" if trip_type != "general" else "trip"
    family_line = ""
    if trip_type == "family" and preprocessed.get("family_names"):
        family_line = f"\nFamily: {', '.join(preprocessed['family_names'])}"

    # ------------------------------------------------------------------
    # Pass 1: Narrative arc (text-only, lightweight)
    # ------------------------------------------------------------------
    _log("=== VISUAL PASS 1: Narrative Arc ===")

    arc_lines = []
    for day in preprocessed["timeline"]:
        arc_lines.append(f"\n=== {day['day_name']} {day['date']} ===")
        for ch in day["chapters"]:
            loc = ch["location"]
            block = ch["time_block"]
            count = len(ch.get("item_ids", []))
            n_vid = sum(1 for iid in ch["item_ids"]
                        if analysis_by_id.get(iid, {}).get("media_type") == "video")
            line = f"  [{block.upper()}] {loc} — {count} items"
            if n_vid:
                line += f" ({n_vid} videos)"
            arc_lines.append(line)

    arc_system = f"""\
You are a professional travel vlog narrative designer with full creative control.
Design the emotional arc and chapter structure for a {style} highlight reel.

IMPORTANT: Create narrative chapters based on STORY BEATS, not locations or times.
For example, "Discovery & Wonder" is a chapter theme, not "Morning at Marina Bay".
Group moments by emotion and narrative purpose, not by when/where they happened.

Output JSON only:
{{
  "title": "vlog title",
  "arc_description": "1-2 sentences describing the overall narrative arc",
  "chapters": [
    {{
      "name": "Chapter Name (narrative, not location)",
      "theme": "emotional theme",
      "target_items": <count>,
      "pacing": "fast|medium|slow",
      "prefer_video": <boolean, default true — video clips bring motion and atmosphere>,
      "music_mood": "describe the ideal background music for this chapter"
    }}
  ]
}}"""

    arc_user = f"""\
Design the narrative arc for a {style} {trip_label} vlog.

{trip_summary}{family_line}
Target: {target_duration}s (~{n_items} items).
Focus: {focus}.

Trip structure:
{"".join(arc_lines)}"""

    arc_content = _gemini_call(arc_system, [arc_user], _log,
                               label="visual pass 1: arc", model="gemini-3-flash-preview")
    _log(f"Arc response:\n{arc_content[:500]}")

    from .media_utils import strip_markdown_fences
    arc_content = strip_markdown_fences(arc_content)
    try:
        narrative_arc = json.loads(arc_content)
    except json.JSONDecodeError:
        _log("Failed to parse arc, continuing without it")
        narrative_arc = None

    # ------------------------------------------------------------------
    # Pass 2: Visual selection — Claude sees contact sheets + filmstrips
    # ------------------------------------------------------------------
    _log("=== VISUAL PASS 2: Visual Selection ===")

    _log("Building contact sheets and filmstrips from cached thumbnails...")
    content_blocks = _build_visual_content_blocks(preprocessed, analysis_by_id, cfg, _log)
    n_img_blocks = sum(1 for b in content_blocks if isinstance(b, dict) and b.get("type") == "image_bytes")
    n_text_blocks = sum(1 for b in content_blocks if isinstance(b, str))
    _log(f"Visual content: {n_text_blocks} text blocks, {n_img_blocks} images (contact sheets + filmstrips)")

    arc_guidance = ""
    if narrative_arc:
        arc_guidance = f"\n**Narrative arc** (follow this structure):\nTitle: {narrative_arc.get('title', '')}\nArc: {narrative_arc.get('arc_description', '')}\nChapters:\n"
        for ch in narrative_arc.get("chapters", []):
            arc_guidance += (
                f"  - {ch.get('name', '?')}: {ch.get('theme', '?')}, "
                f"~{ch.get('target_items', '?')} items, pacing={ch.get('pacing', '?')}"
                f"{', prefer video' if ch.get('prefer_video') else ''}\n"
            )

    intro_text = f"""\
Create a {style} {trip_label} vlog EDL from the photos and videos shown below.

{trip_summary}{family_line}
Target: {target_duration}s (~{n_items} items). Focus: {focus}.
{arc_guidance}
Look at each contact sheet carefully. Select the best photos and video scenes.
For videos, specify start_time/end_time to pick the best scene.
Use the exact path values from the metadata as source_file.

Candidates by day/location:"""

    # Prepend intro text before the content blocks
    visual_parts: list = [intro_text] + content_blocks

    system_prompt = _visual_system_prompt(trip_type)
    _log(f"Visual message: {len(visual_parts)} parts")

    edl_content = _gemini_call(system_prompt, visual_parts, _log,
                               label="visual pass 2: select", model="gemini-3-flash-preview")

    _log(f"=== VISUAL EDL RESPONSE ({len(edl_content)} chars) ===")
    _log(edl_content[:1000])
    _log("=== END ===")

    edl_content = strip_markdown_fences(edl_content)
    edl = EDL.model_validate_json(edl_content)
    n_vid = sum(1 for i in edl.all_items() if i.media_type == "video")
    n_photo = sum(1 for i in edl.all_items() if i.media_type == "photo")
    _log(f"Parsed EDL: {len(edl.segments)} segments, {n_photo} photos + {n_vid} videos = "
         f"{len(edl.all_items())} items, ~{edl.estimated_duration():.0f}s")
    for seg in edl.segments:
        _log(f"  [{seg.name}] ({len(seg.items)} items)")
        _log(f"    Music: {seg.music_mood}")
        _log(f"    Rationale: {seg.narrative_rationale[:150]}")
        for item in seg.items:
            trim = f" trim={item.start_time:.0f}-{item.end_time:.0f}s" if item.start_time is not None else ""
            _log(f"    - {item.media_type:5s} {item.display_duration}s {Path(item.source_file).name}{trim}")

    # ------------------------------------------------------------------
    # Pass 2.5: Audio assessment — Gemini listens to selected video clips
    # ------------------------------------------------------------------
    _assess_audio(edl, _log)

    # ------------------------------------------------------------------
    # Pass 3: Visual review — Gemini reviews selected items at higher res
    # ------------------------------------------------------------------
    _log("=== VISUAL PASS 3: Visual Review ===")

    review_system = """\
You are reviewing a vlog EDL you just created. You can see each selected photo/video
at higher resolution. Check:
1. Does the sequence flow visually? Adjacent shots shouldn't look too similar.
2. Video/photo balance — enough video clips for energy? Not all slideshows?
3. Pacing — duration varies enough? Emotional beats get more time?
4. Are the music_mood values specific and evocative (not generic)?
5. Do text overlays appear at natural transitions?
6. Video trim points — are start_time/end_time selecting the best moment?

Output JSON with TWO top-level fields:
{
  "review_notes": "What you changed and why — be specific (e.g., 'removed #03 because too similar to #02, swapped order of segments 2 and 3 for better arc')",
  "edl": { ... the improved EDL (same schema as input) ... }
}"""

    review_parts = _build_review_blocks(edl, cfg)
    review_content = _gemini_call(review_system, review_parts, _log,
                                   label="visual pass 3: review", model="gemini-3-flash-preview")

    review_content = strip_markdown_fences(review_content)
    try:
        review_data = json.loads(review_content)
        # Extract review notes and EDL from wrapper
        if "review_notes" in review_data and "edl" in review_data:
            review_notes = review_data["review_notes"]
            _log(f"=== Review Notes ===")
            _log(review_notes)
            _log(f"=== End Review Notes ===")
            reviewed = EDL.model_validate(review_data["edl"])
        else:
            # Fallback: Gemini output the EDL directly without wrapper
            _log("(No review_notes wrapper — Gemini output EDL directly)")
            reviewed = EDL.model_validate(review_data)

        n_vid = sum(1 for i in reviewed.all_items() if i.media_type == "video")
        n_photo = sum(1 for i in reviewed.all_items() if i.media_type == "photo")
        _log(f"Reviewed EDL: {len(reviewed.segments)} segments, {n_photo} photos + {n_vid} videos, "
             f"~{reviewed.estimated_duration():.0f}s")
        for seg in reviewed.segments:
            _log(f"  [{seg.name}] ({len(seg.items)} items)")
            _log(f"    Music: {seg.music_mood}")
            for item in seg.items:
                trim = f" trim={item.start_time:.0f}-{item.end_time:.0f}s" if item.start_time is not None else ""
                _log(f"    - {item.media_type:5s} {item.display_duration}s {Path(item.source_file).name}{trim}")
        return reviewed
    except Exception as e:
        _log(f"Review parse failed ({e}), using pass 2 EDL")
        return edl


# ---------------------------------------------------------------------------
# Prompt builder (used by visual planner arc pass for text descriptions)
# ---------------------------------------------------------------------------

def _build_chapters_prompt(preprocessed: dict, analysis_by_id: dict) -> str:
    """Build a structured text representation of the timeline with scores."""
    lines = []

    for day in preprocessed["timeline"]:
        lines.append(f"\n=== {day['day_name']} {day['date']} ===")

        for chapter in day["chapters"]:
            loc = chapter["location"]
            block = chapter["time_block"]

            ab_items = []
            c_items = []
            for item_id in chapter["item_ids"]:
                a = analysis_by_id.get(item_id)
                if not a or not a.get("vision"):
                    continue
                tier = a.get("tier", "?")
                if tier in ("A", "B", "?"):
                    ab_items.append(a)
                elif tier == "C":
                    c_items.append(a)

            if not ab_items and not c_items:
                continue

            # Sort all items by time for gap calculation
            all_chapter = ab_items + c_items
            all_chapter.sort(key=lambda x: x.get("takentime", 0))
            prev_time = None

            lines.append(f"\n  [{block.upper()}] {loc}")

            # Emit all items in chronological order so Claude sees natural flow
            all_sorted = sorted(all_chapter, key=lambda x: x.get("takentime", 0))
            for a in all_sorted:
                v = a["vision"]
                tier = a.get("tier", "?")
                issues = v.get("issues", "")

                if tier in ("A", "B", "?"):
                    tog = v.get("togetherness", v.get("happiness_score", "?"))
                    emo = v.get("genuine_emotion", "?")
                    beat = v.get("story_beat", v.get("scene_type", "?"))
                    qual = v.get("visual_quality", "?")
                    prefix = (
                        f"    [{tier}] fam={a.get('family_count',0)} "
                        f"tog={tog} emo={emo} qual={qual} beat={beat}"
                    )
                else:
                    scene = v.get("scene_type", "?")
                    qual = v.get("visual_quality", "?")
                    prefix = f"    [C] scene={scene} qual={qual}"

                if issues:
                    prefix += f" ISSUES={issues}"
                # Include rich description fields for Claude
                extra_parts = []
                if v.get("setting"):
                    extra_parts.append(f"setting={v['setting']}")
                if v.get("mood"):
                    extra_parts.append(f"mood={v['mood']}")
                if v.get("activity"):
                    extra_parts.append(f"activity={v['activity']}")
                if extra_parts:
                    prefix += " | " + ", ".join(extra_parts)
                lines.append(_format_item_line(a, prefix, prev_time))
                prev_time = a.get("takentime")

    return "\n".join(lines)


def _location_detail(a: dict) -> str:
    """Build location detail string from district/country fields."""
    parts = []
    if a.get("district"):
        parts.append(a["district"])
    if a.get("first_level"):
        parts.append(a["first_level"])
    if a.get("country"):
        parts.append(a["country"])
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def plan(
    cfg: Config,
    *,
    style: str = "upbeat",
    target_duration: int = 180,
    focus: str = "",
    trip_type: str = "family",
    music_file: str | None = None,
    log_fn=None,
) -> tuple[EDL, int]:
    """Generate an EDL from preprocessed + analysis data using the visual planner."""
    _log = log_fn or print
    if trip_type not in TRIP_TYPES:
        _log(f"Unknown trip_type '{trip_type}', falling back to 'general'")
        trip_type = "general"

    if not os.getenv("GEMINI_API_KEY", ""):
        raise RuntimeError(
            "GEMINI_API_KEY not set. Add it to .env for visual planning. "
            "Get a key at https://ai.google.dev/gemini-api/docs/api-key"
        )

    effective_focus = focus or _default_focus(trip_type)
    preprocessed = json.loads((cfg.workspace / "preprocessed.json").read_text())
    analysis_items = json.loads((cfg.workspace / "analysis.json").read_text())
    analysis_by_id: dict[int, dict] = {a["id"]: a for a in analysis_items}

    _log(f"Planning via Gemini with visual input (target {target_duration}s, style={style}, trip_type={trip_type})...")
    edl = _plan_visual(cfg, preprocessed, analysis_by_id, analysis_items,
                       style=style, target_duration=target_duration,
                       focus=effective_focus, trip_type=trip_type, log_fn=_log)

    # Post-process: force effect="none" on video items (Ken Burns fights native motion)
    for seg in edl.segments:
        for item in seg.items:
            if item.media_type == "video" and item.effect != "none":
                item.effect = "none"

    # Set metadata fields
    edl.trip_type = trip_type
    edl.style = style
    edl.intro_style = edl.intro_style or "title_card"
    edl.outro_style = edl.outro_style or "fade_title"
    if not edl.date_range:
        all_dates = sorted({d["date"] for d in preprocessed.get("timeline", [])})
        edl.date_range = _format_date_range(all_dates) if all_dates else ""

    # Store music intent — actual generation happens in assemble
    if music_file and music_file != "auto" and Path(music_file).exists():
        _log(f"Attaching music file: {music_file}")
        edl.music = MusicTrack(file=music_file)
        edl.music_mode = "file"
    elif music_file == "auto":
        edl.music_mode = "auto"
        _log("Music mode: auto (will generate in generate_music step)")

    from .edl import find_latest_version, save_edl
    version = find_latest_version(cfg) + 1
    save_edl(cfg, edl, version)

    clips_dir = cfg.workspace / "clips"
    if clips_dir.exists():
        for f in clips_dir.iterdir():
            f.unlink(missing_ok=True)

    _log(f"EDL v{version}: {len(edl.segments)} segments, "
         f"{len(edl.all_items())} items, ~{edl.estimated_duration():.0f}s")
    return edl, version
