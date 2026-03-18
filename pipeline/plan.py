"""Stage 3: Generate EDL — algorithmic or API-based planning.

Three backends:
  - "algo" (default): deterministic algorithm using analysis scores
  - "api": Claude API call for narrative-aware planning (text-only)
  - "visual": Claude API with photos — sees contact sheets, skips local vision model

Set via PlanConfig.planner or CLI --planner flag.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime
from pathlib import Path

from PIL import Image

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass  # HEIC support unavailable; handled by convert_heic fallback chain

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
    if isinstance(scene, list):
        scene_bonus = max((profile["scene_bonus"].get(s, 0) for s in scene), default=0)
    else:
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

    if planner == "visual":
        _log(f"Planning via Claude API with visual input (target {target_duration}s, style={style}, trip_type={trip_type})...")
        edl = _plan_visual(cfg, preprocessed, analysis_by_id, analysis_items,
                           style=style, target_duration=target_duration,
                           focus=effective_focus, trip_type=trip_type, log_fn=_log)
    elif planner == "api":
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
                # Skip photos with known issues (finger blocking, blurry, etc.)
                issues = v.get("issues", "")
                if issues and any(w in issues.lower() for w in ["finger", "blocked", "obstructed", "eyes closed"]):
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
        def _scene_match(item):
            st = item.get("vision", {}).get("scene_type", "")
            if isinstance(st, list):
                return any(s in good_scenes for s in st)
            return st in good_scenes
        c_items.sort(key=lambda x: (
            0 if _scene_match(x[0]) else 1,
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
Each item includes rich metadata: description, setting, mood, activity, quality
scores, time gaps between shots, and any issues (blurry, finger blocking, etc.).
Your job: select the best items and arrange them into an EDL (Edit Decision List).

## Understanding the input data

- **Tiers**: A = 2+ family members, B = 1 family member, C = scenery/no family
- **Scores**: togetherness (1-10), genuine_emotion (1-10), visual_quality (1-10)
- **Time gaps** like [+3m] or [+2h05m] show how much time passed since the
  previous photo — use these to feel the rhythm of the day and identify
  distinct moments vs burst shots
- **ISSUES field**: skip items with issues like "finger blocking", "eyes closed"
- **setting/mood/activity**: use these to craft narrative flow and avoid
  repetitive sequences (e.g., don't place 3 "posing at landmark" shots in a row)

## Narrative principles

1. **Emotional arc**: Build from curiosity (arrival) → joy (discoveries, meals,
   activities) → warmth (best moments) → nostalgia (departure). Every vlog
   should feel like a complete story.

{guidance}

3. **Rhythm and pacing**: Alternate between wide establishing shots and intimate
   moments. Use time gaps to identify natural scene breaks — a 2-hour gap means
   a completely different moment. Vary display_duration: shorter for action (3s),
   longer for emotional beats (5-6s). Match Ken Burns effects to content — slow
   zoom-in for intimate family moments, pan for landscapes and landmarks.

4. **Visual storytelling**: Use setting and mood fields to create visual variety.
   Don't follow 3 similar shots with another of the same type. Each segment
   should open with a scene-setter and close with its best moment. Prefer
   items where the mood field suggests strong atmosphere.

5. **Text overlays**: Add location names when the setting changes and day labels
   at the start of each new day. Use the setting field for more specific overlay
   text when appropriate (e.g., "Marina Bay at Golden Hour" vs just "Marina Bay").
   Keep text minimal — let the visuals speak.

6. **Smart selection**: You have access to ALL candidates. Be selective — skip
   mediocre items even if a chapter has few options. A shorter, tighter vlog
   beats one padded with weak shots. Skip items with ISSUES flags.

7. **Video moments**: Videos include per-scene breakdowns with timestamps,
   motion type, and descriptions. Select the best scene within a video using
   start_time and end_time. Motion types guide editing: "static" works for
   establishing shots, "smooth_motion"/"pan" for transitions, "handheld" for
   action energy. Don't use the whole video — pick the best moment.

8. **People**: When person names are listed, use them to reason about who
   appears where. Prioritize variety — show different family members, not
   the same person in every shot.

## Technical rules

- display_duration: 3-6s per photo, 4-10s for video clips (vary for rhythm)
- For video items: set start_time and end_time to select the best scene
- Segments: group by location or narrative beat, no fixed size limit
- Transitions: crossfade within segments, fade_black between major location/time changes
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
      "narrative_rationale": "Why these items were chosen and what story beat this segment serves",
      "items": [
        {{
          "source_file": "<exact local_path>",
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


def _claude_call(client, system: str, user: str | list[dict], log_fn,
                  label: str = "", thinking: bool = False):
    """Make a Claude API call. Returns (thinking_text, content, usage).

    *user* can be a plain string or a list of content blocks (for multimodal).
    *thinking*: enable extended thinking (adds cost but improves reasoning).
    """
    _log = log_fn or print
    _log(f"Calling Claude API ({label}{', thinking=on' if thinking else ''})...")

    kwargs: dict = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 16000,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    if thinking:
        kwargs["temperature"] = 1  # required for extended thinking
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": 10000}

    response = client.messages.create(**kwargs)

    thinking_text = ""
    content = ""
    for block in response.content:
        if block.type == "thinking":
            thinking_text = block.thinking
        elif block.type == "text":
            content = block.text

    _log(f"API response ({label}): {response.usage.input_tokens} in, "
         f"{response.usage.output_tokens} out, stop={response.stop_reason}")
    if thinking_text:
        _log(f"=== THINKING ({label}, {len(thinking_text)} chars) ===")
        _log(thinking_text)
        _log("=== END THINKING ===")

    return thinking_text, content, response.usage


def _plan_api(
    cfg: Config, preprocessed: dict, analysis_by_id: dict,
    analysis_items: list[dict],
    style: str, target_duration: int, focus: str,
    trip_type: str = "family", log_fn=None,
) -> EDL:
    """Multi-pass Claude API planning: narrative arc → shot selection → self-review."""
    import anthropic

    _log = log_fn or print
    client = anthropic.Anthropic()

    _log("Building chapters prompt from preprocessed + analysis data...")
    chapters_text = _build_chapters_prompt(preprocessed, analysis_by_id)
    _log(f"Chapters prompt: {len(chapters_text)} chars, {chapters_text.count(chr(10))} lines")

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
                n_candidates += 1
                a = analysis_by_id.get(item_id)
                if a and a.get("media_type") == "video":
                    n_videos += 1

    trip_summary = (
        f"Trip overview: {len(days)} day{'s' if len(days) != 1 else ''}, "
        f"{len(locations)} locations, {n_candidates} candidate items"
        f" ({n_videos} videos).\n"
        f"Locations visited: {', '.join(locations)}"
    )
    _log(f"Trip summary: {len(days)} days, {len(locations)} locations, "
         f"{n_candidates} candidates ({n_videos} videos)")

    n_items = target_duration // 4
    trip_label = f"{trip_type} trip" if trip_type != "general" else "trip"
    family_line = ""
    if trip_type == "family" and preprocessed.get("family_names"):
        family_line = f"\nFamily: {', '.join(preprocessed['family_names'])}"

    # ------------------------------------------------------------------
    # Pass 1: Narrative architect — design the story arc
    # ------------------------------------------------------------------
    _log("=== PASS 1: Narrative Arc Design ===")

    arc_system = f"""\
You are a professional travel vlog narrative designer. Given a trip's structure
(days, locations, photo/video counts, moods), design the emotional arc and
chapter structure for a {style} highlight reel.

Output JSON only:
{{
  "title": "vlog title",
  "arc_description": "1-2 sentences describing the overall narrative arc",
  "chapters": [
    {{
      "name": "Chapter Name",
      "theme": "emotional theme (e.g. 'excited arrival', 'peaceful morning', 'joyful discovery')",
      "target_items": <how many items to select>,
      "pacing": "fast|medium|slow",
      "transition_to_next": "how to transition to the next chapter"
    }}
  ]
}}"""

    # Build a lightweight summary for the arc pass (no full descriptions)
    arc_summary_lines = []
    for day in preprocessed["timeline"]:
        arc_summary_lines.append(f"\n=== {day['day_name']} {day['date']} ===")
        for chapter in day["chapters"]:
            loc = chapter["location"]
            block = chapter["time_block"]
            item_count = len(chapter.get("item_ids", []))
            # Gather mood summary
            moods = set()
            activities = set()
            for item_id in chapter["item_ids"]:
                a = analysis_by_id.get(item_id)
                if a and a.get("vision"):
                    m = a["vision"].get("mood", "")
                    if m:
                        moods.add(m)
                    act = a["vision"].get("activity", "")
                    if act:
                        activities.add(act)
            mood_str = ", ".join(list(moods)[:3]) if moods else "unknown"
            act_str = ", ".join(list(activities)[:3]) if activities else ""
            line = f"  [{block.upper()}] {loc} — {item_count} items, moods: {mood_str}"
            if act_str:
                line += f", activities: {act_str}"
            arc_summary_lines.append(line)

    arc_user = f"""\
Design the narrative arc for a {style} {trip_label} vlog.

{trip_summary}
{family_line}

**Brief**: {target_duration}s (~{target_duration // 60}m{target_duration % 60:02d}s) highlight reel.
Focus: {focus}.
Target ~{n_items} items total.

**Trip structure:**
{"".join(arc_summary_lines)}

Design chapters that build from curiosity → joy → warmth → nostalgia."""

    _, arc_content, _ = _claude_call(client, arc_system, arc_user, _log, "pass 1: arc")
    _log(f"=== ARC RESPONSE ===\n{arc_content}\n=== END ARC ===")

    from .media_utils import strip_markdown_fences
    arc_content = strip_markdown_fences(arc_content)
    try:
        narrative_arc = json.loads(arc_content)
    except json.JSONDecodeError:
        _log("Failed to parse narrative arc, proceeding with single-pass fallback")
        narrative_arc = None

    # ------------------------------------------------------------------
    # Pass 2: Shot selection — pick items using the narrative arc
    # ------------------------------------------------------------------
    _log("=== PASS 2: Shot Selection ===")

    system_prompt = _api_system_prompt(trip_type)

    arc_guidance = ""
    if narrative_arc:
        arc_guidance = f"""
**Narrative arc** (designed in previous pass — follow this structure):
Title: {narrative_arc.get('title', '')}
Arc: {narrative_arc.get('arc_description', '')}
Chapters:
"""
        for ch in narrative_arc.get("chapters", []):
            arc_guidance += (
                f"  - {ch.get('name', '?')}: theme={ch.get('theme', '?')}, "
                f"items={ch.get('target_items', '?')}, pacing={ch.get('pacing', '?')}, "
                f"transition={ch.get('transition_to_next', '?')}\n"
            )

    user_message = f"""\
Create a {style} {trip_label} vlog EDL.

{trip_summary}

**Brief**: {target_duration}s (~{target_duration // 60}m{target_duration % 60:02d}s) highlight reel.
Focus: {focus}.{family_line}
Select ~{n_items} items (at ~4s each = {n_items * 4}s).
{arc_guidance}
**Scored candidates by day/location** (use the `path:` values as source_file):
{chapters_text}

For videos with scenes listed, select the best scene using start_time/end_time.
Craft a narrative that tells the story of this trip — not just the best-scored
photos in order, but a sequence that builds emotion and feels like a journey."""

    _log(f"=== SYSTEM PROMPT ({len(system_prompt)} chars) ===")
    _log(system_prompt)
    _log(f"=== USER MESSAGE ({len(user_message)} chars) ===")
    _log(user_message)
    _log("=== END PROMPTS ===")

    _, edl_content, _ = _claude_call(client, system_prompt, user_message, _log, "pass 2: selection")

    _log(f"=== RAW EDL RESPONSE ({len(edl_content)} chars) ===")
    _log(edl_content)
    _log("=== END RESPONSE ===")

    edl_content = strip_markdown_fences(edl_content)

    _log("Parsing EDL from API response...")
    edl = EDL.model_validate_json(edl_content)
    _log(f"Parsed EDL: {len(edl.segments)} segments, {len(edl.all_items())} items, "
         f"~{edl.estimated_duration():.0f}s estimated")

    for seg in edl.segments:
        if seg.narrative_rationale:
            _log(f"  [{seg.name}] ({len(seg.items)} items): {seg.narrative_rationale}")

    # ------------------------------------------------------------------
    # Pass 3: Self-review — critique and refine
    # ------------------------------------------------------------------
    _log("=== PASS 3: Self-Review ===")

    review_system = """\
You are reviewing a vlog EDL that was just created. Critique it and output
an improved version. Check for:

1. **Redundancy**: Are there similar consecutive shots? Remove duplicates.
2. **Pacing**: Does duration vary enough? Fast moments should be 3s, emotional beats 5-6s.
3. **Story arc**: Does it build emotionally? Is the opening strong? Does the ending feel complete?
4. **Transitions**: Are fade_black transitions used between major scene changes?
5. **Text overlays**: Are day labels and location names placed at natural transition points?
6. **Video trim points**: Are start_time/end_time set for video items to select the best moment?
7. **Total duration**: Is it close to the target?

Output the improved EDL as valid JSON (same schema). Keep narrative_rationale updated."""

    review_user = f"""\
Review and improve this {style} {trip_label} vlog EDL.

Target duration: {target_duration}s. Focus: {focus}.{family_line}

Current EDL:
{edl.model_dump_json(indent=2)}

Improve pacing, remove redundancy, strengthen the narrative arc. Output improved JSON only."""

    _, review_content, _ = _claude_call(client, review_system, review_user, _log, "pass 3: review")

    _log(f"=== REVIEW RESPONSE ({len(review_content)} chars) ===")
    _log(review_content)
    _log("=== END REVIEW ===")

    review_content = strip_markdown_fences(review_content)
    try:
        reviewed_edl = EDL.model_validate_json(review_content)
        _log(f"Reviewed EDL: {len(reviewed_edl.segments)} segments, "
             f"{len(reviewed_edl.all_items())} items, "
             f"~{reviewed_edl.estimated_duration():.0f}s estimated")
        for seg in reviewed_edl.segments:
            if seg.narrative_rationale:
                _log(f"  [{seg.name}] ({len(seg.items)} items): {seg.narrative_rationale}")
        return reviewed_edl
    except Exception as e:
        _log(f"Review parse failed ({e}), using pass 2 EDL")
        return edl


# ---------------------------------------------------------------------------
# Prompt builder for API planner
# ---------------------------------------------------------------------------

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

    transcript = a.get("transcript", "")
    if transcript:
        line += f"\n      transcript: {transcript}"

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
# Backend 3: Visual planner — Claude sees photos directly
# ---------------------------------------------------------------------------

def _visual_system_prompt(trip_type: str) -> str:
    """System prompt for visual planner — Claude sees contact sheets and filmstrips."""
    guidance = _NARRATIVE_GUIDANCE.get(trip_type, _NARRATIVE_GUIDANCE["general"])
    return f"""\
You are a professional travel vlog editor. You will see the actual photos and
video filmstrips from a trip, organized as numbered contact sheets by day/location.

Your job: select the best items and arrange them into an EDL (Edit Decision List)
that tells a compelling story.

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
   a slideshow. Aim for 40-60% video content by screen time. Videos with speech or
   reactions (see transcript) are especially valuable.

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
        if not a or a.get("tier") == "D":
            continue

        local_path = a.get("local_path", "")
        media = a.get("media_type", "photo")
        tier = a.get("tier", "?")
        persons = a.get("persons", [])
        taken_iso = a.get("taken_iso", "")
        time_str = taken_iso[11:16] if taken_iso and len(taken_iso) >= 16 else ""

        label = f"#{idx:02d}"
        parts = [f"{label}: tier={tier}"]
        if a.get("family_count", 0):
            parts.append(f"fam={a['family_count']}")
        if persons:
            parts.append(f"people={','.join(persons[:3])}")
        if time_str:
            parts.append(f"time={time_str}")

        if media == "video":
            dur_ms = a.get("duration_ms")
            dur_s = f"{dur_ms / 1000:.0f}s" if dur_ms else "?"
            n_scenes = len(a.get("scenes", []))
            parts.append(f"video={dur_s} scenes={n_scenes}")
            transcript = a.get("transcript", "")
            if transcript:
                parts.append(f'transcript="{transcript[:100]}"')
            video_items.append(a)
        else:
            photo_paths.append(Path(local_path))

        parts.append(f"path={local_path}")
        lines.append(" ".join(parts))
        idx += 1

    loc = chapter.get("location", "unknown")
    block = chapter.get("time_block", "")
    header = f"\n=== {day['day_name']} {day['date']} [{block.upper()}] {loc} ==="
    text = header + "\n" + "\n".join(lines)
    return text, photo_paths, video_items


def _build_visual_content_blocks(
    preprocessed: dict, analysis_by_id: dict, cfg: Config, log_fn=None,
) -> list[dict]:
    """Build multimodal content blocks: interleaved text + contact sheets + filmstrips."""
    from .media_utils import make_contact_sheet, make_filmstrip

    _log = log_fn or print
    blocks: list[dict] = []
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

            blocks.append({"type": "text", "text": text})

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
                max_per_sheet = 28  # 7 rows × 4 cols = 1792px height, under 2000px
                sheet_idx = 0
                for chunk_start in range(0, len(thumb_paths), max_per_sheet):
                    chunk = thumb_paths[chunk_start:chunk_start + max_per_sheet]
                    chunk_labels = [f"#{global_idx + chunk_start + i:02d}" for i in range(len(chunk))]
                    s_path = sheets_dir / f"{sheet_name.replace('.jpg', '')}_{sheet_idx}.jpg" if len(thumb_paths) > max_per_sheet else sheet_path
                    make_contact_sheet(chunk, s_path, cell_size=256, columns=4, labels=chunk_labels)
                    _log(f"Contact sheet: {s_path.name} ({len(chunk)} photos)")

                    b64 = base64.b64encode(s_path.read_bytes()).decode()
                    blocks.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
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

                blocks.append({"type": "text", "text": f"Video filmstrip for #{global_idx + len(photo_paths) + video_items.index(vi):02d}:"})
                b64 = base64.b64encode(strip_path.read_bytes()).decode()
                blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
                })

            global_idx += n_items

    return blocks


def _build_review_blocks(edl: EDL, cfg: Config) -> list[dict]:
    """Build review content blocks: selected items at higher resolution."""
    from .media_utils import generate_thumbnail

    blocks: list[dict] = [
        {"type": "text", "text": f"Review this EDL. Current version:\n{edl.model_dump_json(indent=2)}"},
    ]
    review_dir = cfg.workspace / "review_thumbs"
    review_dir.mkdir(parents=True, exist_ok=True)

    for seg in edl.segments:
        blocks.append({"type": "text", "text": f"\n--- {seg.name} ({len(seg.items)} items) ---"})
        for item in seg.items:
            src = Path(item.source_file)
            if item.media_type == "video":
                # Use best scene keyframe for review
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
                b64 = base64.b64encode(thumb.read_bytes()).decode()
                blocks.append({"type": "text", "text": f"{item.source_file} ({item.display_duration}s)"})
                blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
                })

    return blocks


def _plan_visual(
    cfg: Config, preprocessed: dict, analysis_by_id: dict,
    analysis_items: list[dict],
    style: str, target_duration: int, focus: str,
    trip_type: str = "family", log_fn=None,
) -> EDL:
    """Multi-pass Claude API planning with visual input — Claude sees actual photos."""
    import anthropic

    _log = log_fn or print
    client = anthropic.Anthropic()

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
                if a and a.get("tier") != "D":
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
You are a professional travel vlog narrative designer. Design the emotional arc
and chapter structure for a {style} highlight reel.

Output JSON only:
{{
  "title": "vlog title",
  "arc_description": "1-2 sentences describing the overall narrative arc",
  "chapters": [
    {{
      "name": "Chapter Name",
      "theme": "emotional theme",
      "target_items": <count>,
      "pacing": "fast|medium|slow",
      "prefer_video": <true if video clips would enhance this chapter>
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

    _, arc_content, _ = _claude_call(client, arc_system, arc_user, _log, "visual pass 1: arc")
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

    _log("Building contact sheets and filmstrips...")
    content_blocks = _build_visual_content_blocks(preprocessed, analysis_by_id, cfg, _log)

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
    visual_message: list[dict] = [{"type": "text", "text": intro_text}] + content_blocks

    system_prompt = _visual_system_prompt(trip_type)
    _log(f"Visual message: {len(visual_message)} content blocks")

    _, edl_content, _ = _claude_call(client, system_prompt, visual_message, _log, "visual pass 2: select")

    _log(f"=== VISUAL EDL RESPONSE ({len(edl_content)} chars) ===")
    _log(edl_content[:1000])
    _log("=== END ===")

    edl_content = strip_markdown_fences(edl_content)
    edl = EDL.model_validate_json(edl_content)
    _log(f"Parsed EDL: {len(edl.segments)} segments, {len(edl.all_items())} items, "
         f"~{edl.estimated_duration():.0f}s")
    for seg in edl.segments:
        _log(f"  [{seg.name}] ({len(seg.items)} items) music={seg.music_mood} | {seg.narrative_rationale}")

    # ------------------------------------------------------------------
    # Pass 3: Visual review — Claude reviews selected items at higher res
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

Output the improved EDL as valid JSON (same schema). Update narrative_rationale
and music_mood if you change anything."""

    review_blocks = _build_review_blocks(edl, cfg)
    _, review_content, _ = _claude_call(client, review_system, review_blocks, _log, "visual pass 3: review")

    review_content = strip_markdown_fences(review_content)
    try:
        reviewed = EDL.model_validate_json(review_content)
        _log(f"Reviewed EDL: {len(reviewed.segments)} segments, {len(reviewed.all_items())} items, "
             f"~{reviewed.estimated_duration():.0f}s")
        for seg in reviewed.segments:
            _log(f"  [{seg.name}] ({len(seg.items)} items) music={seg.music_mood}")
        return reviewed
    except Exception as e:
        _log(f"Review parse failed ({e}), using pass 2 EDL")
        return edl


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
