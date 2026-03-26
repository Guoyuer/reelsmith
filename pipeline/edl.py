"""Edit Decision List — the central data model that flows between pipeline stages."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from ._types import PHOTO_EXTENSIONS, VIDEO_EXTENSIONS

if TYPE_CHECKING:
    from .config import Config


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
    STATIC = "static"
    NONE = "none"


class Transition(StrEnum):
    CROSSFADE = "crossfade"
    CUT = "cut"
    FADE_BLACK = "fade_black"
    WIPE_LEFT = "wipe_left"
    DISSOLVE = "dissolve"
    SMOOTHLEFT = "smoothleft"
    SMOOTHRIGHT = "smoothright"
    CIRCLECROP = "circlecrop"
    FADEWHITE = "fadewhite"


class SegmentTransition(StrEnum):
    FADE_BLACK = "fade_black"
    CROSSFADE = "crossfade"
    WIPE_LEFT = "wipe_left"
    DISSOLVE = "dissolve"
    CUT = "cut"
    FADEWHITE = "fadewhite"


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


class IntroStyle(StrEnum):
    TITLE_CARD = "title_card"
    NONE = "none"


class OutroStyle(StrEnum):
    FADE_TITLE = "fade_title"
    NONE = "none"


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
    segment_transition: SegmentTransition = SegmentTransition.FADE_BLACK
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
    music_duck_ratio: float = (
        0.3  # during speech: music volume *= this (0.0=silent, 1.0=full)
    )
    trip_type: str = "family"  # used by assemble for music generation prompt
    style: str = "upbeat"  # used by assemble for music generation prompt
    intro_style: IntroStyle = IntroStyle.TITLE_CARD
    intro_duration: float = 3.0  # seconds for intro clip
    outro_style: OutroStyle = OutroStyle.FADE_TITLE
    outro_duration: float = 3.0  # seconds for outro clip
    date_range: str = ""  # e.g. "June 13-16, 2025" for title card
    language: Language = Language.EN  # text language: en/cn/both

    def all_items(self) -> list[EditItem]:
        return [item for seg in self.segments for item in seg.items]

    @staticmethod
    def _item_output_duration(item: EditItem) -> float:
        """Actual output duration of a rendered clip (accounts for trim + speed)."""
        if item.start_time is not None and item.end_time is not None:
            source_dur = item.end_time - item.start_time
        else:
            source_dur = item.display_duration
        speed = item.playback_speed or 1.0
        return source_dur / speed

    def estimated_duration(self) -> float:
        total = sum(self._item_output_duration(item) for item in self.all_items())
        if self.intro_style != IntroStyle.NONE:
            total += self.intro_duration
        if self.outro_style != OutroStyle.NONE:
            total += self.outro_duration
        return total

    def summary(self) -> dict:
        """Return key stats for display/logging (avoids recomputing in multiple places)."""
        all_items = self.all_items()
        _dur = self._item_output_duration
        n_photos = sum(1 for i in all_items if i.media_type != "video")
        n_videos = sum(1 for i in all_items if i.media_type == "video")
        photo_time = sum(_dur(i) for i in all_items if i.media_type != "video")
        vid_time = sum(_dur(i) for i in all_items if i.media_type == "video")
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


def validate_edl(edl: EDL, *, strict: bool = True) -> list[dict]:
    """Validate an EDL for correctness before rendering.

    Returns a list of issue dicts with keys: level ("error"/"warning"), message.
    Empty list means the EDL is valid.

    When strict=True, warnings are promoted to errors.
    """
    issues: list[dict] = []

    def _error(msg: str) -> None:
        issues.append({"level": "error", "message": msg})

    def _warn(msg: str) -> None:
        issues.append({"level": "warning" if not strict else "error", "message": msg})

    # --- Top-level fields ---
    if not edl.title:
        _warn("EDL has no title")

    if edl.target_duration <= 0:
        _error(f"Invalid target_duration: {edl.target_duration}")

    if edl.intro_duration <= 0 or edl.intro_duration > 15:
        _error(f"Invalid intro_duration: {edl.intro_duration}")
    if edl.outro_duration <= 0 or edl.outro_duration > 15:
        _error(f"Invalid outro_duration: {edl.outro_duration}")
    if edl.music_duck_ratio < 0 or edl.music_duck_ratio > 1.0:
        _error(f"Invalid music_duck_ratio: {edl.music_duck_ratio}")

    # --- Segments ---
    if not edl.segments:
        _error("No segments")
        return issues

    total_display = 0.0
    all_sources: set[str] = set()
    total_items = 0

    for si, seg in enumerate(edl.segments):
        seg_label = f"seg[{si}] '{seg.name}'"

        if not seg.items:
            _error(f"{seg_label}: no items")
            continue

        if seg.transition != Transition.CUT:
            if seg.transition_duration <= 0 or seg.transition_duration > 3.0:
                _error(
                    f"{seg_label}: transition_duration {seg.transition_duration}s "
                    f"out of range (0, 3.0]"
                )

        for ii, item in enumerate(seg.items):
            item_label = f"{seg_label} item[{ii}]"
            total_items += 1

            # Source file existence
            src = Path(item.source_file)
            if not src.exists():
                _error(f"{item_label}: source file not found: {src}")

            # Duplicate check
            src_key = str(src.resolve()) if src.exists() else item.source_file
            if src_key in all_sources:
                _warn(f"{item_label}: duplicate source: {src.name}")
            all_sources.add(src_key)

            # Media type checks
            if item.media_type not in (MediaType.PHOTO, MediaType.VIDEO):
                _error(f"{item_label}: invalid media_type '{item.media_type}'")

            # Media type vs file extension mismatch
            if src.exists():
                ext = src.suffix.lower()
                photo_exts = PHOTO_EXTENSIONS | {".bmp", ".tiff"}
                video_exts = VIDEO_EXTENSIONS | {".webm", ".mts"}
                if item.media_type == "video" and ext in photo_exts:
                    _error(
                        f"{item_label}: media_type='video' but file is a photo ({ext})"
                    )
                elif item.media_type == "photo" and ext in video_exts:
                    _error(
                        f"{item_label}: media_type='photo' but file is a video ({ext})"
                    )

            # Duration
            if item.display_duration <= 0:
                _error(f"{item_label}: display_duration <= 0 ({item.display_duration})")
            elif item.display_duration < 2.0:
                _warn(
                    f"{item_label}: display_duration too short ({item.display_duration}s, min 2s)"
                )
            elif item.display_duration > 120:
                _warn(
                    f"{item_label}: display_duration very long ({item.display_duration}s)"
                )
            total_display += item.display_duration

            # Video-specific checks
            if item.media_type == "video":
                if item.effect not in (Effect.NONE, Effect.STATIC):
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
                        abs(trim_dur - item.display_duration) > 0.5
                        and item.playback_speed == 1.0
                    ):
                        _warn(
                            f"{item_label}: trim duration ({trim_dur:.1f}s) "
                            f"differs from display_duration ({item.display_duration:.1f}s)"
                        )
                if item.start_time is not None and item.start_time < 0:
                    _error(f"{item_label}: negative start_time ({item.start_time})")
                if item.playback_speed <= 0 or item.playback_speed > 4.0:
                    _error(
                        f"{item_label}: invalid playback_speed ({item.playback_speed})"
                    )

            # Photo-specific checks
            if item.media_type == "photo":
                if item.effect == "none":
                    _warn(f"{item_label}: photo with effect='none' will be static")
                if item.keep_audio:
                    _error(f"{item_label}: photo cannot have keep_audio=True")
                if item.start_time is not None or item.end_time is not None:
                    _error(f"{item_label}: photo should not have start_time/end_time")

            # Text overlay checks
            if item.text_overlay:
                if not item.text_overlay.text:
                    _warn(f"{item_label}: empty text overlay")
                if (
                    item.text_overlay.font_size <= 0
                    or item.text_overlay.font_size > 200
                ):
                    _error(
                        f"{item_label}: invalid font_size ({item.text_overlay.font_size})"
                    )

        # Check transition duration vs shortest clip in segment
        if seg.transition != Transition.CUT and len(seg.items) > 1:
            min_dur = min(it.display_duration for it in seg.items)
            if seg.transition_duration >= min_dur:
                _error(
                    f"{seg_label}: transition_duration ({seg.transition_duration}s) "
                    f">= shortest clip ({min_dur}s)"
                )

    # --- Global checks ---
    if total_items == 0:
        _error("EDL has no items")

    if total_display < 5:
        _warn(f"Total display duration very short: {total_display:.1f}s")

    estimated = edl.estimated_duration()
    if edl.target_duration > 0 and estimated > 0:
        ratio = estimated / edl.target_duration
        if ratio > 2.0:
            _warn(
                f"Estimated duration ({estimated:.0f}s) is >2x target ({edl.target_duration:.0f}s)"
            )
        elif ratio < 0.3:
            _warn(
                f"Estimated duration ({estimated:.0f}s) is <30% of target ({edl.target_duration:.0f}s)"
            )

    # Music checks
    if edl.music:
        if not Path(edl.music.file).exists():
            _warn(f"Music file not found: {edl.music.file}")
        if edl.music.volume < 0 or edl.music.volume > 1.0:
            _error(f"Music volume out of range: {edl.music.volume}")

    return issues
