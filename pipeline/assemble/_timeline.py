"""Timeline — single source of truth for all clip timing in the assembled vlog.

Every consumer (speech audio, music ducking, chapter markers) reads
from the same Timeline object. Clips are sequential with no overlap.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from ._encoder import RenderContext

logger = logging.getLogger("vlog.assemble.timeline")


@dataclass
class TimelineEntry:
    """A single clip's position in the final video."""

    index: int
    path: Path
    actual_duration: float
    edl_duration: float
    keep_audio: bool

    video_offset: float = 0.0
    end_time: float = 0.0


@dataclass
class Timeline:
    """Ordered list of clips with computed timing.

    Build once from all_clips, then use for all offset-dependent operations:
    - speech track: video_offset for keep_audio clips
    - music ducking: speech_ranges()
    - chapter markers: chapter_offsets()
    """

    entries: list[TimelineEntry] = field(default_factory=list)

    @staticmethod
    def build(all_clips: list[dict], ctx: RenderContext | None = None) -> Timeline:
        """Compute timeline from clip list. Sequential — each clip starts where the previous ends."""
        tl = Timeline()
        offset = 0.0
        for i, clip in enumerate(all_clips):
            dur = (ctx.probe_duration(clip["path"]) if ctx else 0.0) or clip["duration"]
            entry = TimelineEntry(
                index=i,
                path=clip["path"],
                actual_duration=dur,
                edl_duration=clip["duration"],
                keep_audio=clip.get("keep_audio", False),
                video_offset=offset,
                end_time=offset + dur,
            )
            tl.entries.append(entry)
            offset += dur
        return tl

    def total_duration(self) -> float:
        if not self.entries:
            return 0.0
        return self.entries[-1].end_time

    def speech_entries(self) -> list[TimelineEntry]:
        return [e for e in self.entries if e.keep_audio]

    def speech_ranges(self) -> list[tuple[float, float]]:
        """Time ranges where speech audio plays (for music ducking)."""
        return [(e.video_offset, e.end_time) for e in self.speech_entries() if e.end_time > e.video_offset]

    def chapter_offsets(self, edl, all_clips: list[dict]) -> list[tuple[float, str]]:
        """Chapter timestamps for YouTube markers."""
        chapters = []
        clip_idx = 0
        if edl.intro_style != "none" and self.entries:
            clip_idx = 1
        for seg in edl.segments:
            if clip_idx < len(self.entries):
                chapters.append((self.entries[clip_idx].video_offset, seg.name))
            clip_idx += len(seg.items)
        return chapters

    def dump(self) -> None:
        """Print the full timeline for debugging."""
        logger.info("=== Timeline ===")
        logger.info(f"{'idx':>5s} {'offset':>8s} {'end':>8s} {'dur':>6s} {'ka':>3s}  clip")
        logger.info("-" * 60)
        for e in self.entries:
            ka = "YES" if e.keep_audio else ""
            logger.info(
                f"{e.index:5d} {e.video_offset:8.2f} {e.end_time:8.2f} "
                f"{e.actual_duration:6.2f} {ka:>3s}  {e.path.name}"
            )
        logger.info(f"Total: {self.total_duration():.2f}s")
        if self.speech_entries():
            ranges = ", ".join(f"{s:.1f}-{e:.1f}s" for s, e in self.speech_ranges())
            logger.info(f"Speech: {ranges}")
        logger.info("=== End Timeline ===")
