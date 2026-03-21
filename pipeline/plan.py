"""Stage 3: Generate EDL — select photos/videos and arrange into a narrative.

Uses the visual planner (Gemini 3 Flash): Gemini sees actual photos via
contact sheets and watches video clips (with audio) to create an EDL.

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

# ---------------------------------------------------------------------------
# Prompt data — loaded from pipeline/prompts/ JSON files at runtime
# ---------------------------------------------------------------------------

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_json(name: str) -> dict:
    """Load a JSON file from the prompts directory."""
    path = _PROMPTS_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


from functools import lru_cache


@lru_cache(maxsize=1)
def _load_narrative_guidance() -> dict:
    return _load_json("narrative_guidance.json")


@lru_cache(maxsize=1)
def _load_lang_instructions() -> dict:
    return _load_json("lang_instructions.json")


@lru_cache(maxsize=1)
def _load_system_template() -> str:
    path = _PROMPTS_DIR / "visual_planner_system.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt template not found: {path}")
    return path.read_text(encoding="utf-8")


def _default_focus(trip_type: str) -> str:
    data = _load_narrative_guidance()
    defaults = data.get("_default_focus", {})
    return defaults.get(trip_type, defaults.get("general", "highlights and memorable moments"))


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
        return f"{first.strftime('%B')} {first.day} - {last.strftime('%B')} {last.day}, {first.year}"
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

    # First pass: calculate total media size to decide inline vs Files API
    n_text = 0
    n_media = 0
    text_chars = 0
    media_bytes_total = 0
    for p in user_parts:
        if isinstance(p, str):
            n_text += 1
            text_chars += len(p)
        elif isinstance(p, dict) and p.get("type") in ("image_bytes", "audio_bytes", "video_bytes"):
            n_media += 1
            media_bytes_total += len(p.get("data", b""))

    # Use Files API for large payloads (>20MB inline limit)
    use_files_api = media_bytes_total > 20 * 1024 * 1024
    uploaded_files = []  # track for cleanup

    parts = []
    for p in user_parts:
        if isinstance(p, str):
            parts.append(types.Part(text=p))
        elif isinstance(p, dict) and p.get("type") in ("image_bytes", "audio_bytes", "video_bytes"):
            is_video = p.get("type") == "video_bytes"
            mime = p.get("mime_type", "image/jpeg")

            if use_files_api:
                # Write to temp file, upload via Files API
                import tempfile
                suffix = ".mp4" if is_video else (".wav" if "audio" in p.get("type", "") else ".jpg")
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
                    tf.write(p["data"])
                    tf_path = tf.name
                try:
                    uploaded = client.files.upload(file=tf_path)
                    uploaded_files.append(uploaded)
                    parts.append(types.Part(
                        file_data=types.FileData(
                            file_uri=uploaded.uri,
                            mime_type=mime,
                        ),
                    ))
                finally:
                    Path(tf_path).unlink(missing_ok=True)
            else:
                res = types.MediaResolution.MEDIA_RESOLUTION_LOW if is_video \
                    else types.MediaResolution.MEDIA_RESOLUTION_MEDIUM
                parts.append(types.Part(
                    inline_data=types.Blob(mime_type=mime, data=p["data"]),
                    media_resolution=res,
                ))
        elif isinstance(p, types.Part):
            parts.append(p)

    upload_method = "Files API" if use_files_api else "inline"
    _log(f"=== [Gemini] API Call: {label} ===")
    _log(f"  Model: {model}")
    _log(f"  System prompt: {len(system)} chars")
    _log(f"  Input: {n_text} text parts ({text_chars} chars), "
         f"{n_media} media files ({media_bytes_total / 1024 / 1024:.1f}MB, {upload_method})")
    if use_files_api:
        _log(f"  Uploaded {len(uploaded_files)} files via Files API")
    # Log system prompt (truncated for readability)
    for line in system.split("\n")[:5]:
        _log(f"  [system] {line}")
    _log(f"  [system] ... ({len(system.split(chr(10)))} lines total)")
    # Log text parts sent to Gemini
    for i, p in enumerate(user_parts):
        if isinstance(p, str):
            preview = p[:200].replace("\n", " ")
            _log(f"  [text #{i}] {preview}{'...' if len(p) > 200 else ''}")
        elif isinstance(p, dict):
            ptype = p.get("type", "?")
            size_kb = len(p.get("data", b"")) // 1024
            _log(f"  [media #{i}] {ptype} {p.get('mime_type', '?')} ({size_kb}KB)")

    import time as _time
    t0 = _time.monotonic()

    response = client.models.generate_content(
        model=model,
        contents=[types.Content(parts=parts)],
        config=types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=16000,
            temperature=0.7,
            thinking_config=types.ThinkingConfig(
                thinking_level="HIGH",
            ),
        ),
    )

    elapsed = _time.monotonic() - t0

    # Cleanup uploaded files (auto-deleted after 48h, but clean up proactively)
    for uf in uploaded_files:
        try:
            client.files.delete(name=uf.name)
        except Exception:
            pass

    content = response.text or ""
    usage = response.usage_metadata
    input_tokens = usage.prompt_token_count or 0
    output_tokens = usage.candidates_token_count or 0
    # Gemini 3 Flash pricing: ~$0.01/M input, ~$0.04/M output (approx)
    cost_est = input_tokens * 0.01 / 1_000_000 + output_tokens * 0.04 / 1_000_000
    _log(f"  Response: {input_tokens:,} input tokens, "
         f"{output_tokens:,} output tokens, {elapsed:.1f}s")
    _log(f"  Estimated cost: ${cost_est:.4f}")
    _log(f"  Output: {len(content)} chars")
    # Log first 500 chars of response for debugging
    preview = content[:500].replace("\n", " ")
    _log(f"  Preview: {preview}...")
    _log(f"=== [Gemini] End {label} ===")

    return content


# ---------------------------------------------------------------------------
# Visual planner system prompt
# ---------------------------------------------------------------------------

def _visual_system_prompt(trip_type: str, language: str = "en") -> str:
    """System prompt for visual planner — loaded from pipeline/prompts/ files."""
    narrative_data = _load_narrative_guidance()
    lang_data = _load_lang_instructions()
    template = _load_system_template()

    guidance = narrative_data.get(trip_type, narrative_data.get("general", ""))
    lang_instruction = lang_data.get(language, lang_data.get("en", ""))

    return template.format(guidance=guidance, lang_instruction=lang_instruction)


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
    - video_items: list of video analysis dicts for preview clips
    """
    lines = []
    photo_paths = []
    photo_labels = []  # matching labels for contact sheet (same index as photo_paths)
    video_items = []
    idx = start_idx

    for item_id in chapter.get("item_ids", []):
        a = analysis_by_id.get(item_id)
        if not a:
            continue

        local_path = a.get("local_path", "")
        media = a.get("media_type", "photo")
        persons = a.get("persons", [])
        taken_iso = a.get("taken_iso", "")
        time_str = taken_iso[11:16] if taken_iso and len(taken_iso) >= 16 else ""

        label = f"#{idx:02d}"
        # Describe who's in the photo
        if a.get("family_count", 0) >= 2:
            who = f"family together ({','.join(persons[:3])})" if persons else "family together"
        elif a.get("family_count", 0) == 1:
            who = f"{persons[0]}" if persons else "one family member"
        elif persons:
            who = f"people: {','.join(persons[:3])}"
        else:
            who = "unknown"
        parts = [f"{label}: {who}"]
        if time_str:
            parts.append(f"time={time_str}")

        # Per-item location (helps Gemini with text overlays)
        item_loc = a.get("district") or a.get("first_level") or a.get("country")
        if item_loc:
            parts.append(f"at={item_loc}")

        if media == "video":
            dur = a.get("video_duration") or (a.get("duration_ms", 0) / 1000)
            dur_s = f"{dur:.0f}s" if dur else "?"
            parts.append(f"video={dur_s}")
            video_items.append(a)
        else:
            exif_data = a.get("exif", {})
            if exif_data:
                exif_parts = []
                if exif_data.get("focal_length"):
                    exif_parts.append(f"{exif_data['focal_length']:.0f}mm")
                if exif_data.get("aperture"):
                    exif_parts.append(f"f/{exif_data['aperture']:.1f}")
                if exif_data.get("iso"):
                    exif_parts.append(f"ISO{exif_data['iso']}")
                if exif_parts:
                    parts.append(" ".join(exif_parts))
            photo_paths.append(Path(local_path))
            photo_labels.append(label)  # keep label in sync with photo_paths

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
    return text, photo_paths, photo_labels, video_items



def _generate_video_previews(
    video_items: list[dict], preview_dir: Path, log_fn=None,
) -> None:
    """Generate one full-length preview per video (360p 1fps + audio).

    Gemini processes video at 1fps internally — sending 1fps means zero
    wasted frames. Full-length means 100% coverage (vs 71% with clips).
    Gemini can reference any second natively for trim point decisions.
    """
    import os
    from .media_utils import run_subprocess

    _log = log_fn or print
    # 360p 1fps CRF35 — matches Gemini's internal processing rate
    encoder = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "35"]
    max_workers = max(4, (os.cpu_count() or 4) // 2)

    tasks: list[tuple[Path, list[str]]] = []
    for vi in video_items:
        vid_id = vi["id"]
        source = Path(vi.get("local_path", ""))
        dur = vi.get("video_duration", 0)
        if not source.exists() or dur <= 0:
            continue

        preview_path = preview_dir / f"preview_{vid_id}.mp4"
        if not preview_path.exists():
            tasks.append((preview_path, [
                "ffmpeg", "-y", "-i", str(source),
                "-vf", "scale=360:-2", "-r", "1",
                *encoder,
                "-c:a", "aac", "-b:a", "64k", "-ac", "1",
                str(preview_path),
            ]))

    if not tasks:
        return

    _log(f"Generating {len(tasks)} video clips (CPU x{max_workers})...")

    from .parallel import run_parallel

    def _progress(done, total):
        if done % 20 == 0 or done == total:
            _log(f"  Video clips: {done}/{total}")

    parallel_tasks = [(p, lambda cmd=cmd: run_subprocess(cmd, capture_output=True)) for p, cmd in tasks]
    run_parallel(parallel_tasks, max_workers, progress_fn=_progress)

    n_ok = sum(1 for p, _ in tasks if p.exists() and p.stat().st_size > 500)
    _log(f"  Video clips done: {n_ok}/{len(tasks)} OK")


def _build_visual_content_blocks(
    preprocessed: dict, analysis_by_id: dict, cfg: Config, log_fn=None,
) -> list:
    """Build multimodal parts: interleaved text + contact sheets + full video previews.

    Returns list of str and media dicts suitable for _gemini_call().
    Videos are sent as full-length 360p 1fps MP4 previews (with audio) so
    Gemini sees 100% of content and can reference any second for trim points.
    Photos are sent as contact sheet grids.
    """
    from .media_utils import make_contact_sheet, run_subprocess

    _log = log_fn or print
    blocks: list = []
    sheets_dir = cfg.contact_sheets_dir
    preview_dir = cfg.preview_clips_dir

    global_idx = 1  # continuous numbering across chapters

    # Pre-generate ALL video previews in one parallel batch (not per-chapter)
    all_video_items = []
    for day in preprocessed["timeline"]:
        for chapter in day["chapters"]:
            for item_id in chapter.get("item_ids", []):
                a = analysis_by_id.get(item_id)
                if a and a.get("media_type") == "video":
                    all_video_items.append(a)
    if all_video_items:
        _generate_video_previews(all_video_items, preview_dir, _log)

    for day in preprocessed["timeline"]:
        for chapter in day["chapters"]:
            text, photo_paths, photo_labels, video_items = _build_visual_chapter_text(
                chapter, day, analysis_by_id, global_idx,
            )
            n_items = len(photo_paths) + len(video_items)
            if n_items == 0:
                continue

            blocks.append(text)

            # Contact sheet for photos — labels must match text metadata numbering
            if photo_paths:
                thumb_paths = []
                for p in photo_paths:
                    thumb = cfg.thumbnails_dir / f"{p.stem}_thumb.jpg"
                    thumb_paths.append(thumb if thumb.exists() else p)

                loc_safe = chapter.get("location", "x").replace("/", "_")[:30]
                sheet_name = f"{day['date']}_{chapter.get('time_block', 'x')}_{loc_safe}.jpg"
                sheet_path = sheets_dir / sheet_name

                max_per_sheet = 12
                sheet_idx = 0
                for chunk_start in range(0, len(thumb_paths), max_per_sheet):
                    chunk = thumb_paths[chunk_start:chunk_start + max_per_sheet]
                    chunk_labels = photo_labels[chunk_start:chunk_start + len(chunk)]
                    s_path = sheets_dir / f"{sheet_name.replace('.jpg', '')}_{sheet_idx}.jpg" if len(thumb_paths) > max_per_sheet else sheet_path
                    if not s_path.exists():
                        make_contact_sheet(chunk, s_path, cell_size=600, columns=4, labels=chunk_labels)
                        _log(f"Contact sheet: {s_path.name} ({len(chunk)} photos)")
                    blocks.append({
                        "type": "image_bytes",
                        "mime_type": "image/jpeg",
                        "data": s_path.read_bytes(),
                    })
                    sheet_idx += 1

            # Videos: full previews already generated in batch above
            for vi in video_items:
                vid_id = vi["id"]
                dur = vi.get("video_duration", 0)
                if dur <= 0:
                    continue

                item_num = global_idx + len(photo_paths) + video_items.index(vi)
                preview_path = preview_dir / f"preview_{vid_id}.mp4"
                if preview_path.exists() and preview_path.stat().st_size > 500:
                    blocks.append(
                        f"Video #{item_num:02d} ({dur:.0f}s full preview with audio). "
                        f"Watch it and pick the best moment — set start_time/end_time in seconds:"
                    )
                    blocks.append({
                        "type": "video_bytes",
                        "mime_type": "video/mp4",
                        "data": preview_path.read_bytes(),
                    })

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
    _log(f"Visual content: {n_text} text blocks, {n_img} contact sheets, {n_vid_clips} video previews")

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
CRITICAL: The vlog MUST be {target_duration}s long. Select ~{n_items} items to fill this duration.
Photos = 3-5s each, videos = 5-10s each. Do the math: {n_items} items × ~4s avg = {target_duration}s.
If your EDL totals less than {int(target_duration * 0.9)}s, you have selected TOO FEW items — add more.
Focus: {focus}.

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

    # Layer 1: Auto-retry on parse failure
    import json as _json
    from pydantic import ValidationError

    edl = None
    for attempt in range(2):
        edl_content = _gemini_call(system_prompt, visual_parts, _log,
                                   label="single pass: plan", model="gemini-3-flash-preview")

        _log(f"=== [Gemini] EDL RESPONSE ({len(edl_content)} chars) ===")
        # Log full response (may contain thinking + JSON)
        for line in edl_content.split("\n"):
            _log(f"  | {line}")
        _log("=== [Gemini] END RESPONSE ===")

        edl_content = strip_markdown_fences(edl_content)
        try:
            edl = EDL.model_validate_json(edl_content)
            break
        except (ValidationError, _json.JSONDecodeError) as e:
            _log(f"Parse failed (attempt {attempt + 1}/2): {e}")
            if attempt == 1:
                raise

    assert edl is not None  # guaranteed by the loop above

    # Layer 2: Fix hallucinated file paths
    media_dir = cfg.media_dir
    removed_count = 0
    for seg in edl.segments:
        valid_items = []
        for item in seg.items:
            source = Path(item.source_file)
            if source.exists():
                valid_items.append(item)
                continue
            # Try fuzzy match: strip numeric ID prefix (e.g., "87681_IMG.jpg" → "IMG.jpg")
            name = source.name
            parts = name.split("_", 1)
            candidates = list(media_dir.glob(f"*{parts[-1]}")) if len(parts) > 1 else []
            if not candidates:
                candidates = list(media_dir.glob(f"*{name}"))
            if candidates:
                item.source_file = str(candidates[0])
                _log(f"  Fixed path: {name} → {candidates[0].name}")
                valid_items.append(item)
            else:
                _log(f"  Removed item with missing source: {name}")
                removed_count += 1
        seg.items = valid_items
    # Remove empty segments
    edl.segments = [s for s in edl.segments if s.items]
    if removed_count:
        _log(f"  Path validation: removed {removed_count} items with missing sources")

    # Layer 2b: Validate video trim points against actual duration
    trim_fixed = 0
    trim_removed = 0
    for seg in edl.segments:
        valid_items = []
        for item in seg.items:
            if item.media_type == "video" and item.start_time is not None:
                vid_dur = analysis_by_id.get(
                    next((aid for aid, a in analysis_by_id.items()
                          if a.get("local_path") == item.source_file), None),
                    {},
                ).get("video_duration")
                if vid_dur and vid_dur > 0:
                    changed = False
                    if item.start_time >= vid_dur:
                        item.start_time = max(vid_dur - 2, 0)
                        changed = True
                    if item.end_time is not None and item.end_time > vid_dur:
                        item.end_time = vid_dur
                        changed = True
                    if item.end_time is not None and item.start_time >= item.end_time:
                        _log(f"  Trim removal: {Path(item.source_file).name} "
                             f"start={item.start_time:.1f} >= end={item.end_time:.1f} "
                             f"(duration={vid_dur:.1f}s)")
                        trim_removed += 1
                        continue
                    if changed:
                        _log(f"  Trim clamped: {Path(item.source_file).name} "
                             f"to [{item.start_time:.1f}, {item.end_time}] "
                             f"(duration={vid_dur:.1f}s)")
                        trim_fixed += 1
            valid_items.append(item)
        seg.items = valid_items
    edl.segments = [s for s in edl.segments if s.items]
    if trim_fixed or trim_removed:
        _log(f"  Trim validation: {trim_fixed} clamped, {trim_removed} removed")

    # Layer 3: Duration check
    actual_dur = edl.estimated_duration()
    if actual_dur < target_duration * 0.5:
        _log(f"WARNING: EDL is {actual_dur:.0f}s, target is {target_duration}s — severely underfilled")
    elif actual_dur < target_duration * 0.8:
        _log(f"WARNING: EDL is {actual_dur:.0f}s, target is {target_duration}s — underfilled")

    n_vid = sum(1 for i in edl.all_items() if i.media_type == "video")
    n_photo = sum(1 for i in edl.all_items() if i.media_type == "photo")
    n_keep_audio = sum(1 for i in edl.all_items() if i.keep_audio)
    n_text_overlay = sum(1 for i in edl.all_items() if i.text_overlay)
    n_speed_ramp = sum(1 for i in edl.all_items() if i.playback_speed != 1.0)

    _log(f"=== [Gemini] PARSED EDL ===")
    _log(f"  Title: {edl.title}")
    _log(f"  Segments: {len(edl.segments)}, Items: {len(edl.all_items())} "
         f"({n_photo} photos + {n_vid} videos)")
    _log(f"  Duration: {actual_dur:.0f}s (target: {target_duration}s, "
         f"{'OK' if actual_dur >= target_duration * 0.8 else 'UNDERFILLED'})")
    _log(f"  Speech clips (keep_audio): {n_keep_audio}")
    _log(f"  Text overlays: {n_text_overlay}")
    _log(f"  Speed ramps: {n_speed_ramp}")

    for si, seg in enumerate(edl.segments):
        seg_dur = sum(i.display_duration for i in seg.items)
        _log(f"  --- Segment {si}: {seg.name} ({len(seg.items)} items, {seg_dur:.0f}s) ---")
        _log(f"    Transition: {seg.transition} ({seg.transition_duration}s) | "
             f"Mode: {seg.mode} | Color: {seg.color_temp}")
        _log(f"    Music mood: {seg.music_mood[:120]}")
        if seg.narrative_rationale:
            _log(f"    Rationale: {seg.narrative_rationale[:150]}")
        for item in seg.items:
            trim = f" trim={item.start_time:.0f}-{item.end_time:.0f}s" if item.start_time is not None else ""
            flags = []
            if item.keep_audio:
                flags.append("SPEECH")
            if item.playback_speed != 1.0:
                flags.append(f"speed={item.playback_speed}x")
            if item.text_overlay:
                flags.append(f'text="{item.text_overlay.text[:30]}"')
            flag_str = f" [{', '.join(flags)}]" if flags else ""
            _log(f"    - {item.media_type:5s} {item.display_duration}s "
                 f"{item.effect:16s} {Path(item.source_file).name}{trim}{flag_str}")
    _log(f"=== [Gemini] END PARSED EDL ===")

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
    resolution: tuple[int, int] = (3840, 2160),
    fps: int = 60,
    quality: float = 1.0,
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

    # Set metadata / render settings on the EDL
    edl.trip_type = trip_type
    edl.style = style
    edl.language = language
    edl.resolution = resolution
    edl.fps = fps
    edl.quality = quality
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
