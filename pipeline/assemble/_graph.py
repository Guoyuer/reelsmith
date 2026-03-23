"""Filter graph builder — generates a filter_complex_script for single-FFmpeg rendering.

Reads the EDL and source files, builds per-item filter chains, concatenates via
the concat filter, and mixes speech + music audio. The result is a script file
that FFmpeg reads via -filter_complex_script.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from ..edl import EDL, EditItem
from ._encoder import RenderContext
from ._filters import (
    is_portrait,
    build_portrait_photo_filter,
    color_grade,
    drawtext_filter,
    portrait_bg_filter,
    zoompan_filter,
)

logger = logging.getLogger("vlog.assemble.graph")


@dataclass
class GraphInput:
    """An FFmpeg input with its -i flags."""

    index: int
    flags: list[str]  # e.g. ["-loop", "1", "-t", "5", "-i", "photo.jpg"]


@dataclass
class GraphResult:
    """Output of build_filter_graph(): inputs + script + stream mappings."""

    inputs: list[list[str]]  # per-input FFmpeg flags (before -filter_complex_script)
    script: str  # filter_complex_script content
    video_out: str  # output video stream label, e.g. "[vout]"
    audio_out: str | None  # output audio stream label, e.g. "[aout]" or None
    speech_offsets: list[tuple[float, Path]]  # (offset_s, clip_path) for speech clips


def build_filter_graph(
    edl: EDL,
    ctx: RenderContext,
    *,
    title_card_path: Path | None = None,
    outro_card_path: Path | None = None,
    music_path: Path | None = None,
    music_volume: float = 0.15,
    music_fade_in: float = 2.0,
    music_fade_out: float = 3.0,
    duck_ratio: float = 0.3,
    language: str = "en",
) -> GraphResult:
    """Build a filter_complex_script from an EDL.

    Returns a GraphResult with FFmpeg input flags, the script content,
    and output stream labels.
    """
    w, h, fps = ctx.w, ctx.h, ctx.fps
    inputs: list[list[str]] = []
    video_filters: list[str] = []  # per-clip filter chains
    speech_offsets: list[tuple[float, Path]] = []

    # --- Compute fade params (same logic as old _assemble.py) ---
    flat_items: list[tuple[int, int, EditItem, object]] = []
    for seg_idx, segment in enumerate(edl.segments):
        for item_idx, item in enumerate(segment.items):
            flat_items.append((seg_idx, item_idx, item, segment))

    fade_params: list[tuple[float, float]] = []
    for fi, (seg_idx, item_idx, item, segment) in enumerate(flat_items):
        fade_in = 0.0
        fade_out = 0.0
        is_montage = segment.mode == "montage"
        if not is_montage:
            if item_idx == 0 and seg_idx > 0:
                fade_in = segment.segment_transition_duration
            if fi + 1 < len(flat_items):
                _, next_item_idx, _, next_segment = flat_items[fi + 1]
                next_is_montage = next_segment.mode == "montage"
                if not next_is_montage:
                    if next_item_idx == 0:
                        fade_out = next_segment.segment_transition_duration
                    else:
                        fade_out = next_segment.transition_duration if next_segment.transition != "cut" else 0.0
        fade_params.append((fade_in, fade_out))

    # --- Segment pairs: each clip outputs [vN] + [aN] for concat ---
    # Clips with keep_audio use real audio; others get generated silence.
    # concat=n=N:v=1:a=1 syncs both streams — no manual offset computation.

    segment_pairs: list[tuple[str, str]] = []  # (video_label, audio_label)

    # Title card (no audio → silence)
    if title_card_path and title_card_path.exists():
        idx = len(inputs)
        inputs.append(["-i", str(title_card_path)])
        video_filters.append(f"[{idx}:v] setpts=PTS-STARTPTS [{_vlabel(idx)}]")
        video_filters.append(f"aevalsrc=0:d={edl.intro_duration}:s=48000:c=stereo [{_alabel(idx)}]")
        segment_pairs.append((f"[{_vlabel(idx)}]", f"[{_alabel(idx)}]"))

    # Per-item filter chains
    for fi, (seg_idx, item_idx, item, segment) in enumerate(flat_items):
        fade_in, fade_out = fade_params[fi]
        source = Path(item.source_file)

        if item.media_type == "photo" and source.suffix.lower() in {".heic", ".heif"}:
            from ..image_utils import convert_heic
            source = convert_heic(source)

        idx = len(inputs)

        if item.media_type == "photo":
            # Photo: use exact frame count for deterministic duration
            frames = int(item.display_duration * fps)
            exact_dur = frames / fps
            inputs.append(["-loop", "1", "-t", str(exact_dur), "-i", str(source)])
            vf = _photo_filter_chain(idx, item, segment, ctx, fade_in, fade_out, language)
            video_filters.append(vf)
            video_filters.append(
                f"aevalsrc=0:d={exact_dur}:s=48000:c=stereo,"
                f"asetpts=PTS-STARTPTS [{_alabel(idx)}]"
            )
            segment_pairs.append((f"[{_vlabel(idx)}]", f"[{_alabel(idx)}]"))
        else:
            # Video: use trim duration (not display_duration) for consistency
            duration = item.display_duration
            if item.start_time is not None and item.end_time is not None:
                duration = item.end_time - item.start_time
            inp = []
            if item.start_time is not None:
                inp += ["-ss", str(item.start_time)]
            inp += ["-i", str(source)]
            inp = ["-t", str(duration)] + inp
            inputs.append(inp)

            speed = item.playback_speed or 1.0
            output_dur = duration / speed
            vf = _video_filter_chain(idx, item, segment, ctx, fade_in, fade_out, output_dur, language)
            video_filters.append(vf)

            if item.keep_audio:
                af = f"[{idx}:a] "
                if speed != 1.0:
                    af += f"atempo={speed},"
                af += (
                    f"asetpts=PTS-STARTPTS,"
                    f"afade=t=in:d=0.3,afade=t=out:st={max(0, output_dur - 0.3):.1f}:d=0.3"
                )
                af += f" [{_alabel(idx)}]"
                video_filters.append(af)
            else:
                video_filters.append(
                    f"aevalsrc=0:d={output_dur:.3f}:s=48000:c=stereo,"
                    f"asetpts=PTS-STARTPTS [{_alabel(idx)}]"
                )

            segment_pairs.append((f"[{_vlabel(idx)}]", f"[{_alabel(idx)}]"))

    # Outro card (no audio → silence)
    if outro_card_path and outro_card_path.exists():
        idx = len(inputs)
        inputs.append(["-i", str(outro_card_path)])
        video_filters.append(f"[{idx}:v] setpts=PTS-STARTPTS [{_vlabel(idx)}]")
        video_filters.append(f"aevalsrc=0:d={edl.outro_duration}:s=48000:c=stereo [{_alabel(idx)}]")
        segment_pairs.append((f"[{_vlabel(idx)}]", f"[{_alabel(idx)}]"))

    # --- Concat video + audio together (perfect sync) ---
    n = len(segment_pairs)
    concat_inputs = "".join(f"{v}{a}" for v, a in segment_pairs)
    concat_line = f"{concat_inputs} concat=n={n}:v=1:a=1 [vout][speech_raw]"
    video_filters.append(concat_line)

    # --- Audio mixing: speech_raw from concat + optional music ---
    audio_out = "[speech_raw]"  # default: just the concat audio (speech + silence)
    music_input_idx = None

    if music_path and music_path.exists():
        music_input_idx = len(inputs)
        inputs.append(["-i", str(music_path)])

        # Estimate total duration for music looping/fading
        total_duration = edl.estimated_duration()
        music_dur = ctx.probe_duration(music_path) or total_duration

        music_chain = f"[{music_input_idx}:a] "
        if music_dur < total_duration:
            loops = int(total_duration / music_dur) + 1
            samples = int(music_dur * 48000)
            music_chain += f"aloop=loop={loops}:size={samples},atrim=0:{total_duration:.3f},"
        music_chain += f"volume={music_volume:.3f}:eval=frame,"
        music_chain += f"afade=t=in:d={music_fade_in},"
        fade_out_start = max(0, total_duration - music_fade_out)
        music_chain += f"afade=t=out:st={fade_out_start:.3f}:d={music_fade_out}"
        music_chain += " [bg]"
        video_filters.append(music_chain)

        # Mix speech (from concat) + music
        video_filters.append(
            f"[speech_raw] apad [sp];\n"
            f"[sp][bg] amix=inputs=2:duration=first:weights=3 1,"
            f"loudnorm=I=-16:TP=-1.5:LRA=11 [aout]"
        )
        audio_out = "[aout]"

    script = ";\n".join(video_filters)
    return GraphResult(
        inputs=[inp for inp in inputs],
        script=script,
        video_out="[vout]",
        audio_out=audio_out,
        speech_offsets=speech_offsets,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _vlabel(idx: int) -> str:
    return f"v{idx}"


def _alabel(idx: int) -> str:
    return f"a{idx}"


def _fade_expr(duration: float, fade_in: float, fade_out: float) -> str:
    """Build video fade + setpts filters."""
    parts = ["setpts=PTS-STARTPTS"]
    if fade_in > 0:
        parts.append(f"fade=t=in:d={fade_in}")
    if fade_out > 0:
        st = max(0, duration - fade_out)
        parts.append(f"fade=t=out:st={st:.3f}:d={fade_out}")
    return "," + ",".join(parts)


def _trim_expr(duration: float) -> str:
    """Trim video to exact duration + reset PTS. Applied AFTER fps filter.

    Skips trim for very short clips (<1s) to avoid producing 0 frames.
    """
    if duration < 1.0:
        return ""
    return f",trim=duration={duration:.6f},setpts=PTS-STARTPTS"


def _photo_filter_chain(
    idx: int, item: EditItem, segment, ctx: RenderContext,
    fade_in: float, fade_out: float, language: str,
) -> str:
    """Build filter chain for a photo input."""
    w, h, fps = ctx.w, ctx.h, ctx.fps
    frames = int(item.display_duration * fps)

    zoom_targets = {
        "ken_burns_in": 0.25, "ken_burns_out": 0.20,
        "ken_burns_left": 0.15, "ken_burns_right": 0.15, "static": 0.0,
    }
    target = zoom_targets.get(item.effect, 0.25)
    variation = (int(hashlib.md5(item.source_file.encode()).hexdigest()[:4], 16) % 10) / 100
    zoom_rate = 0.001 + ((target + variation) / frames) if target > 0 else 0

    dt = ""
    if item.text_overlay:
        dt = "," + drawtext_filter(
            item.text_overlay.text, item.text_overlay.position,
            item.text_overlay.font_size, item.display_duration, language, out_h=h,
        )

    src_w, src_h = ctx.probe_dimensions(Path(item.source_file))
    exact_dur = frames / fps
    fade = _fade_expr(exact_dur, fade_in, fade_out)
    trim = _trim_expr(exact_dur)
    cg = color_grade(segment.color_temp)
    sharpen = ",unsharp=3:3:0.5:3:3:0.0"

    if is_portrait(src_w, src_h):
        portrait_zoom_rate = 0.001 + (0.08 / frames)
        return (
            f"[{idx}:v] split [bg{idx}][fg{idx}];"
            f"[bg{idx}] scale=960:-1:force_original_aspect_ratio=increase,crop=960:540,"
            f"gblur=sigma=60,scale={w}:{h},eq=brightness=-0.15:saturation=0.6 [blurred{idx}];"
            f"[fg{idx}] scale=-1:{h} [sharp{idx}];"
            f"[blurred{idx}][sharp{idx}] overlay=(W-w)/2:(H-h)/2 [comp{idx}];"
            f"[comp{idx}] zoompan=z='1+(1.08-1)*(1-cos(PI*on/{frames}))/2':d={frames}"
            f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":s={w}x{h}:fps={fps}"
            f"{dt}{fade}{trim} [{_vlabel(idx)}]"
        )
    else:
        direction_map = {
            "ken_burns_in": "in", "ken_burns_out": "out",
            "ken_burns_left": "left", "ken_burns_right": "right", "static": "static",
        }
        direction = direction_map.get(item.effect, "in")
        zp = zoompan_filter(zoom_rate, frames, w, h, fps, direction=direction)

        ow, oh = w * 2, h * 2
        src_ratio = src_w / src_h if src_h > 0 else 1.0
        out_ratio = ow / oh

        if abs(src_ratio - out_ratio) / out_ratio < 0.05:
            return f"[{idx}:v] scale={ow}:{oh},{zp},{cg}{sharpen}{dt}{fade}{trim} [{_vlabel(idx)}]"
        else:
            return (
                f"[{idx}:v] split [bg{idx}][fg{idx}];"
                f"[bg{idx}] scale={ow}:{oh}:force_original_aspect_ratio=increase,"
                f"crop={ow}:{oh},gblur=sigma=25 [blurred{idx}];"
                f"[fg{idx}] scale={ow}:{oh}:force_original_aspect_ratio=decrease [sharp{idx}];"
                f"[blurred{idx}][sharp{idx}] overlay=(W-w)/2:(H-h)/2 [comp{idx}];"
                f"[comp{idx}] {zp},{cg}{sharpen}{dt}{fade}{trim} [{_vlabel(idx)}]"
            )


def _video_filter_chain(
    idx: int, item: EditItem, segment, ctx: RenderContext,
    fade_in: float, fade_out: float, output_dur: float, language: str,
) -> str:
    """Build filter chain for a video input."""
    w, h, fps = ctx.w, ctx.h, ctx.fps

    speed = item.playback_speed or 1.0
    speed_vf = f",setpts={1/speed:.4f}*PTS" if speed != 1.0 else ""

    dt = ""
    if item.text_overlay:
        dt = "," + drawtext_filter(
            item.text_overlay.text, item.text_overlay.position,
            item.text_overlay.font_size, item.display_duration, language, out_h=h,
        )

    src_w, src_h = ctx.probe_dimensions(Path(item.source_file))
    cg = color_grade(segment.color_temp)
    fade = _fade_expr(output_dur, fade_in, fade_out)
    trim = _trim_expr(output_dur)

    if is_portrait(src_w, src_h):
        return (
            f"[{idx}:v] split [bg{idx}][fg{idx}];"
            f"[bg{idx}] scale={w}:-1:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},gblur=sigma=60,eq=brightness=-0.15:saturation=0.6 [blurred{idx}];"
            f"[fg{idx}] scale=-1:{h} [sharp{idx}];"
            f"[blurred{idx}][sharp{idx}] overlay=(W-w)/2:(H-h)/2,"
            f"{cg}{speed_vf}{dt}{fade},fps={fps}{trim} [{_vlabel(idx)}]"
        )
    else:
        return (
            f"[{idx}:v] scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
            f"{cg}{speed_vf}{dt}{fade},fps={fps}{trim} [{_vlabel(idx)}]"
        )


def _ducking_expression(
    base_vol: float, duck_ratio: float,
    speech_offsets: list[tuple[float, Path]], total_dur: float,
) -> str:
    """Build the music volume expression with ducking ramps."""
    if not speech_offsets:
        return f"{base_vol:.3f}"
    vol_expr = f"{base_vol:.3f}"
    for offset, path in speech_offsets:
        # Estimate speech duration from clip (conservative: use 10s max)
        end = offset + 10.0  # will be bounded by actual clip duration
        attack_start = max(0, offset - 0.3)
        gain = f"clip((t-{attack_start:.1f})/0.3,0,1)-clip((t-{end:.1f})/1.0,0,1)"
        vol_expr = f"({vol_expr})*(1-{1-duck_ratio:.3f}*({gain}))"
    return vol_expr
