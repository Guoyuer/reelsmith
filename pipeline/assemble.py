"""Stage 4: Render the vlog from an EDL using FFmpeg.

Orchestration only — delegates to encoder, filters, render, concat, audio modules.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from tqdm import tqdm

from .audio import beat_snap_edl, build_speech_track, mix_final_audio, write_chapters
from .concat import concatenate
from .config import Config
from .edl import EDL, EditItem, load_latest_edl, validate_edl
from .encoder import (
    RenderContext,
    get_context,
    init_context,
    probe_duration,
)
from .media_utils import run_subprocess
from .parallel import run_parallel
from .render import render_photo, render_video, render_title_card
from .timeline import Timeline

logger = logging.getLogger("vlog.assemble")


# ---------------------------------------------------------------------------
# Render report — structured clip status tracking (replaces bare print/list)
# ---------------------------------------------------------------------------

@dataclass
class ClipStatus:
    """Status of a single clip render."""
    clip_name: str
    source_file: str
    status: Literal["ok", "skipped", "failed"] = "ok"
    reason: str = ""


@dataclass
class RenderReport:
    """Tracks clip rendering outcomes for structured reporting."""
    clips: list[ClipStatus] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return sum(1 for c in self.clips if c.status == "ok")

    @property
    def skipped_count(self) -> int:
        return sum(1 for c in self.clips if c.status == "skipped")

    @property
    def failed_count(self) -> int:
        return sum(1 for c in self.clips if c.status == "failed")

    def summary(self) -> str:
        parts = [f"{self.ok_count}/{len(self.clips)} OK"]
        if self.skipped_count:
            skipped = [c for c in self.clips if c.status == "skipped"]
            parts.append(f"{self.skipped_count} skipped ({', '.join(c.clip_name for c in skipped)})")
        if self.failed_count:
            failed = [c for c in self.clips if c.status == "failed"]
            parts.append(f"{self.failed_count} failed ({', '.join(f'{c.clip_name}: {c.reason}' for c in failed)})")
        return ", ".join(parts)

    def to_metadata(self) -> dict:
        """Convert to metadata dict."""
        return {
            "clips_ok": self.ok_count,
            "clips_skipped": self.skipped_count,
            "clips_failed": self.failed_count,
            "clip_details": self.summary(),
        }




# ---------------------------------------------------------------------------
# Main assemble entry point
# ---------------------------------------------------------------------------

def assemble(cfg: Config, *, version: int = 1, progress_callback=None, skip_broken: bool = False,
             resolution: tuple[int, int] = (1920, 1080), fps: int = 30,
             quality: float = 1.0) -> tuple[Path, list[dict]]:
    """Read latest edl_v{N}.json and render the vlog video.

    Returns (output_path, validation_issues) where validation_issues is a list
    of dicts with keys: level ("error"/"warning"), check, message.
    """
    _log = logger.info
    cfg.ensure_dirs()

    # Validate render parameters
    w, h = resolution
    if w <= 0 or h <= 0:
        raise ValueError(f"Invalid resolution: {w}x{h}")
    if w % 2 != 0 or h % 2 != 0:
        raise ValueError(f"Resolution must be even: {w}x{h}")
    if fps <= 0 or fps > 120:
        raise ValueError(f"Invalid fps: {fps}")
    if quality <= 0 or quality > 5:
        raise ValueError(f"Invalid quality: {quality}")

    if version > 0:
        edl_path = cfg.workspace / f"edl_v{version}.json"
        edl = EDL.model_validate_json(edl_path.read_text())
    else:
        edl, version = load_latest_edl(cfg)

    # Pre-render EDL quality check
    edl_issues = validate_edl(edl, strict=False)
    edl_errors = [i for i in edl_issues if i["level"] == "error"]
    edl_warnings = [i for i in edl_issues if i["level"] == "warning"]
    if edl_warnings:
        for issue in edl_warnings:
            _log(f"  EDL WARNING: {issue['message']}")
    if edl_errors:
        for issue in edl_errors:
            _log(f"  EDL ERROR: {issue['message']}")
        raise ValueError(
            f"EDL validation failed with {len(edl_errors)} error(s): "
            + "; ".join(i["message"] for i in edl_errors)
        )
    _log(f"EDL validation passed ({len(edl.all_items())} items, "
         f"{len(edl.segments)} segments, ~{edl.estimated_duration():.0f}s)")

    clips_dir = cfg.clips_dir
    output_dir = cfg.output_dir
    output_path = output_dir / f"vlog_v{version}.mp4"

    # Clean stale clips from previous EDL versions
    if clips_dir.exists():
        for old_clip in clips_dir.glob("*.mp4"):
            old_clip.unlink()
        _log(f"Cleaned stale clips from {clips_dir}")

    w, h = resolution
    _fps = fps
    lang = edl.language

    ctx = init_context(quality=quality)

    # Log all FFmpeg commands to output/ffmpeg_commands.log
    ffmpeg_log = logging.getLogger("pipeline.ffmpeg")
    log_path = output_dir / "ffmpeg_commands.log"
    _fh = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    _fh.setLevel(logging.INFO)
    ffmpeg_log.addHandler(_fh)

    try:
        return _assemble_inner(cfg, edl, version, w, h, _fps, lang, clips_dir,
                               output_dir, output_path, progress_callback, skip_broken, ctx)
    finally:
        ffmpeg_log.removeHandler(_fh)
        _fh.close()
        _log(f"FFmpeg commands logged to: {log_path}")


def _assemble_inner(cfg, edl, version, w, h, fps, lang, clips_dir,
                    output_dir, output_path, progress_callback, skip_broken, ctx=None):
    _log = logger.info
    _fps = fps

    # Beat sync: snap transitions to music beats (before rendering clips)
    if edl.music and Path(edl.music.file).exists():
        beat_snap_edl(edl, Path(edl.music.file))

    # Determine parallel workers based on encoder type
    encoder = ctx.get_encoder(w, h, _fps)
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
    report = RenderReport()

    pbar = tqdm(total=total_items, desc=f"Rendering clips (x{max_workers})", unit="clip",
                bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]",
                file=sys.stdout, disable=not sys.stdout.isatty())

    def _do_render(order, seg_idx, item_idx, item, segment):
        clip_name = f"seg{seg_idx:02d}_item{item_idx:02d}.mp4"
        clip_path = clips_dir / clip_name

        if not clip_path.exists():
            source = Path(item.source_file)
            if not source.exists():
                return clip_name, item.source_file, None, "source not found"

            ct = segment.color_temp
            if item.media_type == "photo":
                render_photo(item, clip_path, w, h, _fps, color_temp=ct,
                             text_overlay=item.text_overlay, language=lang, ctx=ctx)
            else:
                render_video(item, clip_path, w, h, _fps, color_temp=ct,
                             text_overlay=item.text_overlay, language=lang, ctx=ctx)

        if not clip_path.exists():
            return clip_name, item.source_file, None, "render failed"
        return clip_name, item.source_file, clip_path, ""

    def _progress(done, total):
        pbar.n = done
        pbar.refresh()
        if progress_callback:
            progress_callback(done, total, "")

    parallel_tasks = [
        (t[0], lambda t=t: _do_render(*t))
        for t in tasks
    ]
    results = run_parallel(parallel_tasks, max_workers, progress_fn=_progress)

    for order, result in results:
        if isinstance(result, Exception):
            report.clips.append(ClipStatus(f"task_{order}", "", "failed", str(result)))
            pbar.write(f"  FAIL: task_{order}: {result}")
        else:
            clip_name, source_file, clip_path, reason = result
            if clip_path is None:
                report.clips.append(ClipStatus(clip_name, source_file, "skipped", reason))
                pbar.write(f"  SKIP: {clip_name} ({reason})")
            else:
                report.clips.append(ClipStatus(clip_name, source_file, "ok"))
                clip_results[order] = clip_path

    pbar.close()
    t_clips = time.monotonic() - t1
    _log(f"Phase 1 (clips): {t_clips:.1f}s ({max_workers} workers, {report.summary()})")

    if report.failed_count + report.skipped_count > 0 and not skip_broken:
        raise RuntimeError(f"Clip rendering issues: {report.summary()}")

    # Build all_clips list with transitions (must be in order)
    all_clips: list[dict] = []
    idx = 0
    for seg_idx, segment in enumerate(edl.segments):
        for item_idx, item in enumerate(segment.items):
            clip_path = clip_results[idx]
            idx += 1
            if clip_path is None:
                continue

            # Skip zero-length clips (e.g. from failed renders)
            clip_dur = probe_duration(clip_path)
            if clip_dur is not None and clip_dur < 0.1:
                _log(f"  Skipping zero-length clip: {clip_path.name}")
                continue

            is_montage = segment.mode == "montage"
            if is_montage:
                transition = "cut"
                td = 0.0
            elif item_idx == 0 and seg_idx > 0:
                # Inter-segment transition: from EDL, not hardcoded
                transition = segment.segment_transition
                td = segment.segment_transition_duration
            elif item_idx > 0:
                transition = segment.transition
                td = segment.transition_duration if transition != "cut" else 0.0
            else:
                transition = "cut"
                td = 0.0

            # Safety net: photo crossfades cause ghosting. Log when triggered.
            prev_is_photo = all_clips and all_clips[-1].get("media_type") == "photo"
            if ((item.media_type == "photo" or prev_is_photo)
                    and transition not in ("cut", "fade_black")):
                _log(f"  Safety net: {transition}→fade_black for photo transition "
                     f"({Path(clip_path).name})")
                transition = "fade_black"
                td = min(td, 0.4)

            all_clips.append({
                "path": clip_path,
                "duration": item.display_duration,
                "transition": transition,
                "transition_duration": td,
                "keep_audio": item.keep_audio,
                "media_type": item.media_type,
            })

    if not all_clips:
        raise RuntimeError("No clips rendered — check source files in EDL")

    # Phase 1b: Render intro/outro clips
    if edl.intro_style == "title_card" and edl.title:
        intro_path = clips_dir / "intro_title.mp4"
        intro_dur = edl.intro_duration
        if not intro_path.exists():
            render_title_card(edl.title, edl.date_range, intro_path, w, h, _fps, duration=intro_dur, language=lang, ctx=ctx)
        if intro_path.exists():
            all_clips.insert(0, {
                "path": intro_path, "duration": intro_dur,
                "transition": "cut", "transition_duration": 0.0,
                "keep_audio": False, "media_type": "video",
            })
            if len(all_clips) > 1:
                all_clips[1]["transition"] = "fade_black"
                all_clips[1]["transition_duration"] = 1.0

    if edl.outro_style == "fade_title" and edl.title:
        outro_path = clips_dir / "outro_title.mp4"
        outro_dur = edl.outro_duration
        if not outro_path.exists():
            render_title_card(edl.title, "", outro_path, w, h, _fps, duration=outro_dur, language=lang, ctx=ctx)
        if outro_path.exists():
            all_clips.append({
                "path": outro_path, "duration": outro_dur,
                "transition": "fade_black", "transition_duration": 1.0,
                "keep_audio": False, "media_type": "video",
            })

    # Phase 2: Concatenate with transitions (video only)
    t2 = time.monotonic()
    _log(f"Concatenating {len(all_clips)} clips...")
    no_music_path = output_dir / f"vlog_v{version}_nomix.mp4"
    concatenate(all_clips, no_music_path, w, h, _fps)
    _log(f"Phase 2 (concat): {time.monotonic() - t2:.1f}s")

    # Phase 2b: Build speech audio track using Timeline as single source of truth
    # Build timeline with MEASURED group durations (fixes speech sync drift)
    tl = Timeline.build_actual(all_clips, output_dir)
    tl.dump()

    speech_audio_path = None
    speech_ka_indices = [i for i, c in enumerate(all_clips) if c.get("keep_audio")]
    if speech_ka_indices:
        video_dur = probe_duration(no_music_path)

        speech_clips = []
        for e in tl.speech_entries():
            speech_clips.append((e.video_offset, e.path))

        speech_audio_path = output_dir / f"vlog_v{version}_speech.wav"
        build_speech_track(speech_clips, video_dur, speech_audio_path)
        _log(f"Speech track: {len(speech_clips)} clips at "
              f"{', '.join(f'{o:.1f}s' for o, _ in speech_clips)}")

    speech_ranges = tl.speech_ranges()

    # Phase 3: Mix music + speech (delegated to audio module)
    t3 = time.monotonic()
    if edl.music and Path(edl.music.file).exists():
        music_dur = probe_duration(Path(edl.music.file))
        video_dur = probe_duration(no_music_path)
        _log(f"Mixing music: video={video_dur:.1f}s, music={music_dur:.1f}s, "
              f"volume={edl.music.volume}, fade_in={edl.music.fade_in}s, fade_out={edl.music.fade_out}s")
    mix_final_audio(no_music_path, output_path,
                    music_track=edl.music, speech_audio=speech_audio_path,
                    speech_ranges=speech_ranges, duck_ratio=edl.music_duck_ratio)
    _log(f"Phase 3 (audio): {time.monotonic() - t3:.1f}s")

    duration = probe_duration(output_path)
    total_time = time.monotonic() - t1
    _log(f"Done: {output_path} ({duration:.1f}s, rendered in {total_time:.0f}s)")

    # Generate YouTube chapter markers (using Timeline offsets)
    chapters_path = output_dir / f"chapters_v{version}.txt"
    write_chapters(edl, all_clips, chapters_path, timeline=tl)

    # Phase 4: Validate output
    has_speech = bool(speech_ka_indices)
    validation_issues = _validate_output(output_path, edl, has_speech, (w, h))
    errors = [i for i in validation_issues if i["level"] == "error"]
    warnings = [i for i in validation_issues if i["level"] == "warning"]

    if warnings:
        _log(f"Validation: {len(warnings)} warning(s)")
        for issue in warnings:
            _log(f"  WARNING [{issue['check']}]: {issue['message']}")
    if errors:
        _log(f"Validation: {len(errors)} error(s)")
        for issue in errors:
            _log(f"  ERROR [{issue['check']}]: {issue['message']}")
        raise RuntimeError(
            f"Output validation failed with {len(errors)} error(s): "
            + "; ".join(i["message"] for i in errors)
        )

    if not validation_issues:
        _log("Validation: all checks passed")

    return output_path, validation_issues


# ---------------------------------------------------------------------------
# Post-assemble output validation
# ---------------------------------------------------------------------------

def _validate_output(
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
            # Audio shorter than video is normal — speech track only
            # covers keep_audio clips. Only warn if audio is LONGER.
            if aud_stream_dur > vid_stream_dur + 5.0:
                _warn("av_sync",
                      f"Audio longer than video: "
                      f"video={vid_stream_dur:.1f}s, audio={aud_stream_dur:.1f}s "
                      f"— possible sync issue")
        except (ValueError, TypeError) as e:
            _warn("av_sync", f"Could not parse stream durations for sync check: {e}")

    # --- 6. Resolution check ---
    if has_video_stream:
        get_context()._dim_cache.pop(str(output_path), None)
        out_w, out_h = get_context().probe_dimensions(output_path)
        exp_w, exp_h = resolution
        if out_w > 0 and out_h > 0:
            if out_w != exp_w or out_h != exp_h:
                _warn("resolution",
                      f"Output resolution {out_w}x{out_h} does not match "
                      f"expected {exp_w}x{exp_h}")

    return issues

