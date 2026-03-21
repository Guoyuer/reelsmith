"""Video concatenation: xfade transitions, group splitting, demuxer fallback."""

from __future__ import annotations

import shutil
from pathlib import Path

from .encoder import get_encoder, probe_duration
from .media_utils import run_subprocess


MAX_GROUP = 10


def _partition_into_groups(n: int, get_transition) -> list[list[int]]:
    """Partition clip indices into groups of <= MAX_GROUP.

    Shared by concatenate() and compute_actual_offsets() to ensure
    identical group boundaries.
    """
    groups: list[list[int]] = [[0]]
    for i in range(1, n):
        should_split = (
            len(groups[-1]) >= MAX_GROUP
            or (len(groups[-1]) >= MAX_GROUP - 3
                and get_transition(i) == "fade_black")
        )
        if should_split:
            groups.append([])
        groups[-1].append(i)
    return groups


def concatenate(clips: list[dict], output_path: Path,
                w: int = 0, h: int = 0, fps: int = 0,
                timeline=None, log_fn=None) -> None:
    """Concatenate clips with transitions (video only). Speech handled separately.

    For reliability at high resolutions, splits into segments and xfades
    within each segment, then concats segments via demuxer.
    """
    _log = log_fn or print

    if len(clips) == 1:
        shutil.copy(str(clips[0]["path"]), str(output_path))
        return

    all_cuts = all(c.get("transition") == "cut" for c in clips)
    if all_cuts:
        concat_demuxer(clips, output_path, w, h, fps)
        return

    # Split clips into groups of <= MAX_GROUP for 4K xfade reliability.
    idx_groups = _partition_into_groups(len(clips), lambda i: clips[i].get("transition"))
    groups = [[clips[i] for i in g] for g in idx_groups]

    _log(f"  Concat strategy: {len(groups)} groups ({', '.join(f'{len(g)} clips' for g in groups)})")

    if len(groups) == 1 and len(clips) <= 15:
        _log(f"  Single xfade ({len(clips)} clips)...")
        concat_xfade(clips, output_path, w, h, fps, timeline=timeline)
        return

    group_files: list[dict] = []
    tmp_dir = output_path.parent
    for gi, group in enumerate(groups):
        if len(group) == 1:
            group_files.append({"path": group[0]["path"], "duration": group[0]["duration"],
                                "transition": "cut", "transition_duration": 0.0})
            _log(f"  Group {gi+1}/{len(groups)}: 1 clip (pass-through)")
            continue

        group_path = tmp_dir / f"_group_{gi}.mp4"
        group[0] = {**group[0], "transition": "cut", "transition_duration": 0.0}
        # If group has any cut transitions (beyond first), use demuxer to avoid xfade chain bugs
        has_internal_cuts = any(c.get("transition") == "cut" for c in group[1:])
        if has_internal_cuts or len(group) > 15:
            _log(f"  Group {gi+1}/{len(groups)}: demuxer {len(group)} clips (has cuts)...")
            concat_demuxer(group, group_path, w, h, fps)
        else:
            _log(f"  Group {gi+1}/{len(groups)}: xfade {len(group)} clips...")
            concat_xfade(group, group_path, w, h, fps)

        if group_path.exists():
            dur = probe_duration(group_path) or sum(c["duration"] for c in group)
            group_files.append({"path": group_path, "duration": dur,
                                "transition": "cut", "transition_duration": 0.0})
            _log(f"  Group {gi+1}/{len(groups)}: done ({dur:.1f}s)")
        else:
            _log(f"  Group {gi+1}/{len(groups)}: xfade failed, falling back to demuxer...")
            concat_demuxer(group, group_path, w, h, fps)
            if group_path.exists():
                dur = probe_duration(group_path) or sum(c["duration"] for c in group)
                group_files.append({"path": group_path, "duration": dur,
                                    "transition": "cut", "transition_duration": 0.0})

    if not group_files:
        _log("  All groups failed, falling back to full demuxer")
        concat_demuxer(clips, output_path, w, h, fps)
        return

    if len(group_files) == 1:
        shutil.move(str(group_files[0]["path"]), str(output_path))
    else:
        _log(f"  Joining {len(group_files)} groups via demuxer...")
        concat_demuxer(group_files, output_path, w, h, fps)


def concat_demuxer(clips: list[dict], output_path: Path,
                   w: int = 0, h: int = 0, fps: int = 0) -> None:
    """Simple concatenation via concat demuxer (no transitions).

    Clips are already encoded at the correct bitrate/resolution, so we
    stream-copy instead of re-encoding.
    """
    list_path = output_path.parent / "concat_list.txt"
    with open(list_path, "w") as f:
        for clip in clips:
            f.write(f"file '{clip['path'].resolve()}'\n")

    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(list_path),
        "-c:v", "copy", "-c:a", "copy",
        str(output_path),
    ]
    run_subprocess(cmd, capture_output=True)


def concat_xfade(clips: list[dict], output_path: Path,
                 w: int = 0, h: int = 0, fps: int = 0,
                 timeline=None) -> None:
    """Concatenate with xfade transitions (video only). Uses Timeline offsets."""
    from .timeline import Timeline

    if timeline is None:
        timeline = Timeline.build(clips)

    inputs = []
    for clip in clips:
        inputs += ["-i", str(clip["path"])]

    filter_parts = []
    for i in range(1, len(timeline.entries)):
        e = timeline.entries[i]
        xfade_transition = {
            "crossfade": "fade", "fade_black": "fadeblack",
            "wipe_left": "wipeleft", "dissolve": "dissolve",
            "smoothleft": "smoothleft", "smoothright": "smoothright",
            "circlecrop": "circlecrop", "cut": "fade",
        }.get(e.transition, "fade")

        in_label = "[0:v]" if i == 1 else f"[v{i-1}]"
        out_label = f"[v{i}]" if i < len(timeline.entries) - 1 else "[vout]"

        td = e.transition_duration
        if td > 0:
            filter_parts.append(
                f"{in_label}[{i}:v]xfade=transition={xfade_transition}"
                f":duration={td}:offset={e.video_offset}{out_label}"
            )
        else:
            # Hard cut — use minimal crossfade (0.01 causes FFmpeg filter chain bugs)
            filter_parts.append(
                f"{in_label}[{i}:v]xfade=transition=fade"
                f":duration=0.1:offset={e.video_offset}{out_label}"
            )

    if not filter_parts:
        concat_demuxer(clips, output_path, w, h, fps)
        return

    filter_complex = ";".join(filter_parts)

    cmd = ["ffmpeg", "-y"] + inputs + [
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        *get_encoder(w, h, fps), "-pix_fmt", "yuv420p",
        str(output_path),
    ]
    result = run_subprocess(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        _log(f"xfade failed, falling back to concat demuxer: {result.stderr[-200:]}")
        concat_demuxer(clips, output_path, w, h, fps)
