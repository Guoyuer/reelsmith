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
2. **Family is the heart**: At least 30-40% of items MUST show family members.
   PRIORITIZE candid moments over posed photos — a blurry shot of real laughter
   beats a sharp photo of everyone smiling at the camera. Look for:
   - Genuine reactions: surprise, delight, awe, exhaustion, silliness
   - Physical connection: holding hands, hugs, piggyback rides, leaning in
   - Shared experiences: pointing at something together, first bites, splashing
   - Quiet moments: a child sleeping, a parent watching their kid explore
   AVOID: generic landmark poses, everyone-look-at-camera group shots (unless
   the expressions are genuinely joyful), repetitive similar photos.
   Every segment needs at least one close-up family moment.""",
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

    # Build content parts with per-part media_resolution control
    # Images at MEDIUM (560 tokens) — good detail at half the cost of HIGH
    # Video at LOW (70 tokens/frame) — motion + audio assessment doesn't need high res
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
        elif isinstance(p, dict) and p.get("type") in ("image_bytes", "audio_bytes", "video_bytes"):
            is_video = p.get("type") == "video_bytes"
            res = types.MediaResolution.MEDIA_RESOLUTION_LOW if is_video \
                else types.MediaResolution.MEDIA_RESOLUTION_MEDIUM
            parts.append(types.Part(
                inline_data=types.Blob(
                    mime_type=p.get("mime_type", "image/jpeg"),
                    data=p["data"],
                ),
                media_resolution=res,
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

_LANG_INSTRUCTIONS = {
    "en": "Write ALL text content (title, segment names, text overlays, narrative_rationale) in English.",
    "cn": "Write ALL text content (title, segment names, text overlays, narrative_rationale) in Chinese (简体中文). "
          "Use natural, evocative Chinese — not literal translations from English.",
    "both": "Write ALL text content in BOTH languages. Format: \"English / 中文\". "
            "For example: title \"Family Adventure / 家庭奇遇\", segment name \"Wonder & Joy / 惊喜与欢乐\", "
            "text overlay \"Day One / 第一天\". Keep both versions concise.",
}


def _visual_system_prompt(trip_type: str, language: str = "en") -> str:
    """System prompt for visual planner — Claude sees contact sheets and filmstrips."""
    guidance = _NARRATIVE_GUIDANCE.get(trip_type, _NARRATIVE_GUIDANCE["general"])
    lang_instruction = _LANG_INSTRUCTIONS.get(language, _LANG_INSTRUCTIONS["en"])
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
- **Video clips**: Short MP4 samples (5s from the middle) WITH AUDIO. Watch and listen
  to each clip. Judge motion quality, framing, and audio content. If you hear family
  speech, laughter, or reactions — that video is especially valuable.
- **Metadata per item**: tier (A=family together, B=one family member, C=scenery),
  person names, location, time.

## Narrative principles

1. **Emotional arc**: Build from curiosity → joy → warmth → nostalgia.

{guidance}

3. **Video-first**: Prefer video clips over photos when both cover the same moment.
   Videos bring motion, atmosphere, and sound — they make a vlog feel alive, not like
   a slideshow. Aim for 40-60% video content by screen time. If you hear family voices
   or meaningful audio in a video clip, set keep_audio=true to preserve it.

4. **Rhythm**: Alternate photos (3-5s, Ken Burns) with video clips (5-10s, real motion).
   Vary pacing — fast cuts for energy, lingering shots for emotion.

5. **Visual judgment**: Use what you SEE in the photos. Trust your eyes over metadata.
   - A candid shot of real laughter beats a posed landmark photo every time
   - Look for emotion in body language, not just faces — leaning in, pointing, running
   - Blurry but emotional > sharp but boring
   - Pick the ONE best photo from a series of similar shots, not multiple

6. **Text overlays**: Evocative, not descriptive. Keep rare (3-5 per vlog max).
   BAD: "Day 1 - Marina Bay", "Gardens by the Bay", "Dinner time"
   GOOD: "The moment we arrived", "Her first time seeing the ocean", "Last night together"
   Text should make the viewer FEEL something, not just label a location.

8. **Language**: {lang_instruction}

7. **Music mood**: Each segment gets its OWN music track. Write a specific, vivid music_mood
   that captures the emotional tone — this will be sent directly to a music generation AI.
   Be specific about instruments and feeling, not generic:
   BAD: "happy music", "sad music", "travel music"
   GOOD: "warm fingerpicked acoustic guitar with light shaker, sun-dappled morning feeling"
   GOOD: "playful marimba and claps, children's adventure energy, building excitement"
   GOOD: "slow solo piano with subtle strings, bittersweet farewell, lingering warmth"

## Technical rules

- display_duration: 3-5s per photo, 5-10s for video clips
- For videos: set start_time and end_time to select the best scene
- effect: ken_burns_in/out/left/right for photos, "none" for video clips
- playback_speed: 1.0 = normal (default). Use 0.5 SPARINGLY for dramatic slow-mo moments
  (a jump, a splash, a reaction). Use 1.5 for transitional walking/travel clips. Most clips = 1.0.
- Transitions: choose per segment — crossfade (default), dissolve, smoothleft, smoothright,
  circlecrop, fade_black (major scene changes), wipe_left. Vary for visual richness.
- mode: "narrative" (default) or "montage" — use montage for 1 energy burst segment max
  (quick 1-2s cuts, no transitions, builds excitement before a calm segment)
- color_temp: "neutral" (default), "warm" (family/food/indoor), "cool" (night/architecture).
  Use conservatively — most segments should be neutral.
- CRITICAL: source_file must be the EXACT path value from the text metadata

Think step-by-step, then output valid JSON only:
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
      "mode": "narrative|montage",
      "color_temp": "neutral|warm|cool",
      "items": [
        {{
          "source_file": "<exact path from metadata>",
          "media_type": "photo|video",
          "display_duration": 3.0-10.0,
          "start_time": null or <seconds for video trim start>,
          "end_time": null or <seconds for video trim end>,
          "effect": "ken_burns_in|ken_burns_out|ken_burns_left|ken_burns_right|static|none",
          "playback_speed": 1.0 (default, 0.5 for slow-mo, 1.5 for fast),
          "keep_audio": true or false (for videos: true if you heard meaningful speech/reactions),
          "text_overlay": null or {{"text": "string", "position": "bottom", "font_size": 48}}
        }}
      ],
      "transition": "crossfade|dissolve|smoothleft|smoothright|circlecrop|fade_black|wipe_left",
      "transition_duration": 0.8
    }}
  ],
  "music": null
}}"""


# ---------------------------------------------------------------------------
# Visual planner content builders
# ---------------------------------------------------------------------------

def _read_exif_brief(path: Path) -> str:
    """Extract brief EXIF info (focal length, aperture, ISO) from a photo."""
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS
        img = Image.open(path)
        exif_data = img._getexif()
        if not exif_data:
            return ""
        exif = {TAGS.get(k, k): v for k, v in exif_data.items()}
        parts = []
        fl = exif.get("FocalLength")
        if fl:
            fl_val = float(fl) if not hasattr(fl, 'numerator') else fl.numerator / fl.denominator
            parts.append(f"{fl_val:.0f}mm")
        fn = exif.get("FNumber")
        if fn:
            fn_val = float(fn) if not hasattr(fn, 'numerator') else fn.numerator / fn.denominator
            parts.append(f"f/{fn_val:.1f}")
        iso = exif.get("ISOSpeedRatings")
        if iso:
            parts.append(f"ISO{iso}")
        return " ".join(parts)
    except Exception:
        return ""


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
            # Add EXIF camera metadata for photos
            exif = _read_exif_brief(Path(local_path))
            if exif:
                parts.append(exif)
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
    """Build multimodal parts: interleaved text + contact sheets + video clips.

    Returns list of str and media dicts suitable for _gemini_call().
    Videos are sent as short MP4 clips (with audio) so Gemini can see motion
    and hear speech. Photos are sent as contact sheet grids.
    """
    from .media_utils import make_contact_sheet, run_subprocess

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
                thumb_paths = []
                for p in photo_paths:
                    thumb = cfg.workspace / "thumbnails" / f"{p.stem}_thumb.jpg"
                    thumb_paths.append(thumb if thumb.exists() else p)

                loc_safe = chapter.get("location", "x").replace("/", "_")[:30]
                sheet_name = f"{day['date']}_{chapter.get('time_block', 'x')}_{loc_safe}.jpg"
                sheet_path = sheets_dir / sheet_name

                max_per_sheet = 6
                sheet_idx = 0
                for chunk_start in range(0, len(thumb_paths), max_per_sheet):
                    chunk = thumb_paths[chunk_start:chunk_start + max_per_sheet]
                    chunk_labels = [f"#{global_idx + chunk_start + i:02d}" for i in range(len(chunk))]
                    s_path = sheets_dir / f"{sheet_name.replace('.jpg', '')}_{sheet_idx}.jpg" if len(thumb_paths) > max_per_sheet else sheet_path
                    make_contact_sheet(chunk, s_path, cell_size=600, columns=3, labels=chunk_labels)
                    _log(f"Contact sheet: {s_path.name} ({len(chunk)} photos)")
                    blocks.append({
                        "type": "image_bytes",
                        "mime_type": "image/jpeg",
                        "data": s_path.read_bytes(),
                    })
                    sheet_idx += 1

            # Videos: short (<15s) sent in full, long sent as 3×2s clips
            for vi in video_items:
                vid_id = vi["id"]
                source = Path(vi.get("local_path", ""))
                dur = vi.get("video_duration", 0)
                if not source.exists() or dur <= 0:
                    continue

                item_num = global_idx + len(photo_paths) + video_items.index(vi)

                if dur <= 15:
                    # Short video: send full clip so Gemini sees complete interactions
                    clip_path = sheets_dir / f"clip_{vid_id}_full.mp4"
                    if not clip_path.exists():
                        run_subprocess(
                            ["ffmpeg", "-y", "-i", str(source),
                             "-vf", "scale=480:-2",
                             "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                             "-c:a", "aac", "-b:a", "64k", "-ac", "1",
                             str(clip_path)],
                            capture_output=True,
                        )
                    if clip_path.exists() and clip_path.stat().st_size > 500:
                        blocks.append(f"Video #{item_num:02d} ({dur:.0f}s, FULL clip with audio):")
                        blocks.append({
                            "type": "video_bytes",
                            "mime_type": "video/mp4",
                            "data": clip_path.read_bytes(),
                        })
                        _log(f"Video #{item_num:02d}: {source.name} (full {dur:.0f}s)")
                    else:
                        _log(f"Video #{item_num:02d}: {source.name} (full clip failed)")
                else:
                    # Long video: send 3×2s samples (start/mid/end)
                    blocks.append(f"Video #{item_num:02d} ({dur:.0f}s total, 3 samples with audio):")
                    clip_positions = [
                        ("start", max(0, dur * 0.1)),
                        ("mid", max(0, (dur - 2) / 2)),
                        ("end", max(0, dur * 0.9 - 2)),
                    ]
                    clips_sent = 0
                    for clip_label, clip_start in clip_positions:
                        clip_path = sheets_dir / f"clip_{vid_id}_{clip_label}.mp4"
                        if not clip_path.exists():
                            run_subprocess(
                                ["ffmpeg", "-y", "-ss", str(clip_start),
                                 "-i", str(source), "-t", "2",
                                 "-vf", "scale=480:-2",
                                 "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                                 "-c:a", "aac", "-b:a", "64k", "-ac", "1",
                                 str(clip_path)],
                                capture_output=True,
                            )
                        if clip_path.exists() and clip_path.stat().st_size > 500:
                            blocks.append({
                                "type": "video_bytes",
                                "mime_type": "video/mp4",
                                "data": clip_path.read_bytes(),
                            })
                            clips_sent += 1
                    _log(f"Video #{item_num:02d}: {source.name} ({clips_sent} clips)")

            global_idx += n_items

    return blocks



# ---------------------------------------------------------------------------
# Visual planner — multi-pass Gemini planning with visual input
# ---------------------------------------------------------------------------

def _plan_visual(
    cfg: Config, preprocessed: dict, analysis_by_id: dict,
    analysis_items: list[dict],
    style: str, target_duration: int, focus: str,
    trip_type: str = "family", language: str = "en", log_fn=None,
) -> EDL:
    """Single-pass Gemini planning with chain-of-thought.

    Gemini sees contact sheets (12 photos/sheet at 400px) + video clips,
    designs narrative arc + selects items + self-reviews in one call.
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
    # Single pass: chain-of-thought (arc → select → self-review)
    # ------------------------------------------------------------------
    _log("=== SINGLE-PASS PLANNING ===")

    _log("Building contact sheets (12/sheet @ 400px) and video clips...")
    content_blocks = _build_visual_content_blocks(preprocessed, analysis_by_id, cfg, _log)
    n_img = sum(1 for b in content_blocks if isinstance(b, dict) and b.get("type") == "image_bytes")
    n_vid_clips = sum(1 for b in content_blocks if isinstance(b, dict) and b.get("type") == "video_bytes")
    n_text = sum(1 for b in content_blocks if isinstance(b, str))
    _log(f"Visual content: {n_text} text blocks, {n_img} contact sheets, {n_vid_clips} video clips")

    # Build trip structure summary for arc thinking
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

    intro_text = f"""\
Create a {style} {trip_label} vlog EDL from the photos and videos shown below.

{trip_summary}{family_line}
Target: {target_duration}s (~{n_items} items). Focus: {focus}.

Trip structure:
{"".join(arc_lines)}

**Think step-by-step:**
1. First, design a narrative arc — 4-6 chapters based on STORY BEATS (not locations).
2. Then, look at every contact sheet and video clip. Select the best items for each chapter.
3. Finally, self-review: check pacing, variety, video/photo balance. Fix any issues.

Output ONE JSON with all your thinking and the final EDL.

Candidates by day/location:"""

    visual_parts: list = [intro_text] + content_blocks

    system_prompt = _visual_system_prompt(trip_type, language=language)
    _log(f"Sending {len(visual_parts)} parts to Gemini (single pass)...")

    from .media_utils import strip_markdown_fences

    edl_content = _gemini_call(system_prompt, visual_parts, _log,
                               label="single pass: plan", model="gemini-3-flash-preview")

    _log(f"=== EDL RESPONSE ({len(edl_content)} chars) ===")
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

    return edl




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
    language: str = "en",
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

    _log(f"Planning via Gemini with visual input (target {target_duration}s, style={style}, trip_type={trip_type}, lang={language})...")
    edl = _plan_visual(cfg, preprocessed, analysis_by_id, analysis_items,
                       style=style, target_duration=target_duration,
                       focus=effective_focus, trip_type=trip_type,
                       language=language, log_fn=_log)

    # Post-process: force effect="none" on video items (Ken Burns fights native motion)
    for seg in edl.segments:
        for item in seg.items:
            if item.media_type == "video" and item.effect != "none":
                item.effect = "none"

    # Set metadata fields
    edl.trip_type = trip_type
    edl.style = style
    edl.language = language
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
