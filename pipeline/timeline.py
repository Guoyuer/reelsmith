"""Timeline — single source of truth for all clip timing in the assembled vlog.

Every consumer (xfade, speech audio, music ducking, chapter markers) reads
from the same Timeline object. No more independent offset calculations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .media_utils import run_subprocess


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

    Build once from all_clips, then use for all offset-dependent operations.
    """
    entries: list[TimelineEntry] = field(default_factory=list)

    @staticmethod
    def build(all_clips: list[dict]) -> "Timeline":
        """Compute timeline matching _concatenate's exact behavior.

        _concatenate splits clips into groups at fade_black boundaries.
        Within each group: xfade (clips overlap by transition_duration).
        Between groups: demuxer (end-to-end, no overlap).

        For xfade groups, we replicate the exact offset formula from _concat_xfade:
          offset[1] = dur[0] - td[1]
          offset[i+1] = offset[i] + dur[i] - td[i]
        """
        tl = Timeline()

        # Probe actual durations
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

        if len(tl.entries) <= 1:
            if tl.entries:
                tl.entries[0].video_offset = 0.0
                tl.entries[0].visible_offset = 0.0
                tl.entries[0].end_time = tl.entries[0].actual_duration
            return tl

        # Split into groups of ≤15 (same logic as _concatenate)
        MAX_GROUP = 10
        groups: list[list[int]] = [[0]]
        for i in range(1, len(tl.entries)):
            should_split = (
                len(groups[-1]) >= MAX_GROUP
                or (len(groups[-1]) >= MAX_GROUP - 3
                    and tl.entries[i].transition == "fade_black")
            )
            if should_split:
                groups.append([])
            groups[-1].append(i)

        # Process each group
        global_offset = 0.0
        for group_indices in groups:
            group_size = len(group_indices)

            if group_size == 1:
                # Single clip: no xfade
                e = tl.entries[group_indices[0]]
                e.video_offset = global_offset
                e.visible_offset = global_offset
                e.end_time = global_offset + e.actual_duration
                global_offset += e.actual_duration
                continue

            # Multi-clip group: xfade within group
            # _concatenate sets group[0].transition = "cut", td = 0
            # Then runs _concat_xfade on the group.
            # Replicate _concat_xfade's exact offset math:

            # First clip in group starts at global_offset
            first = tl.entries[group_indices[0]]
            first.video_offset = global_offset
            first.visible_offset = global_offset
            first.end_time = global_offset + first.actual_duration

            # xfade offsets within the group (relative to group start)
            local_offset = 0.0
            for pos in range(1, group_size):
                idx = group_indices[pos]
                e = tl.entries[idx]
                prev = tl.entries[group_indices[pos - 1]]
                # td for within-group clips (fade_black was stripped by _concatenate)
                td = e.transition_duration

                if pos == 1:
                    local_offset = prev.actual_duration - td
                # else: local_offset already accumulated

                e.video_offset = global_offset + local_offset
                e.visible_offset = e.video_offset + td
                e.end_time = e.video_offset + e.actual_duration

                local_offset += e.actual_duration - td

            # Advance global_offset by the group's total rendered duration
            # group_rendered_dur = local_offset after last clip = final xfade offset + last dur
            global_offset += local_offset + first.actual_duration
            # Wait — local_offset starts at dur[0]-td[1] and accumulates dur[i]-td[i].
            # So local_offset = sum(dur[0..N-1]) - sum(td[1..N-1]) - td[1]
            # Hmm, this double-counts. Let me just compute:
            # group_dur = sum(all durs) - sum(all tds except first clip)
            # first clip's td is 0 (set by _concatenate)
            group_dur = sum(tl.entries[gi].actual_duration for gi in group_indices)
            group_overlap = sum(tl.entries[gi].transition_duration for gi in group_indices[1:])
            global_offset = first.video_offset + group_dur - group_overlap

        return tl

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
        """Time ranges where speech audio plays (for music ducking).

        Uses visible_offset (after transition) so ducking aligns with
        when the viewer actually sees/hears the clip.
        """
        ranges = []
        for e in self.speech_entries():
            start = e.visible_offset
            end = e.end_time
            if end > start:
                ranges.append((start, end))
        return ranges

    def xfade_offsets(self) -> list[float]:
        """Offsets for the xfade filter chain (video_offset per clip)."""
        return [e.video_offset for e in self.entries]

    def dump(self, log_fn=None) -> None:
        """Print the full timeline for debugging."""
        _log = log_fn or print
        _log("=== Timeline ===")
        _log(f"{'idx':>3} {'v_offset':>8} {'visible':>8} {'end':>8} "
             f"{'dur':>5} {'tr':>12} {'td':>4} {'ka':>3}  clip")
        _log("-" * 80)
        for e in self.entries:
            ka = "YES" if e.keep_audio else "   "
            _log(f"{e.index:3d} {e.video_offset:8.2f} {e.visible_offset:8.2f} {e.end_time:8.2f} "
                 f"{e.actual_duration:5.2f} {e.transition:>12} {e.transition_duration:4.1f} {ka}  "
                 f"{e.path.name}")
        _log(f"Total: {self.total_duration():.2f}s")
        sr = self.speech_ranges()
        if sr:
            _log(f"Speech: {', '.join(f'{s:.1f}-{e:.1f}s' for s, e in sr)}")
        _log("=== End Timeline ===")


def _probe_dur(path: Path) -> float:
    """Get video duration in seconds."""
    result = run_subprocess(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0
