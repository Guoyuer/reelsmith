"""Stage 4: Render the vlog from an EDL using FFmpeg.

Orchestration only — delegates to encoder, filters, render, concat, audio modules.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from tqdm import tqdm

from .audio import add_music, beat_snap_edl, build_speech_track, estimate_bpm, write_chapters
from .concat import compute_actual_offsets, concat_xfade, concatenate
from .config import Config
from .edl import EDL, EditItem
from .encoder import (
    get_context,
    get_encoder,
    init_context,
    is_portrait,
    probe_dimensions,
    probe_duration,
)
from .filters import (
    build_portrait_photo_filter,
    color_grade,
    drawtext_filter,
    find_font,
)
from .media_utils import run_subprocess
from .render import render_photo, render_video, render_title_card


# ---------------------------------------------------------------------------
# Backward-compatible aliases (used by tests importing from pipeline.assemble)
# ---------------------------------------------------------------------------
_get_encoder = get_encoder
_probe_dimensions = probe_dimensions
_probe_duration = probe_duration
_is_portrait = is_portrait
_build_portrait_photo_filter = build_portrait_photo_filter
_color_grade = color_grade
_drawtext_filter = drawtext_filter
_find_font = find_font
_render_photo = render_photo
_render_video = render_video
_render_title_card = render_title_card
_concatenate = concatenate
_concat_xfade = concat_xfade
_compute_actual_offsets = compute_actual_offsets
_build_speech_track = build_speech_track
_add_music = add_music
_write_chapters = write_chapters
_estimate_bpm = estimate_bpm
_beat_snap_edl = beat_snap_edl


# ---------------------------------------------------------------------------
# Main assemble entry point
# ---------------------------------------------------------------------------

def assemble(cfg: Config, *, version: int = 1, progress_callback=None, skip_broken: bool = False,
             resolution: tuple[int, int] | None = None, fps: int | None = None,
             quality: float = 1.0) -> tuple[Path, list[dict]]:
    """Read latest edl_v{N}.json and render the vlog video.

    Returns (output_path, validation_issues) where validation_issues is a list
    of dicts with keys: level ("error"/"warning"), check, message.
    """
    ctx = init_context(quality=quality)
    cfg.ensure_dirs()
    from .edl import load_latest_edl
    edl, _ = load_latest_edl(cfg)

    clips_dir = cfg.workspace / "clips"
    output_dir = cfg.workspace / "output"
    output_path = output_dir / f"vlog_v{version}.mp4"

    w, h = resolution or edl.resolution
    _fps = fps or edl.fps
    lang = edl.language

    # Beat sync: snap transitions to music beats (before rendering clips)
    if edl.music and Path(edl.music.file).exists():
        beat_snap_edl(edl, Path(edl.music.file), log_fn=print)

    # Determine parallel workers based on encoder type
    encoder = get_encoder(w, h, _fps)
    encoder_str = " ".join(encoder)
    if "nvenc" in encoder_str:
        max_workers = int(os.environ.get("VLOG_PARALLEL_CLIPS", "3"))
    elif "videotoolbox" in encoder_str:
        max_workers = 2
    else:
        max_workers = max(1, (os.cpu_count() or 4) // 2)

    # Phase 1: Render each item as a normalized clip (parallel)
    t1 = time.monotonic()

    tasks: list[tuple] = []
    for seg_idx, segment in enumerate(edl.segments):
        for item_idx, item in enumerate(segment.items):
            tasks.append((len(tasks), seg_idx, item_idx, item, segment))

    total_items = len(tasks)
    clip_results: list[Path | None] = [None] * total_items
    failed_clips: list[str] = []

    pbar = tqdm(total=total_items, desc=f"Rendering clips (x{max_workers})", unit="clip",
                bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]")

    def _do_render(task):
        order, seg_idx, item_idx, item, segment = task
        clip_name = f"seg{seg_idx:02d}_item{item_idx:02d}.mp4"
        clip_path = clips_dir / clip_name

        if not clip_path.exists():
            source = Path(item.source_file)
            if not source.exists():
                return order, clip_name, None

            ct = getattr(segment, "color_temp", "neutral") or "neutral"
            if item.media_type == "photo":
                render_photo(item, clip_path, w, h, _fps, color_temp=ct,
                             text_overlay=item.text_overlay, language=lang)
            else:
                render_video(item, clip_path, w, h, _fps, color_temp=ct,
                             text_overlay=item.text_overlay, language=lang)

        if not clip_path.exists():
            return order, clip_name, None
        return order, clip_name, clip_path

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_do_render, t): t[0] for t in tasks}
        for future in as_completed(futures):
            try:
                order, clip_name, clip_path = future.result()
                if clip_path is None:
                    failed_clips.append(clip_name)
                    pbar.write(f"  SKIP: {clip_name}")
                else:
                    clip_results[order] = clip_path
            except Exception as e:
                idx = futures[future]
                pbar.write(f"  ERROR ({idx}): {e}")
                failed_clips.append(f"task_{idx}")
            pbar.update(1)
            if progress_callback:
                progress_callback(pbar.n, total_items, "")

    pbar.close()
    t_clips = time.monotonic() - t1
    print(f"Phase 1 (clips): {t_clips:.1f}s ({max_workers} workers, "
          f"{total_items - len(failed_clips)}/{total_items} OK)")

    if failed_clips and not skip_broken:
        raise RuntimeError(f"Failed to render {len(failed_clips)} clips: {', '.join(failed_clips)}")

    # Build all_clips list with transitions (must be in order)
    all_clips: list[dict] = []
    idx = 0
    for seg_idx, segment in enumerate(edl.segments):
        for item_idx, item in enumerate(segment.items):
            clip_path = clip_results[idx]
            idx += 1
            if clip_path is None:
                continue

            is_montage = getattr(segment, "mode", "narrative") == "montage"
            if is_montage:
                transition = "cut"
                td = 0.0
            elif item_idx == 0 and seg_idx > 0:
                transition = "fade_black"
                td = 1.0
            elif item_idx > 0:
                transition = segment.transition
                td = segment.transition_duration if transition != "cut" else 0.0
            else:
                transition = "cut"
                td = 0.0

            all_clips.append({
                "path": clip_path,
                "duration": item.display_duration,
                "transition": transition,
                "transition_duration": td,
                "keep_audio": item.keep_audio,
            })

    if not all_clips:
        raise RuntimeError("No clips rendered — check source files in EDL")

    # Phase 1b: Render intro/outro clips
    if edl.intro_style == "title_card" and edl.title:
        intro_path = clips_dir / "intro_title.mp4"
        if not intro_path.exists():
            render_title_card(edl.title, edl.date_range, intro_path, w, h, _fps, duration=3.0, language=lang)
        if intro_path.exists():
            all_clips.insert(0, {
                "path": intro_path, "duration": 3.0,
                "transition": "cut", "transition_duration": 0.0,
            })
            if len(all_clips) > 1:
                all_clips[1]["transition"] = "fade_black"
                all_clips[1]["transition_duration"] = 1.0

    if edl.outro_style == "fade_title" and edl.title:
        outro_path = clips_dir / "outro_title.mp4"
        if not outro_path.exists():
            render_title_card(edl.title, "", outro_path, w, h, _fps, duration=3.0, language=lang)
        if outro_path.exists():
            all_clips.append({
                "path": outro_path, "duration": 3.0,
                "transition": "fade_black", "transition_duration": 1.0,
            })

    # Phase 2: Concatenate with transitions (video only)
    t2 = time.monotonic()
    print(f"Concatenating {len(all_clips)} clips...")
    no_music_path = output_dir / f"vlog_v{version}_nomix.mp4"
    concatenate(all_clips, no_music_path)
    print(f"Phase 2 (concat): {time.monotonic() - t2:.1f}s")

    # Phase 2b: Build speech audio track
    speech_audio_path = None
    speech_ka_indices = [i for i, c in enumerate(all_clips) if c.get("keep_audio")]
    if speech_ka_indices:
        actual_offsets = compute_actual_offsets(all_clips, output_dir)
        video_dur = probe_duration(no_music_path)

        speech_clips = []
        speech_ranges = []
        for i in speech_ka_indices:
            offset = actual_offsets[i]
            dur = probe_duration(all_clips[i]["path"]) or all_clips[i]["duration"]
            speech_clips.append((offset, all_clips[i]["path"]))
            speech_ranges.append((offset, offset + dur))

        speech_audio_path = output_dir / f"vlog_v{version}_speech.wav"
        build_speech_track(speech_clips, video_dur, speech_audio_path)
        print(f"Speech track: {len(speech_clips)} clips at "
              f"{', '.join(f'{o:.1f}s' for o, _ in speech_clips)}")
    else:
        speech_ranges = []

    # Cleanup temp group files
    for gf in output_dir.glob("_group_*.mp4"):
        gf.unlink(missing_ok=True)

    # Phase 3: Mix music + speech
    t3 = time.monotonic()
    if edl.music and Path(edl.music.file).exists():
        music_dur = probe_duration(Path(edl.music.file))
        video_dur = probe_duration(no_music_path)
        print(f"Mixing music: video={video_dur:.1f}s, music={music_dur:.1f}s, "
              f"volume={edl.music.volume}, fade_in={edl.music.fade_in}s, fade_out={edl.music.fade_out}s")
        add_music(no_music_path, edl.music, output_path,
                  speech_ranges=speech_ranges, speech_audio=speech_audio_path)
        no_music_path.unlink(missing_ok=True)
        if speech_audio_path:
            speech_audio_path.unlink(missing_ok=True)
    elif speech_audio_path:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(no_music_path),
            "-i", str(speech_audio_path),
            "-map", "0:v", "-map", "1:a",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            str(output_path),
        ]
        run_subprocess(cmd, capture_output=True)
        no_music_path.unlink(missing_ok=True)
        speech_audio_path.unlink(missing_ok=True)
    else:
        shutil.move(str(no_music_path), str(output_path))
    print(f"Phase 3 (audio): {time.monotonic() - t3:.1f}s")

    duration = probe_duration(output_path)
    total_time = time.monotonic() - t1
    print(f"Done: {output_path} ({duration:.1f}s, rendered in {total_time:.0f}s)")

    # Generate YouTube chapter markers
    chapters_path = output_dir / f"chapters_v{version}.txt"
    write_chapters(edl, all_clips, chapters_path)

    # Phase 4: Validate output
    has_speech = bool(speech_ka_indices)
    validation_issues = _validate_output_impl(output_path, edl, has_speech, (w, h))
    errors = [i for i in validation_issues if i["level"] == "error"]
    warnings = [i for i in validation_issues if i["level"] == "warning"]

    if warnings:
        print(f"Validation: {len(warnings)} warning(s)")
        for issue in warnings:
            print(f"  WARNING [{issue['check']}]: {issue['message']}")
    if errors:
        print(f"Validation: {len(errors)} error(s)")
        for issue in errors:
            print(f"  ERROR [{issue['check']}]: {issue['message']}")
        raise RuntimeError(
            f"Output validation failed with {len(errors)} error(s): "
            + "; ".join(i["message"] for i in errors)
        )

    if not validation_issues:
        print("Validation: all checks passed")

    return output_path, validation_issues


# ---------------------------------------------------------------------------
# Post-assemble output validation
# ---------------------------------------------------------------------------

def _validate_output_impl(
    output_path: Path,
    edl: EDL,
    has_speech: bool,
    resolution: tuple[int, int],
) -> list[dict]:
    """Validate the rendered output video. Returns a list of issue dicts.

    Each issue dict has:
      - level: "error" or "warning"
      - check: short identifier for the check (e.g. "file_exists", "duration")
      - message: human-readable description

    Errors indicate critical failures; warnings are informational.
    """
    logger = logging.getLogger(__name__)
    issues: list[dict] = []

    def _error(check: str, msg: str) -> None:
        issues.append({"level": "error", "check": check, "message": msg})
        logger.error("Validation FAIL [%s]: %s", check, msg)

    def _warn(check: str, msg: str) -> None:
        issues.append({"level": "warning", "check": check, "message": msg})
        logger.warning("Validation WARN [%s]: %s", check, msg)

    # --- 1. File existence and minimum size ---
    if not output_path.exists():
        _error("file_exists", f"Output file does not exist: {output_path}")
        return issues

    file_size = output_path.stat().st_size
    if file_size < 1024:
        _error("file_size", f"Output file too small ({file_size} bytes): {output_path}")
        return issues

    # --- 2. Duration check ---
    expected_duration = edl.estimated_duration()
    get_context()._dur_cache.pop(str(output_path), None)
    actual_duration = probe_duration(output_path)

    if actual_duration <= 0:
        _error("duration", "Could not probe output duration (ffprobe returned 0)")
    elif expected_duration > 0:
        ratio = actual_duration / expected_duration
        if ratio < 0.5:
            _error("duration",
                   f"Duration {actual_duration:.1f}s is <50% of expected "
                   f"{expected_duration:.1f}s — possible xfade truncation")
        elif ratio < 0.8:
            _warn("duration",
                  f"Duration {actual_duration:.1f}s is <80% of expected "
                  f"{expected_duration:.1f}s — some content may be missing")

    # --- 3. Stream validation (video + audio presence) ---
    stream_result = run_subprocess(
        ["ffprobe", "-v", "error",
         "-show_entries", "stream=codec_type,codec_name",
         "-of", "csv=p=0",
         str(output_path)],
        capture_output=True, text=True,
    )
    stream_lines = [ln.strip() for ln in stream_result.stdout.strip().split("\n") if ln.strip()]
    codec_types = []
    codec_names = {}
    for line in stream_lines:
        parts = line.split(",")
        if len(parts) >= 2:
            codec_name, codec_type = parts[0].strip(), parts[1].strip()
            codec_types.append(codec_type)
            codec_names[codec_type] = codec_name

    has_video_stream = "video" in codec_types
    has_audio_stream = "audio" in codec_types

    if not has_video_stream:
        _error("video_stream", "No video stream found in output")

    if not has_audio_stream and has_speech:
        _warn("audio_stream", "No audio stream in output but speech clips were expected")

    has_music = edl.music is not None and edl.music.file and Path(edl.music.file).exists()
    if not has_audio_stream and has_music:
        _warn("audio_stream_music", "No audio stream in output but music track was configured")

    # --- 4. Video codec check ---
    if has_video_stream:
        video_codec = codec_names.get("video", "")
        expected_codecs = {"hevc", "h264", "h265"}
        if video_codec and video_codec not in expected_codecs:
            _warn("video_codec",
                  f"Unexpected video codec '{video_codec}' "
                  f"(expected one of {sorted(expected_codecs)})")

    # --- 5. Audio-video sync spot check ---
    if has_video_stream and has_audio_stream:
        vid_dur_result = run_subprocess(
            ["ffprobe", "-v", "error",
             "-select_streams", "v:0",
             "-show_entries", "stream=duration",
             "-of", "csv=p=0",
             str(output_path)],
            capture_output=True, text=True,
        )
        aud_dur_result = run_subprocess(
            ["ffprobe", "-v", "error",
             "-select_streams", "a:0",
             "-show_entries", "stream=duration",
             "-of", "csv=p=0",
             str(output_path)],
            capture_output=True, text=True,
        )
        try:
            vid_stream_dur = float(vid_dur_result.stdout.strip())
            aud_stream_dur = float(aud_dur_result.stdout.strip())
            drift = abs(vid_stream_dur - aud_stream_dur)
            if drift > 5.0:
                _warn("av_sync",
                      f"Audio/video stream duration mismatch: "
                      f"video={vid_stream_dur:.1f}s, audio={aud_stream_dur:.1f}s "
                      f"(drift={drift:.1f}s) — possible sync issue")
        except (ValueError, TypeError):
            pass

    # --- 6. Resolution check ---
    if has_video_stream:
        get_context()._dim_cache.pop(str(output_path), None)
        out_w, out_h = probe_dimensions(output_path)
        exp_w, exp_h = resolution
        if out_w > 0 and out_h > 0:
            if out_w != exp_w or out_h != exp_h:
                _warn("resolution",
                      f"Output resolution {out_w}x{out_h} does not match "
                      f"expected {exp_w}x{exp_h}")

    return issues


# Alias for backward compatibility with tests
_validate_output = _validate_output_impl
