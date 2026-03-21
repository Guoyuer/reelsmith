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


def concatenate(clips: list[dict], output_path: Path, timeline=None) -> None:
    """Concatenate clips with transitions (video only). Speech handled separately.

    For reliability at high resolutions, splits into segments and xfades
    within each segment, then concats segments via demuxer.
    """
    if len(clips) == 1:
        shutil.copy(str(clips[0]["path"]), str(output_path))
        return

    all_cuts = all(c.get("transition") == "cut" for c in clips)
    if all_cuts:
        concat_demuxer(clips, output_path)
        return

    # Split clips into groups of <= MAX_GROUP for 4K xfade reliability.
    idx_groups = _partition_into_groups(len(clips), lambda i: clips[i].get("transition"))
    groups = [[clips[i] for i in g] for g in idx_groups]

    print(f"  Concat strategy: {len(groups)} groups ({', '.join(f'{len(g)} clips' for g in groups)})")

    if len(groups) == 1 and len(clips) <= 15:
        print(f"  Single xfade ({len(clips)} clips)...")
        concat_xfade(clips, output_path, timeline=timeline)
        return

    group_files: list[dict] = []
    tmp_dir = output_path.parent
    for gi, group in enumerate(groups):
        if len(group) == 1:
            group_files.append({"path": group[0]["path"], "duration": group[0]["duration"],
                                "transition": "cut", "transition_duration": 0.0})
            print(f"  Group {gi+1}/{len(groups)}: 1 clip (pass-through)")
            continue

        group_path = tmp_dir / f"_group_{gi}.mp4"
        group[0] = {**group[0], "transition": "cut", "transition_duration": 0.0}
        if len(group) <= 15:
            print(f"  Group {gi+1}/{len(groups)}: xfade {len(group)} clips...")
            concat_xfade(group, group_path)
        else:
            print(f"  Group {gi+1}/{len(groups)}: demuxer {len(group)} clips...")
            concat_demuxer(group, group_path)

        if group_path.exists():
            dur = probe_duration(group_path) or sum(c["duration"] for c in group)
            group_files.append({"path": group_path, "duration": dur,
                                "transition": "cut", "transition_duration": 0.0})
            print(f"  Group {gi+1}/{len(groups)}: done ({dur:.1f}s)")
        else:
            print(f"  Group {gi+1}/{len(groups)}: xfade failed, falling back to demuxer...")
            concat_demuxer(group, group_path)
            if group_path.exists():
                dur = probe_duration(group_path) or sum(c["duration"] for c in group)
                group_files.append({"path": group_path, "duration": dur,
                                    "transition": "cut", "transition_duration": 0.0})

    if not group_files:
        print("  All groups failed, falling back to full demuxer")
        concat_demuxer(clips, output_path)
        return

    if len(group_files) == 1:
        shutil.move(str(group_files[0]["path"]), str(output_path))
    else:
        print(f"  Joining {len(group_files)} groups via demuxer...")
        concat_demuxer(group_files, output_path)


def concat_demuxer(clips: list[dict], output_path: Path) -> None:
    """Simple concatenation via concat demuxer (no transitions)."""
    list_path = output_path.parent / "concat_list.txt"
    with open(list_path, "w") as f:
        for clip in clips:
            f.write(f"file '{clip['path'].resolve()}'\n")

    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(list_path),
        *get_encoder(), "-pix_fmt", "yuv420p",
        str(output_path),
    ]
    run_subprocess(cmd, capture_output=True)


def concat_xfade(clips: list[dict], output_path: Path,
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
            filter_parts.append(
                f"{in_label}[{i}:v]xfade=transition=fade"
                f":duration=0.01:offset={e.video_offset}{out_label}"
            )

    if not filter_parts:
        concat_demuxer(clips, output_path)
        return

    filter_complex = ";".join(filter_parts)

    cmd = ["ffmpeg", "-y"] + inputs + [
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        *get_encoder(), "-pix_fmt", "yuv420p",
        str(output_path),
    ]
    result = run_subprocess(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"xfade failed, falling back to concat demuxer: {result.stderr[-200:]}")
        concat_demuxer(clips, output_path)


def compute_actual_offsets(all_clips: list[dict], output_dir: Path) -> list[float]:
    """Compute actual clip start times from rendered group files.

    Instead of mathematically estimating xfade durations (which drift),
    this reads the actual rendered group files and uses their measured
    durations to compute cumulative offsets.
    """
    groups = _partition_into_groups(len(all_clips), lambda i: all_clips[i].get("transition"))

    offsets = [0.0] * len(all_clips)
    global_offset = 0.0

    for gi, group_indices in enumerate(groups):
        group_clips = [all_clips[i] for i in group_indices]
        group_durs = [probe_duration(c["path"]) or c["duration"] for c in group_clips]

        if len(group_indices) == 1:
            offsets[group_indices[0]] = global_offset
            global_offset += group_durs[0]
        else:
            local_offset = 0.0
            for pos, idx in enumerate(group_indices):
                td = all_clips[idx].get("transition_duration", 0.0)
                if all_clips[idx].get("transition") == "cut" or pos == 0:
                    td = 0.0

                if pos == 0:
                    offsets[idx] = global_offset
                elif pos == 1:
                    local_offset = group_durs[0] - td
                    offsets[idx] = global_offset + local_offset
                else:
                    offsets[idx] = global_offset + local_offset

                if pos >= 1:
                    local_offset += group_durs[pos] - td

            group_file = output_dir / f"_group_{gi}.mp4"
            if group_file.exists():
                group_dur = probe_duration(group_file)
            else:
                group_dur = sum(group_durs)
                for pos in range(1, len(group_indices)):
                    td = all_clips[group_indices[pos]].get("transition_duration", 0.0)
                    if all_clips[group_indices[pos]].get("transition") != "cut":
                        group_dur -= td
            global_offset += group_dur

    return offsets
