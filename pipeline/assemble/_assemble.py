"""Stage 4: Render the vlog from an EDL using a single FFmpeg invocation.

Generates a filter_complex_script from the EDL, then runs one FFmpeg process
that reads all source files, applies per-item filters, concatenates, and mixes audio.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from ..config import Config
from ..edl import EDL, load_latest_edl, validate_edl
from ..media_utils import run_subprocess
from ._audio import beat_snap_edl, write_chapters
from ._encoder import RenderContext
from ._graph import build_filter_graph
from ._render import render_title_card

logger = logging.getLogger("vlog.assemble")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class AssembleConfig:
    """CLI-facing input configuration for the assemble stage."""

    w: int
    h: int
    fps: int
    quality: float = 1.0
    version: int | None = None
    edl_path: str | None = None

    def __post_init__(self) -> None:
        if self.w <= 0 or self.h <= 0:
            raise ValueError(f"Invalid resolution: {self.w}x{self.h}")
        if self.w % 2 != 0 or self.h % 2 != 0:
            raise ValueError(f"Resolution must be even: {self.w}x{self.h}")
        if self.fps <= 0 or self.fps > 120:
            raise ValueError(f"Invalid fps: {self.fps}")
        if self.quality <= 0 or self.quality > 5:
            raise ValueError(f"Invalid quality: {self.quality}")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def assemble(cfg: Config, ac: AssembleConfig, *, progress_callback=None) -> tuple[Path, list[dict]]:
    """Render a vlog from an EDL in a single FFmpeg pass.

    Returns (output_path, validation_issues).
    """
    cfg.ensure_dirs()

    # Load EDL
    version = ac.version or 0
    if version > 0:
        edl_file = cfg.edl_path(version)
        if not edl_file.exists():
            raise FileNotFoundError(f"EDL not found: {edl_file}")
        edl = EDL.model_validate_json(edl_file.read_text())
    else:
        edl, version = load_latest_edl(cfg)

    # Validate EDL
    edl_issues = validate_edl(edl, strict=False)
    edl_errors = [i for i in edl_issues if i["level"] == "error"]
    edl_warnings = [i for i in edl_issues if i["level"] == "warning"]
    for issue in edl_warnings:
        logger.info(f"  EDL WARNING: {issue['message']}")
    if edl_errors:
        for issue in edl_errors:
            logger.info(f"  EDL ERROR: {issue['message']}")
        raise ValueError(
            f"EDL validation failed with {len(edl_errors)} error(s): "
            + "; ".join(i["message"] for i in edl_errors)
        )
    logger.info(
        f"EDL validation passed ({len(edl.all_items())} items, "
        f"{len(edl.segments)} segments, ~{edl.estimated_duration():.0f}s)"
    )

    ctx = RenderContext(w=ac.w, h=ac.h, fps=ac.fps, quality=ac.quality)
    res_label = f"{ac.h}p{ac.fps}"
    output_dir = cfg.output_dir
    output_path = output_dir / f"vlog_v{version}_{res_label}.mp4"

    # FFmpeg command log
    ffmpeg_log = logging.getLogger("pipeline.ffmpeg")
    log_path = output_dir / "ffmpeg_commands.log"
    _fh = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    _fh.setLevel(logging.INFO)
    ffmpeg_log.addHandler(_fh)

    try:
        return _assemble_inner(cfg, edl, version, ctx, res_label, output_path, progress_callback)
    finally:
        ffmpeg_log.removeHandler(_fh)
        _fh.close()
        logger.info(f"FFmpeg commands logged to: {log_path}")


def _assemble_inner(cfg, edl, version, ctx, res_label, output_path, progress_callback):
    t_start = time.monotonic()
    output_dir = cfg.output_dir

    # Beat sync
    if edl.music and Path(edl.music.file).exists():
        beat_snap_edl(edl, Path(edl.music.file))

    # Render title cards (2 small FFmpeg calls — generated content, can't be in the graph)
    intro_path = None
    if edl.intro_style == "title_card" and edl.title:
        intro_path = cfg.clips_dir / f"intro_title_{res_label}.mp4"
        if not intro_path.exists():
            bg_photo = _find_first_photo(edl)
            render_title_card(
                edl.title, edl.date_range, intro_path,
                duration=edl.intro_duration, language=edl.language, ctx=ctx,
                background_photo=bg_photo,
            )
        if not intro_path.exists():
            raise RuntimeError(f"Intro title card render failed: {intro_path}")

    outro_path = None
    if edl.outro_style == "fade_title" and edl.title:
        outro_path = cfg.clips_dir / f"outro_title_{res_label}.mp4"
        if not outro_path.exists():
            render_title_card(edl.title, "", outro_path, duration=edl.outro_duration, language=edl.language, ctx=ctx)
        if not outro_path.exists():
            raise RuntimeError(f"Outro title card render failed: {outro_path}")

    if progress_callback:
        progress_callback(1, 3, "title cards ready")

    # Build filter graph
    music_path = Path(edl.music.file) if edl.music and Path(edl.music.file).exists() else None
    graph = build_filter_graph(
        edl, ctx,
        title_card_path=intro_path,
        outro_card_path=outro_path,
        music_path=music_path,
        music_volume=edl.music.volume if edl.music else 0.15,
        music_fade_in=edl.music.fade_in if edl.music else 2.0,
        music_fade_out=edl.music.fade_out if edl.music else 3.0,
        duck_ratio=edl.music_duck_ratio,
        language=edl.language,
    )

    # Write filter graph script
    script_path = output_dir / f"filter_graph_v{version}_{res_label}.txt"
    script_path.write_text(graph.script, encoding="utf-8")
    logger.info(f"Filter graph: {len(graph.inputs)} inputs, {len(graph.script)} chars → {script_path.name}")

    if progress_callback:
        progress_callback(2, 3, "filter graph ready")

    # Build FFmpeg command
    enc = ctx.get_encoder()
    cmd = ["ffmpeg", "-y"]
    for inp in graph.inputs:
        cmd += inp
    cmd += ["-filter_complex_script", str(script_path)]
    cmd += ["-map", graph.video_out]
    if graph.audio_out:
        cmd += ["-map", graph.audio_out]
    cmd += [*enc, "-pix_fmt", "yuv420p"]
    if graph.audio_out:
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-an"]
    cmd += [str(output_path)]

    # Run single FFmpeg
    logger.info(f"Rendering {len(graph.inputs)} inputs → {output_path.name} ...")
    t_render = time.monotonic()
    result = run_subprocess(cmd, capture_output=True, text=True, timeout=1800)
    if result.returncode != 0:
        raise RuntimeError(f"Render failed (rc={result.returncode}): {result.stderr[-500:]}")

    duration = ctx.probe_duration(output_path) or 0.0
    render_time = time.monotonic() - t_render
    total_time = time.monotonic() - t_start
    logger.info(f"Done: {output_path} ({duration:.1f}s, rendered in {render_time:.0f}s, total {total_time:.0f}s)")

    if progress_callback:
        progress_callback(3, 3, "done")

    # YouTube chapters
    chapters_path = output_dir / f"chapters_v{version}_{res_label}.txt"
    write_chapters(edl, [], chapters_path)

    # Validate
    has_speech = any(item.keep_audio for item in edl.all_items())
    issues = _validate_output(output_path, edl, has_speech, (ctx.w, ctx.h), ctx=ctx)
    errors = [i for i in issues if i["level"] == "error"]
    warnings = [i for i in issues if i["level"] == "warning"]
    if warnings:
        logger.info(f"Validation: {len(warnings)} warning(s)")
        for issue in warnings:
            logger.info(f"  WARNING [{issue['check']}]: {issue['message']}")
    if errors:
        logger.info(f"Validation: {len(errors)} error(s)")
        for issue in errors:
            logger.info(f"  ERROR [{issue['check']}]: {issue['message']}")
    else:
        logger.info("Validation: all checks passed")

    size_mb = output_path.stat().st_size / 1024 / 1024
    logger.info(f"Assemble: {output_path.name} ({size_mb:.1f}MB) in {total_time:.0f}s")

    return output_path, issues


def _find_first_photo(edl: EDL) -> str | None:
    """Find the first photo in the EDL for hero-photo title card background."""
    from ..image_utils import convert_heic

    for seg in edl.segments:
        for item in seg.items:
            if item.media_type == "photo":
                bg_path = Path(item.source_file)
                if bg_path.suffix.lower() in {".heic", ".heif"}:
                    bg_path = convert_heic(bg_path)
                return str(bg_path)
    return None


# ---------------------------------------------------------------------------
# Validation (kept from original)
# ---------------------------------------------------------------------------


def _validate_output(
    output_path: Path,
    edl: EDL,
    has_speech: bool,
    resolution: tuple[int, int],
    ctx: RenderContext | None = None,
) -> list[dict]:
    """Validate the rendered output video. Returns a list of issue dicts."""
    issues: list[dict] = []

    def _error(check: str, msg: str) -> None:
        issues.append({"level": "error", "check": check, "message": msg})

    def _warn(check: str, msg: str) -> None:
        issues.append({"level": "warning", "check": check, "message": msg})

    # 1. File existence and size
    if not output_path.exists():
        _error("file", f"Output file does not exist: {output_path}")
        return issues
    if output_path.stat().st_size < 1024:
        _error("file", f"Output file suspiciously small: {output_path.stat().st_size} bytes")
        return issues

    # 2. Duration check
    actual_dur = ctx.probe_duration(output_path) if ctx else 0.0
    if actual_dur == 0:
        _error("duration", "Could not probe output duration (ffprobe returned 0)")
    else:
        expected = edl.estimated_duration()
        if expected > 0:
            ratio = actual_dur / expected
            if ratio < 0.5:
                _error("duration", f"Output duration {actual_dur:.1f}s is <50% of expected {expected:.1f}s")
            elif ratio < 0.8:
                _warn("duration", f"Output duration {actual_dur:.1f}s is <80% of expected {expected:.1f}s")

    # 3. Stream validation
    stream_result = run_subprocess(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,duration", "-of", "csv=p=0",
         str(output_path)],
        capture_output=True, text=True,
    )
    has_video = False
    has_audio = False
    video_codec = ""
    for line in stream_result.stdout.strip().split("\n"):
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2:
            codec_name, codec_type = parts[0], parts[1]
            if codec_type == "video":
                has_video = True
                video_codec = codec_name
            elif codec_type == "audio":
                has_audio = True

    if not has_video:
        _error("streams", "No video stream in output")
    if not has_audio:
        if has_speech or (edl.music and Path(edl.music.file).exists()):
            _warn("streams", "No audio stream in output (expected: speech or music)")

    # 4. Codec check
    if video_codec and video_codec not in ("hevc", "h264", "h265"):
        _warn("codec", f"Unexpected video codec: {video_codec}")

    # 5. A/V sync (rough check: audio duration vs video duration)
    if has_audio and actual_dur > 0:
        audio_dur = 0.0
        for line in stream_result.stdout.strip().split("\n"):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 3 and parts[1] == "audio":
                try:
                    audio_dur = float(parts[2])
                except ValueError:
                    pass
        if audio_dur > 0 and abs(audio_dur - actual_dur) > 5:
            _warn("av_sync", f"Audio ({audio_dur:.1f}s) and video ({actual_dur:.1f}s) differ by >{5}s")

    # 6. Resolution check
    w, h = resolution
    dims = ctx.probe_dimensions(output_path) if ctx else (0, 0)
    if dims != (0, 0) and dims != (w, h):
        _warn("resolution", f"Output resolution {dims[0]}x{dims[1]} != expected {w}x{h}")

    return issues
