"""Edit Decision List — the central data model that flows between pipeline stages."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


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
    items: list[EditItem]
    transition: Literal["crossfade", "cut", "fade_black", "wipe_left"] = "crossfade"
    transition_duration: float = 0.8  # seconds
    mode: Literal["narrative", "montage"] = "narrative"  # montage = quick-cut burst


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
