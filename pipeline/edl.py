"""Edit Decision List — the central data model that flows between pipeline stages."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

if TYPE_CHECKING:
    from .config import Config


class TextOverlay(BaseModel):
    text: str
    position: Literal["top", "center", "bottom"] = "bottom"
    font_size: int = 48


class EditItem(BaseModel):
    source_file: str
    media_type: Literal["photo", "video"]
    start_time: float | None = None  # video trim start (seconds)
    end_time: float | None = None  # video trim end (seconds)
    display_duration: float = 4.0  # how long this item is on screen
    keep_audio: bool = False  # preserve original audio (set by audio assessment)
    transcript: str = ""  # speech transcript (set by audio assessment)
    playback_speed: float = 1.0  # 0.5=slow-mo, 1.0=normal, 1.5=fast (Gemini decides)
    effect: Literal[
        "ken_burns_in",
        "ken_burns_out",
        "ken_burns_left",
        "ken_burns_right",
        "static",
        "none",
    ] = "ken_burns_in"
    text_overlay: TextOverlay | None = None


class Segment(BaseModel):
    name: str  # e.g. "Opening", "Marina Bay", "Hawker Food"
    narrative_rationale: str = ""  # why these items, what story beat this segment serves
    music_mood: str = ""  # e.g. "warm acoustic guitar, uplifting" → MusicGen
    items: list[EditItem]
    transition: Literal["crossfade", "cut", "fade_black", "wipe_left",
                        "dissolve", "smoothleft", "smoothright", "circlecrop"] = "crossfade"
    transition_duration: float = 0.4  # seconds
    mode: Literal["narrative", "montage"] = "narrative"  # montage = quick-cut burst
    color_temp: Literal["warm", "cool", "neutral"] = "neutral"  # Gemini sets per segment


class MusicTrack(BaseModel):
    file: str
    volume: float = 0.15  # 0.0–1.0
    fade_in: float = 2.0
    fade_out: float = 3.0


class EDL(BaseModel):
    title: str
    target_duration: float  # desired total length in seconds
    resolution: tuple[int, int] = (3840, 2160)
    fps: int = 60
    segments: list[Segment]
    music: MusicTrack | None = None
    music_mode: Literal["none", "auto", "file"] = "none"  # auto = generate in generate_music step
    trip_type: str = "family"  # used by assemble for music generation prompt
    style: str = "upbeat"  # used by assemble for music generation prompt
    intro_style: Literal["title_card", "highlight_montage", "none"] = "title_card"
    outro_style: Literal["fade_title", "last_hero", "none"] = "fade_title"
    date_range: str = ""  # e.g. "June 13-16, 2025" for title card

    def all_items(self) -> list[EditItem]:
        return [item for seg in self.segments for item in seg.items]

    def estimated_duration(self) -> float:
        total = sum(item.display_duration for item in self.all_items())
        transitions = sum(
            seg.transition_duration * max(0, len(seg.items) - 1)
            for seg in self.segments
            if seg.transition != "cut"
        )
        # Add intro/outro time estimates
        if self.intro_style == "title_card":
            total += 3.0
        elif self.intro_style == "highlight_montage":
            total += 5.0
        if self.outro_style in ("fade_title", "last_hero"):
            total += 3.0
        return total - transitions


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
    path = cfg.workspace / f"edl_v{version}.json"
    path.write_text(edl.model_dump_json(indent=2))
    return path


def load_latest_edl(cfg: Config) -> tuple[EDL, int]:
    """Load the latest edl_v{N}.json. Falls back to edl.json for migration."""
    version = find_latest_version(cfg)
    if version > 0:
        path = cfg.workspace / f"edl_v{version}.json"
    else:
        path = cfg.workspace / "edl.json"
        if not path.exists():
            raise FileNotFoundError(f"No EDL found in {cfg.workspace}")
        version = 1
    return EDL.model_validate_json(path.read_text()), version
