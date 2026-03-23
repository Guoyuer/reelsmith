"""Stage 4: Render the vlog from an EDL.

7 FFmpeg calls total:
  Phase 1: 6 per-segment renders (filter_complex_script + concat=v=1:a=1)
  Phase 2: 1 final concat (demuxer copy) + music overlay
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
from ._graph import build_segment_graph, compute_fade_params
from ._render import render_title_card

logger = logging.getLogger("vlog.assemble")


@dataclass
class AssembleConfig:
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


def assemble(cfg: Config, ac: AssembleConfig, *, progress_callback=None) -> tuple[Path, list[dict]]:
    """Render a vlog from an EDL. Returns (output_path, validation_issues)."""
    cfg.ensure_dirs()

    # Load & validate EDL
    version = ac.version or 0
    if version > 0:
        edl = EDL.model_validate_json(cfg.edl_path(version).read_text())
    else:
        edl, version = load_latest_edl(cfg)

    issues = validate_edl(edl, strict=False)
    errors = [i for i in issues if i["level"] == "error"]
    for i in issues:
        logger.info(f"  EDL {'ERROR' if i['level'] == 'error' else 'WARNING'}: {i['message']}")
    if errors:
        raise ValueError(f"EDL validation failed: {'; '.join(i['message'] for i in errors)}")
    logger.info(f"EDL: {len(edl.all_items())} items, {len(edl.segments)} segments, ~{edl.estimated_duration():.0f}s")

    ctx = RenderContext(w=ac.w, h=ac.h, fps=ac.fps, quality=ac.quality)
    res_label = f"{ac.h}p{ac.fps}"
    output_dir = cfg.output_dir
    output_path = output_dir / f"vlog_v{version}_{res_label}.mp4"

    # Beat sync
    if edl.music and Path(edl.music.file).exists():
        beat_snap_edl(edl, Path(edl.music.file))

    # --- Phase 1: Per-segment render ---
    t_start = time.monotonic()
    logger.info(f"Phase 1: Rendering {len(edl.segments)} segments...")

    # Title cards
    intro_path = _render_title_card_if_needed(
        edl, "intro", cfg.clips_dir / f"intro_title_{res_label}.mp4", ctx, res_label
    )
    outro_path = _render_title_card_if_needed(
        edl, "outro", cfg.clips_dir / f"outro_title_{res_label}.mp4", ctx, res_label
    )

    fade_params = compute_fade_params(edl)
    segment_files: list[Path] = [output_dir / f"_seg_{i}_{res_label}.mp4" for i in range(len(edl.segments))]

    # Build per-segment FFmpeg commands (must be sequential — graph needs ctx.probe)
    segment_cmds: list[tuple[int, list[str]]] = []
    for seg_idx, segment in enumerate(edl.segments):
        graph = build_segment_graph(
            segment, ctx,
            fade_params=fade_params[seg_idx],
            language=edl.language,
            title_card_path=intro_path if seg_idx == 0 else None,
            outro_card_path=outro_path if seg_idx == len(edl.segments) - 1 else None,
            intro_duration=edl.intro_duration if seg_idx == 0 else 0.0,
            outro_duration=edl.outro_duration if seg_idx == len(edl.segments) - 1 else 0.0,
        )
        script_path = output_dir / f"_seg_{seg_idx}_{res_label}.txt"
        script_path.write_text(graph.script, encoding="utf-8")

        enc = ctx.get_encoder()
        cmd = ["ffmpeg", "-y"]
        for inp in graph.inputs:
            cmd += [str(x) for x in inp]
        cmd += ["-filter_complex_script", str(script_path)]
        cmd += ["-map", "[vout]", "-map", "[aout]"]
        cmd += [*enc, "-pix_fmt", "yuv420p"]
        cmd += ["-c:a", "aac", "-b:a", "192k"]
        cmd += [str(segment_files[seg_idx])]
        segment_cmds.append((seg_idx, cmd))
        logger.info(f"  Segment {seg_idx}: {len(segment.items)} items, {len(graph.inputs)} inputs")

    # Render segments in parallel (3 NVENC sessions max)
    from ..parallel import run_parallel
    max_workers = 3 if "nvenc" in " ".join(ctx.get_encoder()) else 2

    def _render_seg(seg_idx, cmd):
        result = run_subprocess(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise RuntimeError(f"Segment {seg_idx} failed: {result.stderr[-500:]}")
        return seg_idx

    tasks = [(idx, lambda idx=idx, cmd=cmd: _render_seg(idx, cmd)) for idx, cmd in segment_cmds]
    results = run_parallel(tasks, max_workers)

    for seg_idx, result in results:
        if isinstance(result, Exception):
            raise RuntimeError(f"Segment {seg_idx} render failed: {result}")
        dur = ctx.probe_duration(segment_files[seg_idx]) or 0.0
        logger.info(f"  Segment {seg_idx}: {dur:.1f}s")

    t_phase1 = time.monotonic() - t_start
    logger.info(f"Phase 1: {t_phase1:.0f}s ({len(segment_files)} segments)")

    # --- Phase 2: Final concat + music ---
    t2 = time.monotonic()
    logger.info("Phase 2: Concat + music...")

    # Concat demuxer (video + speech audio copy, no re-encode)
    nomix_path = output_path  # if no music, this is the final output
    has_music = edl.music and Path(edl.music.file).exists()

    if has_music:
        nomix_path = output_dir / f"vlog_v{version}_{res_label}_nomix.mp4"

    list_path = output_dir / f"_concat_{res_label}.txt"
    with open(list_path, "w") as f:
        for seg_file in segment_files:
            safe = str(seg_file.resolve()).replace("\\", "/")
            f.write(f"file '{safe}'\n")

    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(list_path),
        "-c:v", "copy", "-c:a", "copy",
        "-fflags", "+genpts",
        "-avoid_negative_ts", "make_zero",
        str(nomix_path),
    ]
    result = run_subprocess(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"Concat failed: {result.stderr[-300:]}")

    total_dur = ctx.probe_duration(nomix_path) or 0.0
    logger.info(f"  Concat: {total_dur:.1f}s")

    # Music overlay
    if has_music:
        music_path = Path(edl.music.file)
        music_dur = ctx.probe_duration(music_path) or total_dur
        vol = edl.music.volume
        fade_in = edl.music.fade_in
        fade_out = edl.music.fade_out

        music_chain = f"[1:a] "
        if music_dur < total_dur:
            loops = int(total_dur / music_dur) + 1
            samples = int(music_dur * 48000)
            music_chain += f"aloop=loop={loops}:size={samples},atrim=0:{total_dur:.3f},"
        music_chain += f"volume={vol:.3f},"
        music_chain += f"afade=t=in:d={fade_in},"
        fade_out_start = max(0, total_dur - fade_out)
        music_chain += f"afade=t=out:st={fade_out_start:.3f}:d={fade_out} [bg]"

        fc = (
            f"{music_chain};\n"
            f"[0:a] apad [sp];\n"
            f"[sp][bg] amix=inputs=2:duration=first:weights=3 1,"
            f"loudnorm=I=-16:TP=-1.5:LRA=11 [aout]"
        )

        cmd = [
            "ffmpeg", "-y",
            "-i", str(nomix_path),
            "-i", str(music_path),
            "-filter_complex", fc,
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            str(output_path),
        ]
        result = run_subprocess(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0 and not output_path.exists():
            raise RuntimeError(f"Music mix failed: {result.stderr[-300:]}")
        if not output_path.exists() or output_path.stat().st_size < 1024:
            raise RuntimeError(f"Music mix produced no output: {result.stderr[-300:]}")

        nomix_path.unlink(missing_ok=True)

    t_phase2 = time.monotonic() - t2
    total_time = time.monotonic() - t_start

    # Chapters (before cleanup — needs segment files for durations)
    chapters_path = output_dir / f"chapters_v{version}_{res_label}.txt"
    seg_durations = [ctx.probe_duration(f) or 0.0 for f in segment_files]
    write_chapters(edl, seg_durations, chapters_path)

    # Clean up segment files
    for seg_file in segment_files:
        seg_file.unlink(missing_ok=True)

    if progress_callback:
        progress_callback(len(edl.segments) + 1, len(edl.segments) + 1, "done")

    final_dur = ctx.probe_duration(output_path) or 0.0
    logger.info(f"Done: {output_path.name} ({final_dur:.1f}s, rendered in {total_time:.0f}s)")

    # Validate
    has_speech = any(item.keep_audio for item in edl.all_items())
    val_issues = _validate_output(output_path, edl, has_speech, (ctx.w, ctx.h), ctx=ctx)
    for i in val_issues:
        level = "ERROR" if i["level"] == "error" else "WARNING"
        logger.info(f"  {level} [{i['check']}]: {i['message']}")
    if not [i for i in val_issues if i["level"] == "error"]:
        logger.info("Validation: all checks passed")

    size_mb = output_path.stat().st_size / 1024 / 1024
    logger.info(f"Assemble: {output_path.name} ({size_mb:.1f}MB) in {total_time:.0f}s")

    return output_path, val_issues


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render_title_card_if_needed(edl, kind, path, ctx, res_label) -> Path | None:
    if kind == "intro" and edl.intro_style == "title_card" and edl.title:
        if not path.exists():
            bg = _find_first_photo(edl)
            render_title_card(
                edl.title, edl.date_range, path,
                duration=edl.intro_duration, language=edl.language, ctx=ctx,
                background_photo=bg,
            )
        if not path.exists():
            raise RuntimeError(f"Intro title card render failed: {path}")
        return path
    elif kind == "outro" and edl.outro_style == "fade_title" and edl.title:
        if not path.exists():
            render_title_card(edl.title, "", path, duration=edl.outro_duration, language=edl.language, ctx=ctx)
        if not path.exists():
            raise RuntimeError(f"Outro title card render failed: {path}")
        return path
    return None


def _find_first_photo(edl: EDL) -> str | None:
    from ..image_utils import convert_heic
    for seg in edl.segments:
        for item in seg.items:
            if item.media_type == "photo":
                p = Path(item.source_file)
                if p.suffix.lower() in {".heic", ".heif"}:
                    p = convert_heic(p)
                return str(p)
    return None


def _validate_output(output_path, edl, has_speech, resolution, ctx=None):
    issues = []

    def _error(check, msg):
        issues.append({"level": "error", "check": check, "message": msg})

    def _warn(check, msg):
        issues.append({"level": "warning", "check": check, "message": msg})

    if not output_path.exists():
        _error("file", f"Output missing: {output_path}")
        return issues
    if output_path.stat().st_size < 1024:
        _error("file", f"Output too small: {output_path.stat().st_size} bytes")
        return issues

    actual_dur = ctx.probe_duration(output_path) if ctx else 0.0
    if actual_dur == 0:
        _error("duration", "Could not probe duration")
    else:
        expected = edl.estimated_duration()
        if expected > 0 and actual_dur / expected < 0.5:
            _error("duration", f"{actual_dur:.1f}s < 50% of expected {expected:.1f}s")
        elif expected > 0 and actual_dur / expected < 0.8:
            _warn("duration", f"{actual_dur:.1f}s < 80% of expected {expected:.1f}s")

    stream_result = run_subprocess(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name", "-of", "csv=p=0",
         str(output_path)], capture_output=True, text=True,
    )
    has_video = "video" in stream_result.stdout
    has_audio = "audio" in stream_result.stdout
    if not has_video:
        _error("streams", "No video stream")
    if not has_audio and (has_speech or (edl.music and Path(edl.music.file).exists())):
        _warn("streams", "No audio stream")

    w, h = resolution
    dims = ctx.probe_dimensions(output_path) if ctx else (0, 0)
    if dims != (0, 0) and dims != (w, h):
        _warn("resolution", f"Output {dims[0]}x{dims[1]} != expected {w}x{h}")

    return issues
