"""Timeline — single source of truth for all clip timing in the assembled vlog.

Every consumer (xfade, speech audio, music ducking, chapter markers) reads
from the same Timeline object. No more independent offset calculations.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from .concat import partition_into_groups
from .encoder import probe_duration as _probe_dur

logger = logging.getLogger("vlog.timeline")


@dataclass
class TimelineEntry:
    """A single clip's position in the final video."""
    index: int                  # position in all_clips list
    path: Path                  # rendered clip file
    actual_duration: float      # probed duration of the clip file
    edl_duration: float         # EDL-requested duration
    transition: str             # "cut", "crossfade", "fade_black", etc.
    transition_duration: float  # overlap with previous clip
    keep_audio: bool            # preserve original audio from this clip

    # Computed by Timeline.build()
    video_offset: float = 0.0          # when xfade starts blending this clip in
    visible_offset: float = 0.0        # when clip is FULLY visible (after transition)
    end_time: float = 0.0              # when clip ends in the timeline


@dataclass
class Timeline:
    """Ordered list of clips with computed timing.

    Build once from all_clips, then use for all offset-dependent operations:
    - concat_xfade: video_offset per entry
    - speech track: visible_offset for keep_audio clips
    - music ducking: speech_ranges()
    - chapter markers: chapter_offsets()
    """
    entries: list[TimelineEntry] = field(default_factory=list)

    @staticmethod
    def build(all_clips: list[dict]) -> "Timeline":
        """Compute timeline from clip metadata (mathematical estimate).

        Uses probed clip durations and transition_duration to compute offsets.
        For more accurate results after rendering, use build_actual().
        """
        tl = Timeline()

        for i, clip in enumerate(all_clips):
            dur = _probe_dur(clip["path"]) or clip["duration"]
            td = clip.get("transition_duration", 0.0)
            tr = clip.get("transition", "cut")
            if tr == "cut":
                td = 0.0

            tl.entries.append(TimelineEntry(
                index=i,
                path=clip["path"],
                actual_duration=dur,
                edl_duration=clip["duration"],
                transition=tr,
                transition_duration=td,
                keep_audio=clip.get("keep_audio", False),
            ))

        tl._compute_offsets()
        return tl

    @staticmethod
    def build_actual(all_clips: list[dict], output_dir: Path) -> "Timeline":
        """Compute timeline using MEASURED group file durations.

        After concatenate() renders group files (_group_0.mp4, etc.),
        this reads their actual durations instead of estimating mathematically.
        This fixes speech sync drift caused by xfade frame rounding.
        """
        tl = Timeline()

        for i, clip in enumerate(all_clips):
            dur = _probe_dur(clip["path"]) or clip["duration"]
            td = clip.get("transition_duration", 0.0)
            tr = clip.get("transition", "cut")
            if tr == "cut":
                td = 0.0

            tl.entries.append(TimelineEntry(
                index=i,
                path=clip["path"],
                actual_duration=dur,
                edl_duration=clip["duration"],
                transition=tr,
                transition_duration=td,
                keep_audio=clip.get("keep_audio", False),
            ))

        tl._compute_offsets_actual(output_dir)
        return tl

    def _compute_offsets(self) -> None:
        """Compute offsets from clip durations (mathematical estimate)."""
        if not self.entries:
            return
        if len(self.entries) == 1:
            self.entries[0].video_offset = 0.0
            self.entries[0].visible_offset = 0.0
            self.entries[0].end_time = self.entries[0].actual_duration
            return

        groups = partition_into_groups(
            len(self.entries), lambda i: self.entries[i].transition
        )
        global_offset = 0.0
        for group_indices in groups:
            self._compute_group_offsets(group_indices, global_offset)
            group_dur = sum(self.entries[gi].actual_duration for gi in group_indices)
            group_overlap = sum(self.entries[gi].transition_duration for gi in group_indices[1:])
            if group_overlap > group_dur:
                logger.warning("Group overlap (%.2fs) exceeds duration (%.2fs), clamping",
                               group_overlap, group_dur)
                group_overlap = group_dur
            global_offset = self.entries[group_indices[0]].video_offset + group_dur - group_overlap

    def _compute_offsets_actual(self, output_dir: Path) -> None:
        """Compute offsets using measured group file durations."""
        if not self.entries:
            return
        if len(self.entries) == 1:
            self.entries[0].video_offset = 0.0
            self.entries[0].visible_offset = 0.0
            self.entries[0].end_time = self.entries[0].actual_duration
            return

        groups = partition_into_groups(
            len(self.entries), lambda i: self.entries[i].transition
        )
        global_offset = 0.0
        for gi, group_indices in enumerate(groups):
            self._compute_group_offsets(group_indices, global_offset)

            # Use ACTUAL rendered group duration if file exists
            group_file = output_dir / f"_group_{gi}.mp4"
            if group_file.exists():
                global_offset += _probe_dur(group_file)
            else:
                group_dur = sum(self.entries[idx].actual_duration for idx in group_indices)
                group_overlap = sum(self.entries[idx].transition_duration for idx in group_indices[1:])
                global_offset += group_dur - group_overlap

    def _compute_group_offsets(self, group_indices: list[int], global_offset: float) -> None:
        """Compute per-clip offsets within a single group."""
        if len(group_indices) == 1:
            e = self.entries[group_indices[0]]
            e.video_offset = global_offset
            e.visible_offset = global_offset
            e.end_time = global_offset + e.actual_duration
            return

        first = self.entries[group_indices[0]]
        first.video_offset = global_offset
        first.visible_offset = global_offset
        first.end_time = global_offset + first.actual_duration

        local_offset = 0.0
        for pos in range(1, len(group_indices)):
            idx = group_indices[pos]
            e = self.entries[idx]
            td = e.transition_duration

            if pos == 1:
                local_offset = first.actual_duration - td

            e.video_offset = global_offset + local_offset
            e.visible_offset = e.video_offset + td
            e.end_time = e.video_offset + e.actual_duration

            local_offset += e.actual_duration - td

    def total_duration(self) -> float:
        """Estimated total duration of the concatenated video."""
        if not self.entries:
            return 0.0
        last = self.entries[-1]
        return last.video_offset + last.actual_duration

    def speech_entries(self) -> list[TimelineEntry]:
        """Clips with keep_audio=True."""
        return [e for e in self.entries if e.keep_audio]

    def speech_ranges(self) -> list[tuple[float, float]]:
        """Time ranges where speech audio plays (for music ducking)."""
        ranges = []
        for e in self.speech_entries():
            start = e.visible_offset
            end = e.end_time
            if end > start:
                ranges.append((start, end))
        return ranges

    def chapter_offsets(self, edl, all_clips: list[dict]) -> list[tuple[float, str]]:
        """Compute chapter timestamps from segment boundaries.

        Returns list of (offset_seconds, segment_name) pairs.
        """
        chapters = []
        clip_idx = 0

        # Skip intro clip if present
        has_intro = edl.intro_style != "none"
        if has_intro and self.entries:
            clip_idx = 1

        for seg in edl.segments:
            if clip_idx < len(self.entries):
                offset = self.entries[clip_idx].video_offset
            else:
                offset = self.total_duration()
            chapters.append((offset, seg.name))
            clip_idx += len(seg.items)

        return chapters

    def xfade_offsets(self) -> list[float]:
        """Offsets for the xfade filter chain (video_offset per clip)."""
        return [e.video_offset for e in self.entries]

    def dump(self) -> None:
        """Print the full timeline for debugging."""
        logger.info("=== Timeline ===")
        logger.info("%3s %8s %8s %8s %5s %12s %4s %3s  clip",
                    "idx", "v_offset", "visible", "end", "dur", "tr", "td", "ka")
        logger.info("-" * 80)
        for e in self.entries:
            ka = "YES" if e.keep_audio else "   "
            logger.info("%3d %8.2f %8.2f %8.2f %5.2f %12s %4.1f %s  %s",
                        e.index, e.video_offset, e.visible_offset, e.end_time,
                        e.actual_duration, e.transition, e.transition_duration, ka,
                        e.path.name)
        logger.info("Total: %.2fs", self.total_duration())
        sr = self.speech_ranges()
        if sr:
            logger.info("Speech: %s", ", ".join(f"{s:.1f}-{e:.1f}s" for s, e in sr))
        logger.info("=== End Timeline ===")
