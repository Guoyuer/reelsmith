"""Filter graph builder — generates a filter_complex_script per EDL segment.

Each segment gets its own FFmpeg call with concat=v=1:a=1, ensuring perfect
audio-video sync. Photos get silence, keep_audio videos contribute speech.

Filter graph topology (photo):

    [idx:v] split -+-[bg] scale,crop,gblur,eq -[blurred]--+
                   |                                      |
                   +-[fg] scale -----------[sharp]--------+
                                                          |
                                  [blurred][sharp] overlay,ken_burns,color,fade -> [v{idx}]
    aevalsrc=0 (silence) ------------------------------------------> [a{idx}]

Filter graph topology (video, aspect fill):

    [idx:v] trim,split -+-[bg] scale,crop,gblur,eq -[blurred]--+
                        |                                      |
                        +-[fg] scale -----------[sharp]--------+
                                                               |
                                       [blurred][sharp] overlay,color,speed,fade -> [v{idx}]
    [idx:a] atrim,atempo (or aevalsrc=0) -----------------------------> [a{idx}]
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from pathlib import Path

from .. import constants as C
from ..edl import EDL, EditItem, Segment
from ._encoder import RenderContext
from ._filters import (
    color_grade,
    drawtext_filter,
    is_portrait,
    ken_burns_filter,
)

logger = logging.getLogger("reelsmith.assemble.graph")

# Effect enum value → ken_burns direction string
_EFFECT_DIRECTIONS = {
    "ken_burns_in": "in",
    "ken_burns_out": "out",
    "ken_burns_left": "left",
    "ken_burns_right": "right",
    "none": "static",
}


@dataclass
class SegmentGraph:
    """One segment's FFmpeg inputs + filter graph."""

    inputs: list[list[str]]
    script: str
    # item_idx → (input_idx, source_name, filter_line_start) for error mapping
    item_map: list[tuple[int, str, int]] = dataclasses.field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


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
    fps = ctx.fps
    inputs: list[list[str]] = []
    filters: list[str] = []
    v_a_pairs: list[tuple[str, str]] = []
    item_map: list[tuple[int, str, int]] = []
    # --- Title card (prepended to first segment) ---
    if title_card_path and title_card_path.exists():
        _add_static_card(
            inputs, filters, v_a_pairs, title_card_path, intro_duration, fps
        )

    # --- Content items ---
    for item_idx, item in enumerate(segment.items):
        fade_in, fade_out = fade_params[item_idx]
        source = Path(item.source_file)

        # HEIC photos work natively with the loop filter (no -loop 1 needed)

        idx = len(inputs)
        filter_line = len(filters)

        if item.media_type == "photo":
            frames = int(item.display_duration * fps)
            exact_dur = frames / fps
            # Decode photo once; loop filter duplicates the frame.
            # This is ~20x faster than -loop 1 which re-decodes per frame.
            inputs.append(["-i", str(source)])
            filters.append(
                _photo_filter(idx, item, segment, ctx, fade_in, fade_out, language)
            )
            filters.append(f"{_silence(exact_dur)} [a{idx}]")
        else:
            inputs.append(["-i", str(source)])
            filters.append(
                _video_filter(idx, item, segment, ctx, fade_in, fade_out, language)
            )

            # Audio: preserve speech or generate silence
            trim_start = item.start_time or 0.0
            trim_dur = (
                item.end_time - item.start_time
                if item.start_time is not None and item.end_time is not None
                else item.display_duration
            )
            speed = item.playback_speed
            if item.keep_audio:
                parts = [
                    f"[{idx}:a] atrim=start={trim_start}:duration={trim_dur}",
                    f"atempo={speed}" if speed != 1.0 else None,
                    f"asetpts=PTS-STARTPTS [a{idx}]",
                ]
                filters.append(",".join(p for p in parts if p))
            else:
                filters.append(f"{_silence(trim_dur / speed)} [a{idx}]")

        v_a_pairs.append((f"[v{idx}]", f"[a{idx}]"))
        item_map.append((idx, source.name, filter_line))

    # --- Outro card (appended to last segment) ---
    if outro_card_path and outro_card_path.exists():
        _add_static_card(
            inputs, filters, v_a_pairs, outro_card_path, outro_duration, fps
        )

    # --- Concat all v/a pairs ---
    n = len(v_a_pairs)
    concat_in = "".join(f"{v}{a}" for v, a in v_a_pairs)
    filters.append(f"{concat_in} concat=n={n}:v=1:a=1 [vout][aout]")

    return SegmentGraph(
        inputs=inputs,
        script=";\n".join(filters),
        item_map=item_map,
    )


def compute_fade_params(edl: EDL) -> list[list[tuple[float, float]]]:
    """Compute fade_in/fade_out per item, grouped by segment."""
    all_fades: list[list[tuple[float, float]]] = []

    for seg_idx, segment in enumerate(edl.segments):
        seg_fades: list[tuple[float, float]] = []

        for item_idx in range(len(segment.items)):
            fade_in = 0.0
            fade_out = 0.0

            if segment.mode != "montage":
                # Fade in: first item of a non-first segment
                if item_idx == 0 and seg_idx > 0:
                    fade_in = segment.segment_transition_duration

                # Fade out: determined by the NEXT item's context
                next_seg, next_item_idx = _next_item(edl, seg_idx, item_idx)
                if next_seg is not None and next_seg.mode != "montage":
                    if next_item_idx == 0:
                        # Crossing segment boundary → use next segment's transition
                        fade_out = next_seg.segment_transition_duration
                    elif next_seg.transition != "cut":
                        # Same segment, intra-segment transition
                        fade_out = next_seg.transition_duration

            seg_fades.append((fade_in, fade_out))

        all_fades.append(seg_fades)

    return all_fades


def _next_item(edl: EDL, seg_idx: int, item_idx: int) -> tuple[Segment | None, int]:
    """Return (segment, item_index) for the next item in the EDL.

    If the next item is in the same segment, returns (current_segment, item_idx+1).
    If crossing to the next segment, returns (next_segment, 0).
    If this is the last item overall, returns (None, 0).
    """
    seg = edl.segments[seg_idx]
    if item_idx + 1 < len(seg.items):
        return seg, item_idx + 1
    if seg_idx + 1 < len(edl.segments):
        return edl.segments[seg_idx + 1], 0
    return None, 0


# ---------------------------------------------------------------------------
# Per-item builders (called by build_segment_graph)
# ---------------------------------------------------------------------------


def _silence(duration: float) -> str:
    """Stereo silence source at 48kHz for the given duration."""
    return f"aevalsrc=0:d={duration}:s={C.SAMPLE_RATE}:c=stereo"


def _add_static_card(
    inputs: list[list[str]],
    filters: list[str],
    v_a_pairs: list[tuple[str, str]],
    path: Path,
    duration: float,
    fps: int,
) -> None:
    """Add a title/outro card: normalize video + generate silence."""
    idx = len(inputs)
    inputs.append(["-i", str(path)])
    filters.append(
        f"[{idx}:v] fps={fps},format=yuv420p,setsar=1,setpts=PTS-STARTPTS [v{idx}]"
    )
    filters.append(f"{_silence(duration)} [a{idx}]")
    v_a_pairs.append((f"[v{idx}]", f"[a{idx}]"))


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


def _overlay_vf(item: EditItem, language: str, out_h: int) -> str:
    """Return drawtext filter fragment for text_overlay, or empty string."""
    if not item.text_overlay:
        return ""
    return "," + drawtext_filter(
        item.text_overlay.text,
        item.text_overlay.position,
        item.text_overlay.font_size,
        item.display_duration,
        language,
        out_h=out_h,
    )


def _blurred_bg(idx: int, w: int, h: int, sigma: int) -> str:
    """Blurred-background composite: upscale+crop+blur bg, fit-scale fg, overlay.

    Shared by photo and video filters for non-matching aspect ratios.
    Returns the bg/fg/overlay filter lines (without the leading split or
    trailing post-composite filters).
    """
    bg_parts = [
        f"scale={w}:{h}:force_original_aspect_ratio=increase",
        f"crop={w}:{h}",
        f"boxblur={sigma}:3",
        "eq=brightness=-0.15:saturation=0.6",
    ]
    return (
        f"[bg{idx}] {','.join(bg_parts)} [blurred{idx}];"
        f"[fg{idx}] scale={w}:{h}:force_original_aspect_ratio=decrease [sharp{idx}];"
        f"[blurred{idx}][sharp{idx}] overlay=(W-w)/2:(H-h)/2"
    )


def _photo_filter(
    idx: int,
    item: EditItem,
    segment: Segment,
    ctx: RenderContext,
    fade_in: float,
    fade_out: float,
    language: str,
) -> str:
    """Photo filter: blurred-bg composite + Ken Burns."""
    w, h, fps = ctx.w, ctx.h, ctx.fps
    frames = int(item.display_duration * fps)
    exact_dur = frames / fps

    overlay_vf = _overlay_vf(item, language, h)
    fade = _fade_expr(exact_dur, fade_in, fade_out)
    color_vf = color_grade(segment.color_temp)
    direction = _EFFECT_DIRECTIONS.get(item.effect, "in")
    ken_burns_vf = ken_burns_filter(frames, w, h, fps, direction=direction)

    # loop= duplicates the single decoded frame; avoids -loop 1 re-decode.
    # setpts+fps normalize timestamps to match -loop 1 output exactly.
    loop_vf = f"loop=loop={frames - 1}:size=1:start=0,setpts=N/{fps}/TB,fps={fps},"
    return (
        f"[{idx}:v] {loop_vf}split [bg{idx}][fg{idx}];"
        f"{_blurred_bg(idx, w, h, C.BG_BLUR_SIGMA)},"
        f"{ken_burns_vf},{color_vf}{overlay_vf}{fade} [v{idx}]"
    )


def _video_filter(
    idx: int,
    item: EditItem,
    segment: Segment,
    ctx: RenderContext,
    fade_in: float,
    fade_out: float,
    language: str,
) -> str:
    """Video filter: trim + speed + aspect fill or direct scale."""
    w, h, fps = ctx.w, ctx.h, ctx.fps

    # Derive trim/speed from item
    trim_start = item.start_time or 0.0
    trim_dur = (
        item.end_time - item.start_time
        if item.start_time is not None and item.end_time is not None
        else item.display_duration
    )
    speed = item.playback_speed
    output_dur = trim_dur / speed

    speed_vf = f",setpts={1 / speed:.4f}*PTS" if speed != 1.0 else ""
    trim_vf = (
        f"trim=start={trim_start}:duration={trim_dur},setpts=PTS-STARTPTS,"
        if trim_dur > 0
        else ""
    )

    overlay_vf = _overlay_vf(item, language, h)
    src_w, src_h = ctx.probe_dimensions(Path(item.source_file))
    color_vf = color_grade(segment.color_temp)
    fade = _fade_expr(output_dur, fade_in, fade_out)

    # Blurred background composite for non-matching aspect ratios (portrait AND
    # non-16:9 landscape like 2.35:1). Exact 16:9 videos pass through without
    # visible blur since the foreground covers the entire frame.
    needs_aspect_fill = is_portrait(src_w, src_h) or (
        src_w > 0
        and src_h > 0
        and abs(src_w / src_h - w / h) > C.ASPECT_RATIO_TOLERANCE
    )
    if needs_aspect_fill:
        return (
            f"[{idx}:v] {trim_vf}format=yuv420p,split [bg{idx}][fg{idx}];"
            f"{_blurred_bg(idx, w, h, C.BG_BLUR_SIGMA)},"
            f"{color_vf}{speed_vf}{overlay_vf}{fade},fps={fps} [v{idx}]"
        )
    else:
        direct_parts = [
            f"scale={w}:{h}:force_original_aspect_ratio=decrease",
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2",
        ]
        return (
            f"[{idx}:v] {trim_vf}format=yuv420p,{','.join(direct_parts)},"
            f"{color_vf}{speed_vf}{overlay_vf}{fade},fps={fps} [v{idx}]"
        )
