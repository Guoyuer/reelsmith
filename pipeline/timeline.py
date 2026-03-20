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
        """Compute timeline from the all_clips list (same structure as assemble uses).

        Replicates xfade's exact offset math so video and audio are guaranteed in sync.
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

        # Compute offsets using xfade's exact algorithm:
        #   offset[1] = dur[0] - td[1]
        #   offset[i] = offset[i-1] + dur[i-1] - td[i]   (for i >= 2)
        #   (but xfade code sets offset = dur[0]-td[1] at i=1, then offset += dur[i]-td[i])
        if len(tl.entries) > 1:
            offset = 0.0
            for i in range(1, len(tl.entries)):
                e = tl.entries[i]
                prev = tl.entries[i - 1]
                if i == 1:
                    offset = prev.actual_duration - e.transition_duration
                e.video_offset = offset
                e.visible_offset = offset + e.transition_duration
                e.end_time = offset + e.actual_duration
                offset += e.actual_duration - e.transition_duration

        # Entry 0 always starts at 0
        if tl.entries:
            tl.entries[0].video_offset = 0.0
            tl.entries[0].visible_offset = 0.0
            tl.entries[0].end_time = tl.entries[0].actual_duration

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
