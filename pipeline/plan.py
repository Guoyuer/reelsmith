"""Stage 3: Generate EDL — algorithmic or API-based planning.

Two backends:
  - "algo" (default): deterministic algorithm using analysis scores
  - "api": Claude API call for narrative-aware planning

Set via PlanConfig.planner or CLI --planner flag.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pillow_heif
from PIL import Image

pillow_heif.register_heif_opener()

from .config import Config
from .edl import EDL, EditItem, MusicTrack, Segment, TextOverlay

# Type alias for (analysis_dict, chapter_dict, date_string) tuples
Candidate = tuple[dict, dict, str]


# ---------------------------------------------------------------------------
# Trip-type configuration
# ---------------------------------------------------------------------------

TRIP_TYPES = {
    "family", "solo", "food", "adventure", "architecture", "general",
}

SCORING_PROFILES: dict[str, dict] = {
    "family": {
        "tier_bonus": {"A": 20, "B": 10, "C": 0},
        "togetherness_w": 2.0, "emotion_w": 1.5, "quality_w": 1.0,
        "scene_bonus": {},
    },
    "solo": {
        "tier_bonus": {"A": 5, "B": 5, "C": 10},
        "togetherness_w": 0.0, "emotion_w": 1.0, "quality_w": 2.0,
        "scene_bonus": {"landmark": 5, "nature": 5},
    },
    "food": {
        "tier_bonus": {"A": 5, "B": 5, "C": 10},
        "togetherness_w": 0.5, "emotion_w": 1.0, "quality_w": 1.5,
        "scene_bonus": {"food": 10, "meal": 10},
    },
    "adventure": {
        "tier_bonus": {"A": 10, "B": 8, "C": 8},
        "togetherness_w": 0.5, "emotion_w": 2.0, "quality_w": 1.5,
        "scene_bonus": {"activity": 8, "nature": 5},
    },
    "architecture": {
        "tier_bonus": {"A": 3, "B": 3, "C": 15},
        "togetherness_w": 0.0, "emotion_w": 0.5, "quality_w": 2.5,
        "scene_bonus": {"landmark": 8, "building": 8},
    },
    "general": {
        "tier_bonus": {"A": 10, "B": 7, "C": 5},
        "togetherness_w": 1.0, "emotion_w": 1.0, "quality_w": 1.5,
        "scene_bonus": {"landmark": 3, "nature": 3, "meal": 3},
    },
}

DEFAULT_FOCUS = {
    "family": "happiness with family",
    "solo": "personal journey and discovery",
    "food": "culinary experiences and flavors",
    "adventure": "action, awe, and exploration",
    "architecture": "design, structures, and spaces",
    "general": "highlights and memorable moments",
}

STYLE_PARAMS = {
    "upbeat":     {"base_dur": 3.5, "vary": 0.5, "transition": "crossfade", "td": 0.6},
    "cinematic":  {"base_dur": 5.0, "vary": 1.0, "transition": "fade_black", "td": 1.2},
    "reflective": {"base_dur": 5.5, "vary": 1.5, "transition": "crossfade", "td": 1.0},
    "energetic":  {"base_dur": 2.5, "vary": 0.5, "transition": "crossfade", "td": 0.4},
}


def _default_focus(trip_type: str) -> str:
    return DEFAULT_FOCUS.get(trip_type, DEFAULT_FOCUS["general"])


# ---------------------------------------------------------------------------
# Scoring & filtering helpers
# ---------------------------------------------------------------------------

def _is_portrait_file(a: dict) -> bool:
    """Check if an item is portrait orientation from its local file."""
    try:
        path = Path(a.get("local_path", ""))
        if not path.exists():
            return False
        img = Image.open(path)
        w, h = img.size
        return h > w * 1.2
    except Exception:
        return False


def _item_score(a: dict, trip_type: str = "family") -> float:
    """Score an analyzed item for selection priority based on trip_type."""
    profile = SCORING_PROFILES.get(trip_type, SCORING_PROFILES["general"])
    v = a.get("vision", {})
    tier_bonus = profile["tier_bonus"].get(a.get("tier", "C"), 0)
    togetherness = v.get("togetherness", 0) * profile["togetherness_w"]
    emotion = v.get("genuine_emotion", 0) * profile["emotion_w"]
    quality = v.get("visual_quality", 5) * profile["quality_w"]
    scene = v.get("scene_type", v.get("story_beat", ""))
    scene_bonus = profile["scene_bonus"].get(scene, 0)

    score = tier_bonus + togetherness + emotion + quality + scene_bonus
    if _is_portrait_file(a):
        score -= 8
    return score


def _is_too_dark(path: Path, threshold: float = 50.0) -> bool:
    """Check if an image is too dark (mean brightness < threshold)."""
    try:
        import cv2
        img = cv2.imread(str(path))
        if img is None:
            import numpy as np
            pil_img = Image.open(path).convert("L").resize((64, 64))
            return float(np.mean(list(pil_img.getdata()))) < threshold
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        return float(gray.mean()) < threshold
    except Exception:
        return False


def _word_overlap(words_a: set[str], words_b: set[str]) -> float:
    """Jaccard similarity between two word sets."""
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


def _desc_words(a: dict) -> set[str]:
    """Extract lowercase description words from an analysis item."""
    return set(a.get("vision", {}).get("description", "").lower().split())


def _is_desc_duplicate(desc_words: set[str], seen: list[set[str]]) -> bool:
    """Check if description is too similar to any previously seen."""
    return any(_word_overlap(desc_words, d) > 0.5 for d in seen)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_day_label(date_str: str, first_date: str) -> str:
    """Format 'YYYY-MM-DD' as 'Day N · June 13' style label."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        first = datetime.strptime(first_date, "%Y-%m-%d")
        day_num = (dt - first).days + 1
        month_day = dt.strftime("%B %-d")
        return f"Day {day_num} \u00b7 {month_day}"
    except (ValueError, TypeError):
        return date_str


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


def _deterministic_choice(key: str, choices: list[str]) -> str:
    """Pick from choices deterministically based on key hash."""
    h = int(hashlib.md5(key.encode()).hexdigest(), 16)
    return choices[h % len(choices)]


# ---------------------------------------------------------------------------
# Duration & effect helpers
# ---------------------------------------------------------------------------

def _score_to_duration(a: dict, trip_type: str, base_dur: float, vary: float) -> float:
    """Map an item's score to display duration — better items get more time.

    Returns duration in [base_dur - vary, base_dur + 2*vary].
    """
    v = a.get("vision", {})
    togetherness = v.get("togetherness", 0)
    emotion = v.get("genuine_emotion", 0)
    quality = v.get("visual_quality", 5)
    tier = a.get("tier", "C")

    if trip_type == "family":
        raw = (togetherness * 0.35 + emotion * 0.35 + quality * 0.15
               + (1.0 if tier == "A" else 0.5 if tier == "B" else 0.0) * 1.5)
        max_raw = 10.0
    else:
        raw = emotion * 0.3 + quality * 0.4 + (0.5 if tier in ("A", "B") else 0.0)
        max_raw = 7.0

    importance = min(raw / max_raw, 1.0)
    dur = base_dur - vary + importance * 3.0 * vary
    return round(min(max(dur, base_dur - vary), base_dur + 2.0 * vary), 1)


def _choose_effect(a: dict, trip_type: str, hero_files: set[str]) -> str:
    """Choose Ken Burns effect based on content, not round-robin."""
    v = a.get("vision", {})
    scene = v.get("scene_type", v.get("story_beat", ""))
    tier = a.get("tier", "C")
    path = a.get("local_path", "")

    if path in hero_files:
        return "ken_burns_in"
    if tier == "C" and v.get("visual_quality", 0) >= 7:
        return "static"
    if scene in ("landmark", "nature", "scenery", "building"):
        return _deterministic_choice(path, ["ken_burns_left", "ken_burns_right"])
    if tier in ("A", "B") or scene in ("posed", "candid", "group"):
        return "ken_burns_in"
    return _deterministic_choice(path, [
        "ken_burns_in", "ken_burns_out", "ken_burns_left", "ken_burns_right", "static",
    ])


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def plan(
    cfg: Config,
    *,
    style: str = "upbeat",
    target_duration: int = 180,
    focus: str = "",
    planner: str = "algo",
    trip_type: str = "family",
    music_file: str | None = None,
    log_fn=None,
) -> tuple[EDL, int]:
    """Generate an EDL from preprocessed + analysis data."""
    _log = log_fn or print
    if trip_type not in TRIP_TYPES:
        _log(f"Unknown trip_type '{trip_type}', falling back to 'general'")
        trip_type = "general"

    effective_focus = focus or _default_focus(trip_type)
    preprocessed = json.loads((cfg.workspace / "preprocessed.json").read_text())
    analysis_items = json.loads((cfg.workspace / "analysis.json").read_text())
    analysis_by_id: dict[int, dict] = {a["id"]: a for a in analysis_items}

    if planner == "api":
        _log(f"Planning via Claude API (target {target_duration}s, style={style}, trip_type={trip_type})...")
        edl = _plan_api(cfg, preprocessed, analysis_by_id, analysis_items,
                        style=style, target_duration=target_duration,
                        focus=effective_focus, trip_type=trip_type, log_fn=_log)
    else:
        _log(f"Planning algorithmically (target {target_duration}s, style={style}, trip_type={trip_type})...")
        edl = _plan_auto(preprocessed, analysis_by_id,
                         style=style, target_duration=target_duration,
                         trip_type=trip_type)

    # Set metadata fields (API planner doesn't set these)
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
        _log("Music mode: auto (will generate in assemble step)")

    from .iterate import _find_latest_version, _save_edl
    version = _find_latest_version(cfg) + 1
    _save_edl(cfg, edl, version)

    clips_dir = cfg.workspace / "clips"
    if clips_dir.exists():
        for f in clips_dir.iterdir():
            f.unlink(missing_ok=True)

    _log(f"EDL v{version}: {len(edl.segments)} segments, "
         f"{len(edl.all_items())} items, ~{edl.estimated_duration():.0f}s")
    return edl, version


# ---------------------------------------------------------------------------
# Backend 1: Algorithmic planner — broken into clear phases
# ---------------------------------------------------------------------------

def _gather_candidates(
    preprocessed: dict, analysis_by_id: dict,
) -> list[Candidate]:
    """Phase 1a: Collect eligible candidates from all timeline chapters."""
    candidates: list[Candidate] = []
    for day in preprocessed["timeline"]:
        date_str = day["date"]
        for chapter in day["chapters"]:
            for item_id in chapter["item_ids"]:
                a = analysis_by_id.get(item_id)
                if not a or not a.get("vision") or a.get("tier") == "D":
                    continue
                v = a["vision"]
                if v.get("visual_quality", 5) < 5 or v.get("vlog_worthy") is False:
                    continue
                if v.get("story_beat", v.get("scene_type", "")) == "transport":
                    continue
                if _is_too_dark(Path(a.get("local_path", ""))):
                    continue
                candidates.append((a, chapter, date_str))
    return candidates


def _select_items(
    candidates: list[Candidate], trip_type: str, target_items: int,
) -> list[Candidate]:
    """Phase 1b: Select items — family mode keeps all A/B, others pick by score."""
    seen_descs: list[set[str]] = []

    if trip_type == "family":
        ab = [c for c in candidates if c[0].get("tier") in ("A", "B")]
        c_items = [c for c in candidates if c[0].get("tier") == "C"]

        # Dedup A/B by description similarity
        deduped_ab = []
        for a, ch, ds in ab:
            dw = _desc_words(a)
            if _is_desc_duplicate(dw, seen_descs):
                continue
            seen_descs.append(dw)
            deduped_ab.append((a, ch, ds))

        # Fill with best C items, spread across locations
        good_scenes = {"landmark", "nature", "food", "scenery"}
        c_items.sort(key=lambda x: (
            0 if x[0].get("vision", {}).get("scene_type", "") in good_scenes else 1,
            -_item_score(x[0], trip_type),
        ))
        remaining = max(0, target_items - len(deduped_ab))
        c_fill = []
        loc_counts: dict[str, int] = {}
        for a, ch, ds in c_items:
            if len(c_fill) >= remaining:
                break
            loc = ch.get("location", "unknown")
            if loc_counts.get(loc, 0) >= 3:
                continue
            dw = _desc_words(a)
            if _is_desc_duplicate(dw, seen_descs):
                continue
            seen_descs.append(dw)
            c_fill.append((a, ch, ds))
            loc_counts[loc] = loc_counts.get(loc, 0) + 1

        return deduped_ab + c_fill
    else:
        candidates.sort(key=lambda x: _item_score(x[0], trip_type), reverse=True)
        selected = []
        loc_counts: dict[str, int] = {}
        for a, ch, ds in candidates:
            if len(selected) >= target_items:
                break
            loc = ch.get("location", "unknown")
            if loc_counts.get(loc, 0) >= 5:
                continue
            dw = _desc_words(a)
            if _is_desc_duplicate(dw, seen_descs):
                continue
            seen_descs.append(dw)
            selected.append((a, ch, ds))
            loc_counts[loc] = loc_counts.get(loc, 0) + 1
        return selected


def _dedup_by_time_proximity(
    items: list[Candidate], trip_type: str, threshold: int = 30,
) -> list[Candidate]:
    """Remove items within `threshold` seconds at the same location, keeping best."""
    result: list[Candidate] = []
    for a, ch, ds in items:
        t = a.get("takentime", 0)
        loc = ch.get("location", "")
        skip = False
        for i, (prev_a, prev_ch, prev_ds) in enumerate(result):
            prev_t = prev_a.get("takentime", 0)
            if prev_ch.get("location", "") == loc and abs(t - prev_t) < threshold:
                if _item_score(a, trip_type) > _item_score(prev_a, trip_type):
                    result[i] = (a, ch, ds)  # replace with better
                skip = True
                break
        if not skip:
            result.append((a, ch, ds))
    return result


def _build_segments(
    selected: list[Candidate], trip_type: str, hero_files: set[str],
    base_dur: float, vary: float, seg_transition: str, seg_td: float,
) -> list[Segment]:
    """Phase 2: Build segments from selected items grouped by day/location."""
    first_date = selected[0][2] if selected else ""

    # Group by day
    day_groups: list[list[Candidate]] = []
    current_date = ""
    current_day: list[Candidate] = []
    for item in selected:
        if item[2] != current_date:
            if current_day:
                day_groups.append(current_day)
            current_day = []
            current_date = item[2]
        current_day.append(item)
    if current_day:
        day_groups.append(current_day)

    segments: list[Segment] = []
    seen_locations: set[str] = set()

    for day_idx, day_items in enumerate(day_groups):
        day_items.sort(key=lambda x: x[0].get("takentime", 0))

        current_edit_items: list[EditItem] = []
        current_location = ""
        date_str = day_items[0][2]
        is_first_of_day = True

        for a, ch, _ in day_items:
            location = ch["location"] if ch["location"] != "unknown" else ""

            # Flush segment on location change
            if location != current_location and current_edit_items:
                segments.append(Segment(
                    name=current_location or date_str,
                    items=current_edit_items,
                    transition=seg_transition, transition_duration=seg_td,
                ))
                current_edit_items = []

            current_location = location
            dur = _score_to_duration(a, trip_type, base_dur, vary)

            # Text overlay — human-readable dates, no repeat labels
            overlay = None
            if is_first_of_day:
                day_label = _format_day_label(date_str, first_date)
                if location and location not in seen_locations:
                    label = f"{day_label} \u00b7 {location}"
                    seen_locations.add(location)
                else:
                    label = day_label
                overlay = TextOverlay(text=label, position="bottom")
                is_first_of_day = False
            elif not current_edit_items and location and location not in seen_locations:
                overlay = TextOverlay(text=location, position="bottom")
                seen_locations.add(location)

            current_edit_items.append(EditItem(
                source_file=a["local_path"],
                media_type=a.get("media_type", "photo"),
                display_duration=round(dur, 1),
                effect=_choose_effect(a, trip_type, hero_files),
                text_overlay=overlay,
            ))

        if current_edit_items:
            segments.append(Segment(
                name=current_location or date_str,
                items=current_edit_items,
                transition=seg_transition, transition_duration=seg_td,
            ))

        # Fade between days
        if segments and day_idx < len(day_groups) - 1:
            segments[-1].transition = "fade_black"
            segments[-1].transition_duration = 1.0

    return segments


def _promote_best_opening(
    segments: list[Segment], analysis_by_id: dict, trip_type: str,
) -> None:
    """Swap the highest-scored item into the first position."""
    all_items = [item for seg in segments for item in seg.items]
    if len(all_items) < 3:
        return

    path_to_analysis = {a["local_path"]: a for a in analysis_by_id.values()
                        if "local_path" in a}

    best_idx = max(
        range(len(all_items)),
        key=lambda i: _item_score(path_to_analysis.get(all_items[i].source_file, {}), trip_type),
    )
    if best_idx == 0:
        return

    target_item = all_items[best_idx]
    first_items = segments[0].items
    for seg in segments:
        if target_item in seg.items:
            seg_idx = seg.items.index(target_item)
            first_items[0], seg.items[seg_idx] = seg.items[seg_idx], first_items[0]
            break


def _plan_auto(
    preprocessed: dict, analysis_by_id: dict,
    style: str, target_duration: int,
    trip_type: str = "family",
) -> EDL:
    """Deterministic EDL from analysis scores and timeline structure."""
    params = STYLE_PARAMS.get(style, STYLE_PARAMS["upbeat"])
    base_dur, vary = params["base_dur"], params["vary"]
    target_items = max(10, int(target_duration / base_dur))

    # Phase 1: Select items
    candidates = _gather_candidates(preprocessed, analysis_by_id)
    selected = _select_items(candidates, trip_type, target_items)
    selected.sort(key=lambda x: x[0].get("takentime", 0))
    selected = _dedup_by_time_proximity(selected, trip_type)

    # Identify hero shots (top 3)
    hero_files: set[str] = set()
    if len(selected) >= 5:
        ranked = sorted(selected, key=lambda x: _item_score(x[0], trip_type), reverse=True)
        hero_files = {a["local_path"] for a, _, _ in ranked[:3]}

    # Phase 2: Build segments
    segments = _build_segments(
        selected, trip_type, hero_files,
        base_dur, vary, params["transition"], params["td"],
    )

    # Boost hero durations
    for seg in segments:
        for item in seg.items:
            if item.source_file in hero_files:
                item.display_duration = round(base_dur + 3.0 * vary, 1)

    # Best item first
    _promote_best_opening(segments, analysis_by_id, trip_type)

    # Title & metadata
    is_family = trip_type == "family"
    if is_family:
        title = f"{preprocessed.get('family_names', ['Family'])[0]}'s Trip"
    else:
        title = _default_focus(trip_type).title()

    all_dates = sorted({ds for _, _, ds in selected})
    return EDL(
        title=title,
        target_duration=target_duration,
        segments=segments,
        trip_type=trip_type,
        style=style,
        intro_style="title_card",
        outro_style="fade_title",
        date_range=_format_date_range(all_dates) if all_dates else "",
    )


# ---------------------------------------------------------------------------
# Backend 2: Claude API planner
# ---------------------------------------------------------------------------

_NARRATIVE_GUIDANCE = {
    "family": """\
2. **Family is the heart**: Tier A items (2+ family members together) are the
   emotional core. Use their togetherness and genuine_emotion scores to find
   the most authentic moments — real laughter > posed smiles.""",
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


def _api_system_prompt(trip_type: str) -> str:
    """Build system prompt with trip-type-specific narrative guidance."""
    guidance = _NARRATIVE_GUIDANCE.get(trip_type, _NARRATIVE_GUIDANCE["general"])

    return f"""\
You are a professional travel vlog editor. You create emotionally resonant
highlight reels from trip photos by selecting and sequencing the best
moments into a cinematic narrative.

You will receive scored photo/video candidates organized by day/time/location.
Your job: select the best items and arrange them into an EDL (Edit Decision List).

## Narrative principles

1. **Emotional arc**: Build from curiosity (arrival) → joy (discoveries, meals,
   activities) → warmth (best moments) → nostalgia (departure). Every vlog
   should feel like a complete story.

{guidance}

3. **Rhythm and pacing**: Alternate between wide establishing shots and intimate
   moments. Vary display_duration: shorter for action/movement (3s), longer for
   emotional beats (5s). Use Ken Burns effects that match the mood — slow
   zoom-in for intimate moments, pan for landscapes.

4. **Visual storytelling**: Use story_beat and description to create variety —
   don't follow 3 similar shots with another of the same type. Each chapter
   should open with a scene-setter and close with its best moment.

5. **Text overlays**: Add location names when the setting changes and dates
   at the start of each new day. Keep text minimal — let the visuals speak.

## Technical rules

- display_duration: 3-5s per photo, up to 8s for video clips (vary for rhythm)
- Segments: one per location/chapter, 3-8 items each
- Transitions: crossfade within segments, fade_black between segments
- Skip chapters with no good candidates
- CRITICAL: source_file must be the EXACT local_path value from the input data

Output valid JSON only:
{{
  "title": "string",
  "target_duration": <seconds>,
  "resolution": [3840, 2160],
  "fps": 60,
  "segments": [
    {{
      "name": "Chapter Name",
      "items": [
        {{
          "source_file": "<exact local_path>",
          "media_type": "photo",
          "display_duration": 3.0-5.0,
          "effect": "ken_burns_in|ken_burns_out|ken_burns_left|ken_burns_right|static",
          "text_overlay": null or {{"text": "string", "position": "bottom", "font_size": 48}}
        }}
      ],
      "transition": "crossfade|fade_black",
      "transition_duration": 0.8
    }}
  ],
  "music": null
}}"""


def _plan_api(
    cfg: Config, preprocessed: dict, analysis_by_id: dict,
    analysis_items: list[dict],
    style: str, target_duration: int, focus: str,
    trip_type: str = "family", log_fn=None,
) -> EDL:
    """Use Claude API to generate an EDL with narrative awareness."""
    import anthropic

    _log = log_fn or print

    _log("Building chapters prompt from preprocessed + analysis data...")
    chapters_text = _build_chapters_prompt(preprocessed, analysis_by_id)
    _log(f"Chapters prompt: {len(chapters_text)} chars, {chapters_text.count(chr(10))} lines")

    # Trip-level summary
    days = preprocessed.get("timeline", [])
    locations: list[str] = []
    n_candidates = 0
    for day in days:
        for ch in day.get("chapters", []):
            loc = ch.get("location", "")
            if loc and loc != "unknown" and loc not in locations:
                locations.append(loc)
            n_candidates += ch.get("count", len(ch.get("item_ids", [])))

    trip_summary = (
        f"Trip overview: {len(days)} day{'s' if len(days) != 1 else ''}, "
        f"{len(locations)} locations, {n_candidates} candidate items.\n"
        f"Locations visited: {', '.join(locations)}"
    )
    _log(f"Trip summary: {len(days)} days, {len(locations)} locations, {n_candidates} candidates")

    n_items = target_duration // 4
    trip_label = f"{trip_type} trip" if trip_type != "general" else "trip"
    family_line = ""
    if trip_type == "family" and preprocessed.get("family_names"):
        family_line = f"\nFamily: {', '.join(preprocessed['family_names'])}"

    user_message = f"""\
Create a {style} {trip_label} vlog EDL.

{trip_summary}

**Brief**: {target_duration}s (~{target_duration // 60}m{target_duration % 60:02d}s) highlight reel.
Focus: {focus}.{family_line}
Select ~{n_items} items (at ~4s each = {n_items * 4}s).

**Scored candidates by day/location** (use the `path:` values as source_file):
{chapters_text}

Craft a narrative that tells the story of this trip — not just the best-scored
photos in order, but a sequence that builds emotion and feels like a journey."""

    system_prompt = _api_system_prompt(trip_type)

    _log(f"=== SYSTEM PROMPT ({len(system_prompt)} chars) ===")
    _log(system_prompt)
    _log(f"=== USER MESSAGE ({len(user_message)} chars) ===")
    _log(user_message)
    _log("=== END PROMPTS ===")

    _log("Calling Claude API (model=claude-sonnet-4-20250514, max_tokens=8192)...")
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8192,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )

    content = response.content[0].text
    _log(f"API response: {response.usage.input_tokens} in, {response.usage.output_tokens} out, "
         f"stop_reason={response.stop_reason}")

    _log(f"=== RAW API RESPONSE ({len(content)} chars) ===")
    _log(content)
    _log("=== END RESPONSE ===")

    from .media_utils import strip_markdown_fences
    content = strip_markdown_fences(content)

    _log("Parsing EDL from API response...")
    edl = EDL.model_validate_json(content)
    _log(f"Parsed EDL: {len(edl.segments)} segments, {len(edl.all_items())} items, "
         f"~{edl.estimated_duration():.0f}s estimated")
    return edl


# ---------------------------------------------------------------------------
# Prompt builder for API planner
# ---------------------------------------------------------------------------

def _format_item_line(a: dict, tier_prefix: str) -> str:
    """Format a single analysis item as a prompt line with metadata."""
    v = a["vision"]
    desc = v.get("description", "")[:80]
    media = a.get("media_type", "photo")
    cluster = a.get("cluster_size", 1)

    line = tier_prefix
    if media == "video":
        line += " (video)"
    if cluster > 1:
        line += f" (best of {cluster})"
    line += f" | {desc}"

    loc_detail = _location_detail(a)
    if loc_detail:
        line += f"\n      location: {loc_detail}"
    line += f"\n      path: {a['local_path']}"

    transcript = a.get("transcript", "")
    if transcript:
        line += f"\n      transcript: {transcript[:100]}"

    return line


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

            lines.append(f"\n  [{block.upper()}] {loc}")

            for a in ab_items:
                v = a["vision"]
                tog = v.get("togetherness", v.get("happiness_score", "?"))
                emo = v.get("genuine_emotion", "?")
                beat = v.get("story_beat", v.get("scene_type", "?"))
                qual = v.get("visual_quality", "?")
                prefix = (
                    f"    [{a.get('tier','?')}] fam={a.get('family_count',0)} "
                    f"tog={tog} emo={emo} qual={qual} beat={beat}"
                )
                lines.append(_format_item_line(a, prefix))

            c_items.sort(key=lambda x: x["vision"].get("visual_quality", 0), reverse=True)
            for a in c_items[:2]:
                v = a["vision"]
                scene = v.get("scene_type", "?")
                qual = v.get("visual_quality", "?")
                prefix = f"    [C] scene={scene} qual={qual}"
                lines.append(_format_item_line(a, prefix))

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
