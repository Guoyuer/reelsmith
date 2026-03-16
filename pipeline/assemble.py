"""Stage 4: Render the vlog from an EDL using FFmpeg."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from tqdm import tqdm

from .config import Config
from .edl import EDL, EditItem, Segment
from .media_utils import convert_heic, run_subprocess, _zoompan_filter, _portrait_bg_filter


# ---------------------------------------------------------------------------
# Portrait detection & filter helpers (pure functions, easily testable)
# ---------------------------------------------------------------------------

def _probe_dimensions(path: Path) -> tuple[int, int]:
    """Use ffprobe to get (width, height) of a media file."""
    result = run_subprocess(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0:s=x",
            str(path),
        ],
        capture_output=True, text=True,
    )
    try:
        parts = result.stdout.strip().split("x")
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return 0, 0


def _is_portrait(src_w: int, src_h: int) -> bool:
    """Return True if the source is clearly portrait (height > width * 1.2)."""
    return src_w > 0 and src_h > src_w * 1.2


def _build_portrait_photo_filter(
    out_w: int, out_h: int, frames: int, fps: int, zoom_rate: float,
) -> str:
    """Build FFmpeg filter_complex for portrait photos: blurred BG + sharp FG + gentle Ken Burns."""
    return (
        f"[0:v]split[bg][fg];"
        f"[bg]scale=960:-1:force_original_aspect_ratio=increase,crop=960:540,"
        f"gblur=sigma=20,scale={out_w}:{out_h}[blurred];"
        f"[fg]scale=-1:{out_h}[sharp];"
        f"[blurred][sharp]overlay=(W-w)/2:(H-h)/2[comp];"
        f"[comp]zoompan=z='min(zoom+{zoom_rate:.6f},1.08)':d={frames}"
        f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":s={out_w}x{out_h}:fps={fps}"
    )


# ---------------------------------------------------------------------------
# Main assemble entry point
# ---------------------------------------------------------------------------

def assemble(cfg: Config, *, version: int = 1, progress_callback=None, skip_broken: bool = False,
             resolution: tuple[int, int] | None = None, fps: int | None = None) -> Path:
    """Read latest edl_v{N}.json and render the vlog video."""
    cfg.ensure_dirs()
    from .iterate import _load_latest_edl
    edl, _ = _load_latest_edl(cfg)

    clips_dir = cfg.workspace / "clips"
    output_dir = cfg.workspace / "output"
    output_path = output_dir / f"vlog_v{version}.mp4"

    w, h = resolution or edl.resolution
    fps = fps or edl.fps

    # Phase 1: Render each item as a normalized clip
    all_clips: list[dict] = []  # {"path": Path, "duration": float, "transition": str, "transition_duration": float}
    failed_clips: list[str] = []
    total_items = sum(len(seg.items) for seg in edl.segments)
    pbar = tqdm(total=total_items, desc="Rendering clips", unit="clip",
                bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]")

    for seg_idx, segment in enumerate(edl.segments):
        for item_idx, item in enumerate(segment.items):
            clip_name = f"seg{seg_idx:02d}_item{item_idx:02d}.mp4"
            clip_path = clips_dir / clip_name

            if not clip_path.exists():
                source = Path(item.source_file)
                if not source.exists():
                    pbar.write(f"  SKIP (missing): {item.source_file}")
                    failed_clips.append(clip_name)
                    pbar.update(1)
                    continue

                pbar.set_postfix_str(clip_name, refresh=True)
                if item.media_type == "photo":
                    _render_photo(item, clip_path, w, h, fps)
                else:
                    _render_video(item, clip_path, w, h, fps)

            if not clip_path.exists():
                failed_clips.append(clip_name)
                pbar.update(1)
                continue

            # Apply text overlay if specified
            if item.text_overlay:
                overlaid = clips_dir / f"{clip_path.stem}_txt.mp4"
                if not overlaid.exists():
                    _add_text_overlay(
                        clip_path, overlaid,
                        item.text_overlay.text,
                        item.text_overlay.position,
                        item.text_overlay.font_size,
                        clip_duration=item.display_duration,
                    )
                if overlaid.exists():
                    clip_path = overlaid

            # Determine transition (use segment's transition for items within segment)
            transition = segment.transition if item_idx > 0 else "cut"
            td = segment.transition_duration if transition != "cut" else 0.0
            # Between segments: use fade_black
            if item_idx == 0 and seg_idx > 0:
                transition = "fade_black"
                td = 1.0

            all_clips.append({
                "path": clip_path,
                "duration": item.display_duration,
                "transition": transition,
                "transition_duration": td,
            })
            pbar.update(1)
            if progress_callback:
                progress_callback(pbar.n, total_items, clip_name)

    pbar.close()

    if failed_clips and not skip_broken:
        raise RuntimeError(f"Failed to render {len(failed_clips)} clips: {', '.join(failed_clips)}")

    if not all_clips:
        raise RuntimeError("No clips rendered — check source files in EDL")

    # Phase 2: Concatenate with transitions
    print(f"Concatenating {len(all_clips)} clips...")
    no_music_path = output_dir / f"vlog_v{version}_nomix.mp4"
    _concatenate(all_clips, no_music_path)

    # Phase 3: Add music if specified
    if edl.music and Path(edl.music.file).exists():
        print("Mixing music...")
        _add_music(no_music_path, edl.music, output_path)
        no_music_path.unlink(missing_ok=True)
    else:
        shutil.move(str(no_music_path), str(output_path))

    duration = _probe_duration(output_path)
    print(f"Done: {output_path} ({duration:.1f}s)")
    return output_path


def _render_photo(item: EditItem, out: Path, w: int, h: int, fps: int) -> None:
    """Render a photo with Ken Burns effect as a video clip."""
    source = Path(item.source_file)

    # Convert HEIC to JPEG first — FFmpeg can't use HEIC with -loop 1
    if source.suffix.lower() in {".heic", ".heif"}:
        try:
            source = convert_heic(source)
        except RuntimeError:
            print(f"    HEIC convert failed: {item.source_file}")
            return

    frames = int(item.display_duration * fps)
    zoom_rate = 0.001 + (0.3 / frames)  # reach ~1.3x zoom over the duration

    # Probe dimensions (after HEIC conversion) to decide portrait vs landscape
    src_w, src_h = _probe_dimensions(source)
    portrait = _is_portrait(src_w, src_h)

    if portrait:
        # Portrait: blurred background + sharp foreground + gentle Ken Burns
        portrait_zoom_rate = 0.001 + (0.08 / frames)  # gentler zoom for portrait
        fc = _build_portrait_photo_filter(w, h, frames, fps, portrait_zoom_rate)
        cmd = [
            "ffmpeg", "-y", "-loop", "1", "-i", str(source),
            "-t", str(item.display_duration),
            "-filter_complex", fc,
            "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
            "-an",
            str(out),
        ]
    else:
        # Landscape: existing Ken Burns zoompan (works well)
        direction_map = {
            "ken_burns_in": "in",
            "ken_burns_out": "out",
            "ken_burns_left": "left",
            "ken_burns_right": "right",
            "static": "static",
        }
        direction = direction_map.get(item.effect, "in")
        zp = _zoompan_filter(zoom_rate, frames, w, h, fps, direction=direction)

        cmd = [
            "ffmpeg", "-y", "-loop", "1", "-i", str(source),
            "-t", str(item.display_duration),
            "-vf", f"scale={w*2}:{h*2}:force_original_aspect_ratio=increase,crop={w*2}:{h*2},{zp}",
            "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
            "-an",
            str(out),
        ]

    result = run_subprocess(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"    Photo render failed: {result.stderr[-200:]}")


def _render_video(item: EditItem, out: Path, w: int, h: int, fps: int) -> None:
    """Trim and normalize a video clip."""
    cmd = ["ffmpeg", "-y"]
    if item.start_time is not None:
        cmd += ["-ss", str(item.start_time)]
    cmd += ["-i", str(item.source_file)]

    duration = item.display_duration
    if item.start_time is not None and item.end_time is not None:
        duration = item.end_time - item.start_time
    cmd += ["-t", str(duration)]

    # Probe dimensions to decide portrait vs landscape
    src_w, src_h = _probe_dimensions(Path(item.source_file))
    portrait = _is_portrait(src_w, src_h)

    if portrait:
        # Portrait: blurred background + sharp foreground (no black bars)
        fc = _portrait_bg_filter(w, h)
        cmd += [
            "-filter_complex", fc,
            "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
            "-r", str(fps),
            "-an",
            str(out),
        ]
    else:
        # Landscape: existing scale+pad (fine for landscape)
        cmd += [
            "-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                   f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2",
            "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
            "-r", str(fps),
            "-an",
            str(out),
        ]

    result = run_subprocess(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"    Video render failed: {result.stderr[-200:]}")


def _add_text_overlay(
    input_path: Path, output_path: Path,
    text: str, position: str, font_size: int,
    clip_duration: float = 4.0,
) -> None:
    """Burn a text overlay onto a clip."""
    y_positions = {"top": "50", "center": "(h-text_h)/2", "bottom": "h-text_h-60"}
    y_expr = y_positions.get(position, y_positions["bottom"])

    # Escape special characters for drawtext
    safe_text = text.replace("'", "\u2019").replace(":", "\\:")

    end_time = min(clip_duration - 0.5, 3.0)
    vf = (
        f"drawtext=text='{safe_text}':fontsize={font_size}:fontcolor=white"
        f":borderw=2:bordercolor=black"
        f":x=(w-text_w)/2:y={y_expr}"
        f":enable='between(t,0.5,{end_time:.1f})'"
    )
    cmd = [
        "ffmpeg", "-y", "-i", str(input_path),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        str(output_path),
    ]
    run_subprocess(cmd, capture_output=True)


def _concatenate(clips: list[dict], output_path: Path) -> None:
    """Concatenate clips, using xfade for crossfades and fade_black."""
    if len(clips) == 1:
        shutil.copy(str(clips[0]["path"]), str(output_path))
        return

    # For large numbers of clips, use concat demuxer (simpler, more robust)
    # For smaller sets with transitions, use xfade filter chain
    if len(clips) > 30 or all(c["transition"] == "cut" for c in clips):
        _concat_demuxer(clips, output_path)
    else:
        _concat_xfade(clips, output_path)


def _concat_demuxer(clips: list[dict], output_path: Path) -> None:
    """Simple concatenation via concat demuxer (no transitions)."""
    list_path = output_path.parent / "concat_list.txt"
    with open(list_path, "w") as f:
        for clip in clips:
            f.write(f"file '{clip['path'].resolve()}'\n")

    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(list_path),
        "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
        str(output_path),
    ]
    run_subprocess(cmd, capture_output=True)
    list_path.unlink(missing_ok=True)


def _concat_xfade(clips: list[dict], output_path: Path) -> None:
    """Concatenate with xfade transitions between clips."""
    inputs = []
    for clip in clips:
        inputs += ["-i", str(clip["path"])]

    # Build xfade filter chain
    filter_parts = []
    offset = 0.0

    for i in range(1, len(clips)):
        td = clips[i]["transition_duration"]
        transition_type = clips[i]["transition"]

        if transition_type == "cut":
            td = 0.0

        xfade_transition = {
            "crossfade": "fade",
            "fade_black": "fadeblack",
            "wipe_left": "wipeleft",
            "cut": "fade",
        }.get(transition_type, "fade")

        if i == 1:
            in_label = "[0:v]"
            offset = clips[0]["duration"] - td
        else:
            in_label = f"[v{i-1}]"

        out_label = f"[v{i}]" if i < len(clips) - 1 else "[vout]"

        if td > 0:
            filter_parts.append(
                f"{in_label}[{i}:v]xfade=transition={xfade_transition}"
                f":duration={td}:offset={offset}{out_label}"
            )
        else:
            # No transition — just concat via overlay workaround
            filter_parts.append(
                f"{in_label}[{i}:v]xfade=transition=fade"
                f":duration=0.01:offset={offset}{out_label}"
            )

        offset += clips[i]["duration"] - td

    if not filter_parts:
        _concat_demuxer(clips, output_path)
        return

    filter_complex = ";".join(filter_parts)

    cmd = ["ffmpeg", "-y"] + inputs + [
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
        str(output_path),
    ]
    result = run_subprocess(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"xfade failed, falling back to concat demuxer: {result.stderr[-200:]}")
        _concat_demuxer(clips, output_path)


def _add_music(video_path: Path, music, output_path: Path) -> None:
    """Mix background music under the video."""
    total_dur = _probe_duration(video_path)
    fade_out_start = max(0, total_dur - music.fade_out)

    audio_filter = (
        f"[1:a]volume={music.volume},"
        f"afade=t=in:d={music.fade_in},"
        f"afade=t=out:st={fade_out_start}:d={music.fade_out}[a]"
    )
    cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-i", str(music.file),
        "-filter_complex", audio_filter,
        "-map", "0:v", "-map", "[a]",
        "-shortest",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        str(output_path),
    ]
    run_subprocess(cmd, capture_output=True)


def _probe_duration(path: Path) -> float:
    """Get video duration in seconds."""
    result = run_subprocess(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            str(path),
        ],
        capture_output=True, text=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0
