"""Filter graph builder — generates a filter_complex_script per EDL segment.

Each segment gets its own FFmpeg call with concat=v=1:a=1, ensuring perfect
audio-video sync. Photos get silence, keep_audio videos contribute speech.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from ..edl import EDL, EditItem, Segment
from ._encoder import RenderContext
from ._filters import (
    color_grade,
    drawtext_filter,
    is_portrait,
    zoompan_filter,
)

logger = logging.getLogger("vlog.assemble.graph")


@dataclass
class SegmentGraph:
    """One segment's FFmpeg inputs + filter graph."""

    inputs: list[list[str]]
    script: str
    has_speech: bool


def build_segment_graph(
    segment: Segment,
    ctx: RenderContext,
    *,
    fade_params: list[tuple[float, float]],
    language: str = "en",
    title_card_path: Path | None = None,
    outro_card_path: Path | None = None,
    intro_duration: float = 0.0,
    outro_duration: float = 0.0,
) -> SegmentGraph:
    """Build filter graph for one EDL segment.

    Returns inputs + script for a single FFmpeg call.
    Video and audio are concat'd together (v=1:a=1) for perfect sync.
    """
    w, h, fps = ctx.w, ctx.h, ctx.fps
    inputs: list[list[str]] = []
    filters: list[str] = []
    segment_pairs: list[tuple[str, str]] = []
    has_speech = False

    # Title card (prepended to first segment)
    if title_card_path and title_card_path.exists():
        idx = len(inputs)
        inputs.append(["-i", str(title_card_path)])
        filters.append(f"[{idx}:v] setpts=PTS-STARTPTS [v{idx}]")
        filters.append(f"aevalsrc=0:d={intro_duration}:s=48000:c=stereo [a{idx}]")
        segment_pairs.append((f"[v{idx}]", f"[a{idx}]"))

    for item_idx, item in enumerate(segment.items):
        fade_in, fade_out = fade_params[item_idx]
        source = Path(item.source_file)

        # HEIC conversion for photos (FFmpeg can't -loop 1 with HEIC)
        if item.media_type == "photo" and source.suffix.lower() in {".heic", ".heif"}:
            from ..image_utils import convert_heic
            source = convert_heic(source)

        idx = len(inputs)

        if item.media_type == "photo":
            frames = int(item.display_duration * fps)
            exact_dur = frames / fps
            inputs.append(["-loop", "1", "-t", str(exact_dur), "-i", str(source)])
            vf = _photo_filter(idx, item, segment, ctx, fade_in, fade_out, language)
            filters.append(vf)
            filters.append(f"aevalsrc=0:d={exact_dur}:s=48000:c=stereo [a{idx}]")
        else:
            duration = item.display_duration
            if item.start_time is not None and item.end_time is not None:
                duration = item.end_time - item.start_time
            # No -ss/-t on input — use trim/atrim in filter chain instead
            # (FFmpeg 8 filter_complex ignores input-level -t)
            inputs.append(["-i", str(source)])

            speed = item.playback_speed or 1.0
            output_dur = duration / speed
            trim_start = item.start_time or 0.0
            vf = _video_filter(
                idx, item, segment, ctx, fade_in, fade_out,
                output_dur, language, trim_start=trim_start, trim_duration=duration,
            )
            filters.append(vf)

            if item.keep_audio:
                af = f"[{idx}:a] "
                af += f"atrim=start={trim_start}:duration={duration},"
                if speed != 1.0:
                    af += f"atempo={speed},"
                af += f"asetpts=PTS-STARTPTS [a{idx}]"
                filters.append(af)
                has_speech = True
            else:
                filters.append(f"aevalsrc=0:d={output_dur:.3f}:s=48000:c=stereo [a{idx}]")

        segment_pairs.append((f"[v{idx}]", f"[a{idx}]"))

    # Outro card (appended to last segment)
    if outro_card_path and outro_card_path.exists():
        idx = len(inputs)
        inputs.append(["-i", str(outro_card_path)])
        filters.append(f"[{idx}:v] setpts=PTS-STARTPTS [v{idx}]")
        filters.append(f"aevalsrc=0:d={outro_duration}:s=48000:c=stereo [a{idx}]")
        segment_pairs.append((f"[v{idx}]", f"[a{idx}]"))

    # Concat all pairs
    n = len(segment_pairs)
    concat_in = "".join(f"{v}{a}" for v, a in segment_pairs)
    filters.append(f"{concat_in} concat=n={n}:v=1:a=1 [vout][aout]")

    return SegmentGraph(
        inputs=inputs,
        script=";\n".join(filters),
        has_speech=has_speech,
    )


def compute_fade_params(edl: EDL) -> list[list[tuple[float, float]]]:
    """Compute fade_in/fade_out per item, grouped by segment."""
    all_fades: list[list[tuple[float, float]]] = []

    flat = []
    for seg_idx, segment in enumerate(edl.segments):
        for item_idx, item in enumerate(segment.items):
            flat.append((seg_idx, item_idx, item, segment))

    seg_fades: list[tuple[float, float]] = []
    current_seg = 0

    for fi, (seg_idx, item_idx, item, segment) in enumerate(flat):
        if seg_idx != current_seg:
            all_fades.append(seg_fades)
            seg_fades = []
            current_seg = seg_idx

        fade_in = 0.0
        fade_out = 0.0
        is_montage = segment.mode == "montage"

        if not is_montage:
            if item_idx == 0 and seg_idx > 0:
                fade_in = segment.segment_transition_duration
            if fi + 1 < len(flat):
                _, next_item_idx, _, next_segment = flat[fi + 1]
                if next_segment.mode != "montage":
                    if next_item_idx == 0:
                        fade_out = next_segment.segment_transition_duration
                    else:
                        fade_out = next_segment.transition_duration if next_segment.transition != "cut" else 0.0

        seg_fades.append((fade_in, fade_out))

    all_fades.append(seg_fades)
    return all_fades


# ---------------------------------------------------------------------------
# Per-clip filter chains
# ---------------------------------------------------------------------------


def _fade_expr(duration: float, fade_in: float, fade_out: float) -> str:
    """Video normalization: format + SAR + PTS reset + fades + hard trim.

    The trim at the end is CRITICAL — FFmpeg 8 filter_complex ignores
    input-level -t, so without this, -loop 1 photos produce infinite frames.
    """
    parts = ["format=yuv420p", "setsar=1", "setpts=PTS-STARTPTS"]
    if fade_in > 0:
        parts.append(f"fade=t=in:d={fade_in}")
    if fade_out > 0:
        st = max(0, duration - fade_out)
        parts.append(f"fade=t=out:st={st:.3f}:d={fade_out}")
    # Hard trim ensures finite output (prevents -loop 1 infinite frames)
    parts.append(f"trim=duration={duration:.6f}")
    parts.append("setpts=PTS-STARTPTS")
    return "," + ",".join(parts)


def _photo_filter(
    idx: int, item: EditItem, segment: Segment, ctx: RenderContext,
    fade_in: float, fade_out: float, language: str,
) -> str:
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

    exact_dur = frames / fps
    fade = _fade_expr(exact_dur, fade_in, fade_out)
    cg = color_grade(segment.color_temp)
    sharpen = ",unsharp=3:3:0.5:3:3:0.0"

    src_w, src_h = ctx.probe_dimensions(Path(item.source_file))
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
            f"{dt}{fade} [v{idx}]"
        )
    else:
        direction_map = {
            "ken_burns_in": "in", "ken_burns_out": "out",
            "ken_burns_left": "left", "ken_burns_right": "right", "static": "static",
        }
        direction = direction_map.get(item.effect, "in")
        ow, oh = w * 2, h * 2
        # zoompan at 2x resolution → lanczos downscale (zoompan's bilinear is too soft)
        zp = zoompan_filter(zoom_rate, frames, ow, oh, fps, direction=direction)
        lanczos = f"scale={w}:{h}:flags=lanczos"
        src_ratio = src_w / src_h if src_h > 0 else 1.0
        out_ratio = ow / oh

        if abs(src_ratio - out_ratio) / out_ratio < 0.05:
            return f"[{idx}:v] scale={ow}:{oh},{zp},{lanczos},{cg}{sharpen}{dt}{fade} [v{idx}]"
        else:
            return (
                f"[{idx}:v] split [bg{idx}][fg{idx}];"
                f"[bg{idx}] scale={ow}:{oh}:force_original_aspect_ratio=increase,"
                f"crop={ow}:{oh},gblur=sigma=25 [blurred{idx}];"
                f"[fg{idx}] scale={ow}:{oh}:force_original_aspect_ratio=decrease [sharp{idx}];"
                f"[blurred{idx}][sharp{idx}] overlay=(W-w)/2:(H-h)/2 [comp{idx}];"
                f"[comp{idx}] {zp},{lanczos},{cg}{sharpen}{dt}{fade} [v{idx}]"
            )


def _video_filter(
    idx: int, item: EditItem, segment: Segment, ctx: RenderContext,
    fade_in: float, fade_out: float, output_dur: float, language: str,
    trim_start: float = 0.0, trim_duration: float = 0.0,
) -> str:
    w, h, fps = ctx.w, ctx.h, ctx.fps
    speed = item.playback_speed or 1.0
    speed_vf = f",setpts={1/speed:.4f}*PTS" if speed != 1.0 else ""
    # Trim in filter chain (not -ss/-t on input — FFmpeg 8 filter_complex ignores those)
    trim_vf = f"trim=start={trim_start}:duration={trim_duration},setpts=PTS-STARTPTS," if trim_duration > 0 else ""

    dt = ""
    if item.text_overlay:
        dt = "," + drawtext_filter(
            item.text_overlay.text, item.text_overlay.position,
            item.text_overlay.font_size, item.display_duration, language, out_h=h,
        )

    src_w, src_h = ctx.probe_dimensions(Path(item.source_file))
    cg = color_grade(segment.color_temp)
    fade = _fade_expr(output_dur, fade_in, fade_out)

    # trim_vf trims the source before processing (replaces -ss/-t on input)
    if is_portrait(src_w, src_h):
        return (
            f"[{idx}:v] {trim_vf}format=yuv420p,split [bg{idx}][fg{idx}];"
            f"[bg{idx}] scale={w}:-1:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},gblur=sigma=60,eq=brightness=-0.15:saturation=0.6 [blurred{idx}];"
            f"[fg{idx}] scale=-1:{h} [sharp{idx}];"
            f"[blurred{idx}][sharp{idx}] overlay=(W-w)/2:(H-h)/2,"
            f"{cg}{speed_vf}{dt}{fade},fps={fps} [v{idx}]"
        )
    else:
        return (
            f"[{idx}:v] {trim_vf}format=yuv420p,scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
            f"{cg}{speed_vf}{dt}{fade},fps={fps} [v{idx}]"
        )
