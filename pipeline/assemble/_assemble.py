"""Stage 4: Render the vlog from an EDL.

7 FFmpeg calls total:
  Phase 1: 6 per-segment renders (filter_complex_script + concat=v=1:a=1) → .ts
  Phase 2: 1 MPEG-TS concat (copy) + music overlay → .mp4
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from ..config import Config, ProgressCallback
from ..edl import EDL, load_latest_edl, validate_edl
from ..utils.media import run_subprocess
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

    def __post_init__(self) -> None:
        if self.w <= 0 or self.h <= 0:
            raise ValueError(f"Invalid resolution: {self.w}x{self.h}")
        if self.w % 2 != 0 or self.h % 2 != 0:
            raise ValueError(f"Resolution must be even: {self.w}x{self.h}")
        if self.fps <= 0 or self.fps > 120:
            raise ValueError(f"Invalid fps: {self.fps}")
        if self.quality <= 0 or self.quality > 5:
            raise ValueError(f"Invalid quality: {self.quality}")


def assemble(
    cfg: Config, ac: AssembleConfig, *, progress_callback: ProgressCallback = None
) -> tuple[Path, list[dict[str, str]]]:
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
        level = "ERROR" if i["level"] == "error" else "WARNING"
        logger.info("  EDL %s: %s", level, i["message"])
    if errors:
        raise ValueError(
            f"EDL validation failed: {'; '.join(i['message'] for i in errors)}"
        )
    logger.info(
        "EDL: %d items, %d segments, ~%.0fs",
        len(edl.all_items()),
        len(edl.segments),
        edl.estimated_duration(),
    )

    ctx = RenderContext(w=ac.w, h=ac.h, fps=ac.fps, quality=ac.quality)
    res_label = f"{ac.h}p{ac.fps}"
    output_path = cfg.output_dir / f"vlog_v{version}_{res_label}.mp4"

    # Music file check
    has_music = edl.music and Path(edl.music.file).exists()
    if edl.music_mode == "auto" and not has_music:
        logger.warning(
            "music_mode=auto but music file missing: %s — "
            "run 'vlog plan' with --music auto to generate, or use --music none",
            edl.music.file if edl.music else "(not set)",
        )

    # Beat sync
    if has_music:
        beat_snap_edl(edl, Path(edl.music.file))

    t_start = time.monotonic()

    # Phase 1: render segments
    segment_files = _render_segments(
        edl, ctx, cfg, res_label=res_label, progress_callback=progress_callback
    )

    # Phase 2: concat + music mix
    _concat_and_mix(
        segment_files,
        edl,
        ctx,
        cfg,
        output_path,
        version=version,
        res_label=res_label,
        progress_callback=progress_callback,
    )

    total_time = time.monotonic() - t_start

    # Validate
    if progress_callback:
        progress_callback(0, 0, "validating output...")

    final_dur = ctx.probe_duration(output_path) or 0.0
    logger.info(
        "Done: %s (%.1fs, rendered in %.0fs)", output_path.name, final_dur, total_time
    )

    has_speech = any(item.keep_audio for item in edl.all_items())
    val_issues = _validate_output(output_path, edl, has_speech, (ctx.w, ctx.h), ctx=ctx)
    for i in val_issues:
        level = "ERROR" if i["level"] == "error" else "WARNING"
        logger.info("  %s [%s]: %s", level, i["check"], i["message"])
    if not any(i["level"] == "error" for i in val_issues):
        logger.info("Validation: all checks passed")

    # Rich validation panel to terminal
    try:
        import sys

        if sys.stderr.isatty():
            from rich.console import Console
            from rich.panel import Panel
            from rich.text import Text

            lines = Text()
            for vi in val_issues:
                icon = "\u2717" if vi["level"] == "error" else "\u26a0"
                style = "red" if vi["level"] == "error" else "yellow"
                lines.append(f" {icon} ", style=style)
                lines.append(f"[{vi['check']}] {vi['message']}\n")
            if not val_issues:
                lines.append(" \u2713 All checks passed\n", style="green")
            has_errors = any(vi["level"] == "error" for vi in val_issues)
            Console(stderr=True).print(
                Panel(
                    lines,
                    title="Validation",
                    border_style="red" if has_errors else "green",
                )
            )
    except ImportError:
        pass

    return output_path, val_issues


# ---------------------------------------------------------------------------
# Phase 1: Per-segment render
# ---------------------------------------------------------------------------


def _render_segments(
    edl: EDL,
    ctx: RenderContext,
    cfg: Config,
    *,
    res_label: str,
    progress_callback: ProgressCallback = None,
) -> list[Path]:
    """Build segment filter graphs and encode segments in parallel.

    Returns list of segment .ts file paths.
    """
    t_start = time.monotonic()
    logger.info("Phase 1: Rendering %d segments...", len(edl.segments))

    output_dir = cfg.output_dir

    # Title cards
    intro_path = _render_title_card_if_needed(
        edl, "intro", cfg.clips_dir / f"intro_title_{res_label}.mp4", ctx, res_label
    )
    outro_path = _render_title_card_if_needed(
        edl, "outro", cfg.clips_dir / f"outro_title_{res_label}.mp4", ctx, res_label
    )

    fade_params = compute_fade_params(edl)
    segment_files: list[Path] = [
        output_dir / f"_seg_{i}_{res_label}.ts" for i in range(len(edl.segments))
    ]

    # Build per-segment FFmpeg commands (must be sequential — graph needs ctx.probe)
    segment_cmds: list[tuple[int, list[str]]] = []
    for seg_idx, segment in enumerate(edl.segments):
        graph = build_segment_graph(
            segment,
            ctx,
            fade_params=fade_params[seg_idx],
            language=edl.language,
            title_card_path=intro_path if seg_idx == 0 else None,
            outro_card_path=outro_path if seg_idx == len(edl.segments) - 1 else None,
            intro_duration=edl.intro_duration if seg_idx == 0 else 0.0,
            outro_duration=edl.outro_duration
            if seg_idx == len(edl.segments) - 1
            else 0.0,
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
        logger.info(
            "  Segment %d: %d items, %d inputs",
            seg_idx,
            len(segment.items),
            len(graph.inputs),
        )

    # Render segments in parallel (3 NVENC sessions max)
    from ..utils.parallel import run_parallel

    max_workers = 3 if "nvenc" in " ".join(ctx.get_encoder()) else 2

    if progress_callback:
        progress_callback(0, 0, "rendering segments...")

    def _render_seg(seg_idx, cmd):
        result = run_subprocess(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise RuntimeError(f"Segment {seg_idx} failed: {result.stderr}")
        return seg_idx

    def _on_seg_done(done, total):
        if progress_callback:
            progress_callback(done, total, "render segments")

    tasks = [
        (idx, lambda idx=idx, cmd=cmd: _render_seg(idx, cmd))
        for idx, cmd in segment_cmds
    ]
    results = run_parallel(tasks, max_workers, progress_fn=_on_seg_done)

    for seg_idx, result in results:
        if isinstance(result, Exception):
            raise RuntimeError(f"Segment {seg_idx} render failed: {result}")
        dur = ctx.probe_duration(segment_files[seg_idx]) or 0.0
        logger.info("  Segment %d: %.1fs", seg_idx, dur)

    t_phase1 = time.monotonic() - t_start
    logger.info("Phase 1: %.0fs (%d segments)", t_phase1, len(segment_files))

    return segment_files


# ---------------------------------------------------------------------------
# Phase 2: Concat + music mix
# ---------------------------------------------------------------------------


def _concat_and_mix(
    segment_files: list[Path],
    edl: EDL,
    ctx: RenderContext,
    cfg: Config,
    output_path: Path,
    *,
    version: int,
    res_label: str,
    progress_callback: ProgressCallback = None,
) -> Path:
    """Concat demuxer + music overlay. Returns final output path."""
    output_dir = cfg.output_dir

    logger.info("Phase 2: Concat + music...")
    if progress_callback:
        progress_callback(0, 0, "concatenating segments...")

    # Concat (video + speech audio copy, no re-encode)
    nomix_path = output_path  # if no music, this is the final output
    has_music = edl.music and Path(edl.music.file).exists()

    if has_music:
        nomix_path = output_dir / f"vlog_v{version}_{res_label}_nomix.mp4"

    # Concat demuxer with TS files.  TS has inline timestamps so the demuxer
    # adjusts PTS/DTS correctly across segments (unlike MP4 concat which fails
    # with HEVC B-frames, and unlike TS byte-concat which creates DTS collisions).
    list_path = output_dir / f"_concat_{res_label}.txt"
    with open(list_path, "w") as f:
        for seg_file in segment_files:
            safe = str(seg_file.resolve()).replace("\\", "/")
            f.write(f"file '{safe}'\n")

    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_path),
        "-c:v",
        "copy",
        "-c:a",
        "copy",
        str(nomix_path),
    ]
    result = run_subprocess(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise RuntimeError(f"Concat failed: {result.stderr}")

    total_dur = ctx.probe_duration(nomix_path) or 0.0
    logger.info("  Concat: %.1fs", total_dur)

    # Music overlay
    if has_music:
        assert edl.music is not None  # narrowing for type checker
        if progress_callback:
            progress_callback(0, 0, "mixing music...")
        music_path = Path(edl.music.file)
        music_dur = ctx.probe_duration(music_path) or total_dur
        vol = edl.music.volume
        fade_in = edl.music.fade_in
        fade_out = edl.music.fade_out

        music_chain = "[1:a] "
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
            f"[bg][sp] sidechaincompress="
            f"threshold=0.02:ratio=6:attack=200:release=1000 [ducked];\n"
            f"[sp][ducked] amix=inputs=2:duration=first,"
            f"loudnorm=I=-16:TP=-1.5:LRA=11 [aout]"
        )

        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(nomix_path),
            "-i",
            str(music_path),
            "-filter_complex",
            fc,
            "-map",
            "0:v",
            "-map",
            "[aout]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            str(output_path),
        ]
        result = run_subprocess(cmd, capture_output=True, text=True, timeout=600)
        if not output_path.exists() or output_path.stat().st_size < 1024:
            raise RuntimeError(f"Music mix failed: {result.stderr}")

        nomix_path.unlink(missing_ok=True)

    # Chapters (before cleanup — needs segment files for durations)
    chapters_path = output_dir / f"chapters_v{version}_{res_label}.txt"
    seg_durations = [ctx.probe_duration(f) or 0.0 for f in segment_files]
    write_chapters(edl, seg_durations, chapters_path)

    # Clean up segment files
    for seg_file in segment_files:
        seg_file.unlink(missing_ok=True)

    return output_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render_title_card_if_needed(
    edl: EDL, kind: str, path: Path, ctx: RenderContext, res_label: str
) -> Path | None:
    if kind == "intro" and edl.intro_style == "title_card" and edl.title:
        if not path.exists():
            bg = _find_first_photo(edl)
            render_title_card(
                edl.title,
                edl.date_range,
                path,
                duration=edl.intro_duration,
                language=edl.language,
                ctx=ctx,
                background_photo=bg,
            )
        if not path.exists():
            raise RuntimeError(f"Intro title card render failed: {path}")
        return path
    elif kind == "outro" and edl.outro_style == "fade_title" and edl.title:
        if not path.exists():
            render_title_card(
                edl.title,
                "",
                path,
                duration=edl.outro_duration,
                language=edl.language,
                ctx=ctx,
            )
        if not path.exists():
            raise RuntimeError(f"Outro title card render failed: {path}")
        return path
    return None


def _find_first_photo(edl: EDL) -> str | None:
    from ..utils.image import convert_heic

    for seg in edl.segments:
        for item in seg.items:
            if item.media_type == "photo":
                p = Path(item.source_file)
                if p.suffix.lower() in {".heic", ".heif"}:
                    p = convert_heic(p)
                return str(p)
    return None


def _validate_output(
    output_path: Path,
    edl: EDL,
    has_speech: bool,
    resolution: tuple[int, int],
    ctx: RenderContext | None = None,
) -> list[dict[str, str]]:
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
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name",
            "-of",
            "csv=p=0",
            str(output_path),
        ],
        capture_output=True,
        text=True,
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
