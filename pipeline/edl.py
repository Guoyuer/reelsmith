"""Edit Decision List — the central data model that flows between pipeline stages."""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from ._types import PHOTO_EXTENSIONS, VIDEO_EXTENSIONS

if TYPE_CHECKING:
    from .config import Config


# ---------------------------------------------------------------------------
# Validation thresholds
# ---------------------------------------------------------------------------

# Title cards beyond 15s stall pacing — YouTube analytics show drop-off
# spikes when intros exceed ~12s; 15s leaves a small creative buffer.
_MAX_INTRO_DURATION = 15  # seconds

# Transitions longer than 3s feel sluggish; 1-2s is the sweet spot for
# crossfades.  3s is a generous upper bound that still feels intentional.
_MAX_TRANSITION_DURATION = 3.0  # seconds

# Below 2s the viewer can't register a photo; Ken Burns needs at least
# ~1.5s to be perceptible, so 2s is the hard floor.
_MIN_DISPLAY_DURATION = 2.0  # seconds; items shorter than this get a warning

# A single item over 2 minutes almost always means Gemini hallucinated
# a huge trim window rather than intentional slow cinema.
_WARN_DISPLAY_DURATION = 120  # seconds; items longer than this get a warning

# At 4K the drawtext filter renders at pixel size; 200px keeps text
# readable without dominating the frame on any target resolution.
_MAX_FONT_SIZE = 200  # pixels

# 2x target means Gemini wildly over-selected — budget math is broken.
# 0.3x means post-processing stripped most items — Gemini output is junk.
# Both thresholds trigger early abort to avoid wasting a render cycle.
_DURATION_RATIO_WARN = 2.0  # warn if estimated > 2x target
_DURATION_RATIO_FAIL = 0.3  # fail if estimated < 30% of target

# Trim-vs-display mismatches under 0.5s come from rounding in the
# preview→local timestamp conversion; larger gaps signal real errors.
_TRIM_TOLERANCE = 0.5  # seconds; trim vs display mismatch tolerance

# An EDL under 5s total is clearly degenerate (e.g. a single 3s photo).
_MIN_TOTAL_DISPLAY = 5  # seconds; minimum total display duration

# FFmpeg atempo filter chains cap out around 4x before audio artifacts
# become severe; also >4x rarely looks intentional in a travel vlog.
_MAX_PLAYBACK_SPEED = 4.0


# ---------------------------------------------------------------------------
# Enumerations — single source of truth for valid string values
# ---------------------------------------------------------------------------


class MediaType(StrEnum):
    PHOTO = "photo"
    VIDEO = "video"


class Effect(StrEnum):
    KEN_BURNS_IN = "ken_burns_in"
    KEN_BURNS_OUT = "ken_burns_out"
    KEN_BURNS_LEFT = "ken_burns_left"
    KEN_BURNS_RIGHT = "ken_burns_right"
    NONE = "none"


class Transition(StrEnum):
    CROSSFADE = "crossfade"
    CUT = "cut"
    FADE_BLACK = "fade_black"


class ColorTemp(StrEnum):
    WARM = "warm"
    COOL = "cool"
    NEUTRAL = "neutral"


class SegmentMode(StrEnum):
    NARRATIVE = "narrative"
    MONTAGE = "montage"


class OverlayPosition(StrEnum):
    TOP = "top"
    CENTER = "center"
    BOTTOM = "bottom"


class MusicMode(StrEnum):
    NONE = "none"
    AUTO = "auto"
    FILE = "file"


class Language(StrEnum):
    EN = "en"
    CN = "cn"
    BOTH = "both"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class TextOverlay(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    text: str
    position: OverlayPosition = OverlayPosition.BOTTOM
    font_size: int = 48


class EditItem(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    source_file: str
    media_type: MediaType
    start_time: float | None = None  # video trim start (seconds)
    end_time: float | None = None  # video trim end (seconds)
    display_duration: float = 4.0  # how long this item is on screen
    keep_audio: bool = (
        False  # preserve original audio (Gemini decides from video clips)
    )
    playback_speed: float = 1.0  # 0.5=slow-mo, 1.0=normal, 1.5=fast (Gemini decides)
    effect: Effect = Effect.KEN_BURNS_IN
    text_overlay: TextOverlay | None = None


class Segment(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    name: str  # e.g. "Opening", "Marina Bay", "Hawker Food"
    narrative_rationale: str = (
        ""  # why these items, what story beat this segment serves
    )
    music_mood: str = ""  # e.g. "warm acoustic guitar, uplifting" → Lyria per-segment
    items: list[EditItem]
    # Intra-segment transition (between items within this segment)
    transition: Transition = Transition.CROSSFADE
    transition_duration: float = 0.4  # seconds
    # Inter-segment transition (how this segment starts, from previous segment)
    segment_transition: Transition = Transition.FADE_BLACK
    segment_transition_duration: float = 1.0  # seconds
    mode: SegmentMode = SegmentMode.NARRATIVE  # montage = quick-cut burst
    color_temp: ColorTemp = ColorTemp.NEUTRAL  # Gemini sets per segment


class MusicTrack(BaseModel):
    file: str
    volume: float = 0.40  # 0.0–1.0 (ducked dynamically by sidechaincompress)
    fade_in: float = 2.0
    fade_out: float = 3.0


class EDL(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    title: str
    target_duration: float  # desired total length in seconds
    segments: list[Segment]
    music: MusicTrack | None = None
    music_mode: MusicMode = MusicMode.NONE  # auto = generate in generate_music step
    trip_type: str  # set by orchestrator from PlanConfig
    style: str  # set by orchestrator from PlanConfig
    intro_duration: float = 3.0  # seconds for intro title card
    outro_duration: float = 3.0  # seconds for outro title card
    date_range: str = ""  # e.g. "June 13-16, 2025" for title card
    language: Language = Language.EN  # text language: en/cn/both

    def all_items(self) -> list[EditItem]:
        return [item for seg in self.segments for item in seg.items]

    def estimated_duration(self) -> float:
        total = sum(item.display_duration for item in self.all_items())
        total += self.intro_duration + self.outro_duration
        return total

    def summary(self) -> dict:
        """Return key stats for display/logging (avoids recomputing in multiple places)."""
        all_items = self.all_items()
        n_photos = sum(1 for i in all_items if i.media_type != "video")
        n_videos = sum(1 for i in all_items if i.media_type == "video")
        photo_time = sum(
            i.display_duration for i in all_items if i.media_type != "video"
        )
        vid_time = sum(i.display_duration for i in all_items if i.media_type == "video")
        total_time = photo_time + vid_time
        return {
            "n_photos": n_photos,
            "n_videos": n_videos,
            "n_keep_audio": sum(1 for i in all_items if i.keep_audio),
            "n_text_overlay": sum(1 for i in all_items if i.text_overlay),
            "photo_time": photo_time,
            "vid_time": vid_time,
            "vid_pct": int(vid_time / total_time * 100) if total_time > 0 else 0,
            "estimated_duration": self.estimated_duration(),
        }


# ---------------------------------------------------------------------------
# EDL persistence helpers
# ---------------------------------------------------------------------------


def find_latest_version(cfg: Config) -> int:
    """Find the latest version number from edl_v*.json files."""
    versions = []
    for f in cfg.workspace.glob("edl_v*.json"):
        try:
            versions.append(int(f.stem.split("_v")[1]))
        except (IndexError, ValueError):
            pass
    return max(versions) if versions else 0


def save_edl(cfg: Config, edl: EDL, version: int) -> Path:
    """Save EDL as workspace/edl_v{version}.json."""
    path = cfg.edl_path(version)
    path.write_text(edl.model_dump_json(indent=2))
    return path


def load_latest_edl(cfg: Config) -> tuple[EDL, int]:
    """Load the latest edl_v{N}.json."""
    version = find_latest_version(cfg)
    if version == 0:
        raise FileNotFoundError(f"No EDL found in {cfg.workspace}")
    path = cfg.edl_path(version)
    return EDL.model_validate_json(path.read_text()), version


# ---------------------------------------------------------------------------
# EDL quality validation
# ---------------------------------------------------------------------------


def _issue_reporters(
    issues: list[dict], strict: bool
) -> tuple[Callable[[str], None], Callable[[str], None]]:
    """Create _error/_warn closures that append to *issues*."""

    def _error(msg: str) -> None:
        issues.append({"level": "error", "message": msg})

    def _warn(msg: str) -> None:
        issues.append({"level": "warning" if not strict else "error", "message": msg})

    return _error, _warn


def _validate_top_level(edl: EDL, issues: list[dict], strict: bool) -> None:
    """Validate top-level EDL fields (title, target_duration, intro/outro)."""
    _error, _warn = _issue_reporters(issues, strict)

    if not edl.title:
        _warn("EDL has no title")

    if edl.target_duration <= 0:
        _error(f"Invalid target_duration: {edl.target_duration}")

    if edl.intro_duration <= 0 or edl.intro_duration > _MAX_INTRO_DURATION:
        _error(f"Invalid intro_duration: {edl.intro_duration}")
    if edl.outro_duration <= 0 or edl.outro_duration > _MAX_INTRO_DURATION:
        _error(f"Invalid outro_duration: {edl.outro_duration}")


def _validate_item(
    item: EditItem, item_label: str, issues: list[dict], strict: bool
) -> float:
    """Validate a single EditItem. Returns item.display_duration for accumulation."""
    _error, _warn = _issue_reporters(issues, strict)

    src = Path(item.source_file)
    if item.media_type not in (MediaType.PHOTO, MediaType.VIDEO):
        _error(f"{item_label}: invalid media_type '{item.media_type}'")

    if src.exists():
        ext = src.suffix.lower()
        if item.media_type == "video" and ext in PHOTO_EXTENSIONS:
            _error(f"{item_label}: media_type='video' but file is a photo ({ext})")
        elif item.media_type == "photo" and ext in VIDEO_EXTENSIONS:
            _error(f"{item_label}: media_type='photo' but file is a video ({ext})")

    if item.display_duration <= 0:
        _error(f"{item_label}: display_duration <= 0 ({item.display_duration})")
    elif item.display_duration < _MIN_DISPLAY_DURATION:
        _warn(
            f"{item_label}: display_duration too short ({item.display_duration}s, min {_MIN_DISPLAY_DURATION}s)"
        )
    elif item.display_duration > _WARN_DISPLAY_DURATION:
        _warn(f"{item_label}: display_duration very long ({item.display_duration}s)")

    if item.media_type == "video":
        if item.effect != Effect.NONE:
            _error(
                f"{item_label}: video should have effect='none', "
                f"got '{item.effect}'"
            )
        if item.start_time is not None and item.end_time is not None:
            if item.start_time >= item.end_time:
                _error(
                    f"{item_label}: start_time ({item.start_time}) "
                    f">= end_time ({item.end_time})"
                )
            trim_dur = item.end_time - item.start_time
            if (
                abs(trim_dur - item.display_duration) > _TRIM_TOLERANCE
                and item.playback_speed == 1.0
            ):
                _warn(
                    f"{item_label}: trim duration ({trim_dur:.1f}s) "
                    f"differs from display_duration ({item.display_duration:.1f}s)"
                )
        if item.start_time is not None and item.start_time < 0:
            _error(f"{item_label}: negative start_time ({item.start_time})")
        if item.playback_speed <= 0 or item.playback_speed > _MAX_PLAYBACK_SPEED:
            _error(f"{item_label}: invalid playback_speed ({item.playback_speed})")

    if item.media_type == "photo":
        if item.effect == Effect.NONE:
            _warn(f"{item_label}: photo with effect='none' will be static")
        if item.keep_audio:
            _error(f"{item_label}: photo cannot have keep_audio=True")
        if item.start_time is not None or item.end_time is not None:
            _error(f"{item_label}: photo should not have start_time/end_time")

    if item.text_overlay:
        if not item.text_overlay.text:
            _warn(f"{item_label}: empty text overlay")
        if (
            item.text_overlay.font_size <= 0
            or item.text_overlay.font_size > _MAX_FONT_SIZE
        ):
            _error(f"{item_label}: invalid font_size ({item.text_overlay.font_size})")

    return item.display_duration


def _validate_segments(edl: EDL, issues: list[dict], strict: bool) -> tuple[float, int]:
    """Validate all segments and their items. Returns (total_display, total_items)."""
    _error, _warn = _issue_reporters(issues, strict)

    total_display = 0.0
    all_sources: set[str] = set()
    total_items = 0

    for si, seg in enumerate(edl.segments):
        seg_label = f"seg[{si}] '{seg.name}'"

        if not seg.items:
            _error(f"{seg_label}: no items")
            continue

        if seg.transition != Transition.CUT:
            if (
                seg.transition_duration <= 0
                or seg.transition_duration > _MAX_TRANSITION_DURATION
            ):
                _error(
                    f"{seg_label}: transition_duration {seg.transition_duration}s "
                    f"out of range (0, {_MAX_TRANSITION_DURATION}]"
                )

        for ii, item in enumerate(seg.items):
            item_label = f"{seg_label} item[{ii}]"
            total_items += 1

            # Source file existence + duplicate check (needs cross-item state)
            src = Path(item.source_file)
            if not src.exists():
                _error(f"{item_label}: source file not found: {src}")

            src_key = str(src.resolve()) if src.exists() else item.source_file
            if src_key in all_sources:
                _warn(f"{item_label}: duplicate source: {src.name}")
            all_sources.add(src_key)

            total_display += _validate_item(item, item_label, issues, strict)

        # Check transition duration vs shortest clip in segment
        if seg.transition != Transition.CUT and len(seg.items) > 1:
            min_dur = min(it.display_duration for it in seg.items)
            if seg.transition_duration >= min_dur:
                _error(
                    f"{seg_label}: transition_duration ({seg.transition_duration}s) "
                    f">= shortest clip ({min_dur}s)"
                )

    return total_display, total_items


def _validate_global(
    edl: EDL,
    total_display: float,
    total_items: int,
    issues: list[dict],
    strict: bool,
) -> None:
    """Validate global constraints (totals, duration ratio, music)."""
    _error, _warn = _issue_reporters(issues, strict)

    if total_items == 0:
        _error("EDL has no items")

    if total_display < _MIN_TOTAL_DISPLAY:
        _warn(f"Total display duration very short: {total_display:.1f}s")

    estimated = edl.estimated_duration()
    if edl.target_duration > 0 and estimated > 0:
        ratio = estimated / edl.target_duration
        if ratio > _DURATION_RATIO_WARN:
            _warn(
                f"Estimated duration ({estimated:.0f}s) is >{_DURATION_RATIO_WARN}x target ({edl.target_duration:.0f}s)"
            )
        elif ratio < _DURATION_RATIO_FAIL:
            _warn(
                f"Estimated duration ({estimated:.0f}s) is <{int(_DURATION_RATIO_FAIL * 100)}% of target ({edl.target_duration:.0f}s)"
            )

    # Music checks
    if edl.music:
        if not Path(edl.music.file).exists():
            _warn(f"Music file not found: {edl.music.file}")
        if edl.music.volume < 0 or edl.music.volume > 1.0:
            _error(f"Music volume out of range: {edl.music.volume}")


def validate_edl(edl: EDL, *, strict: bool = True) -> list[dict]:
    """Validate an EDL for correctness before rendering.

    Returns a list of issue dicts with keys: level ("error"/"warning"), message.
    Empty list means the EDL is valid.

    When strict=True, warnings are promoted to errors.
    """
    issues: list[dict] = []

    _validate_top_level(edl, issues, strict)

    if not edl.segments:
        issues.append({"level": "error", "message": "No segments"})
        return issues

    total_display, total_items = _validate_segments(edl, issues, strict)
    _validate_global(edl, total_display, total_items, issues, strict)

    return issues
