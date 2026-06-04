"""Filter graph builder — generates a filter_complex_script per EDL segment.

Each segment gets its own FFmpeg call with concat=v=1:a=1, ensuring perfect
audio-video sync. Photos get silence, keep_audio videos contribute speech.

Filter graph topology (photo):

    [idx:v] loop,split -+-[bg] scale,crop,boxblur,eq -[blurred]--+
                        |                                        |
                        +-[fg] scale -----------[sharp]----------+
                                                                 |
                                  [blurred][sharp] overlay,ken_burns,color,fade -> [v{idx}]
    aevalsrc=0 (silence) ------------------------------------------> [a{idx}]

Filter graph topology (video, aspect fill):

    [idx:v] trim,split -+-[bg] scale,crop,boxblur,eq -[blurred]--+
                        |                                        |
                        +-[fg] scale -----------[sharp]----------+
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
from ..utils.image import decode_heic_for_filter
from ._encoder import RenderContext, RenderSettings
from ._filters import (
    drawtext_filter,
    hdr_to_sdr_filter,
    is_hdr_transfer,
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

_SDR_SET_PARAMS = (
    "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv"
)


def item_render_seconds(item: EditItem, fps: int) -> float:
    """Duration this item occupies in the final video — the renderer's own value.

    The single source of truth for per-item timeline length, mirroring exactly
    what the render filters produce:

    - photo: floored to whole frames — ``int(display_duration * fps) / fps``
      (see ``_photo_filter``'s ``frames``).
    - video: its trim window over playback speed —
      ``(end_time - start_time) / playback_speed`` (see ``_video_filter``'s
      ``output_dur``); falls back to ``display_duration / speed`` when untrimmed.

    Deliberately NOT ``item.display_duration``: ``beat_snap_edl`` rewrites that
    field, but the video renderer ignores it (it trims to start/end), so using
    display_duration to place items makes the cue sheet drift. ``build_segment_graph``
    exports these values so downstream consumers read rendered truth, never
    re-derive it.
    """
    if item.media_type == "photo":
        return int(item.display_duration * fps) / fps
    if item.start_time is not None and item.end_time is not None:
        trim_dur = item.end_time - item.start_time
    else:
        trim_dur = item.display_duration
    return trim_dur / item.playback_speed


def _trim_params(item: EditItem) -> tuple[float, float, float]:
    """Return (trim_start, trim_dur, speed) for an item's render/audio window."""
    trim_start = item.start_time or 0.0
    if item.start_time is not None and item.end_time is not None:
        trim_dur = item.end_time - item.start_time
    else:
        trim_dur = item.display_duration
    return trim_start, trim_dur, item.playback_speed


@dataclass
class SegmentGraph:
    """One segment's FFmpeg inputs + filter graph."""

    inputs: list[list[str]]
    script: str
    # item_idx → (input_idx, source_name, filter_line_start) for error mapping
    item_map: list[tuple[int, str, int]] = dataclasses.field(default_factory=list)
    # True if any item tone-maps via libplacebo — caller must add the Vulkan
    # device (-init_hw_device vulkan) to this segment's FFmpeg command.
    uses_vulkan: bool = False
    # Per-content-item rendered duration in the final video (seconds), in
    # segment.items order. Exported truth for the cue sheet — see
    # item_render_seconds. Excludes title cards (those are not content items).
    item_durations: list[float] = dataclasses.field(default_factory=list)


@dataclass(frozen=True)
class ResolvedRenderItem:
    """A segment item with all probe/decode decisions resolved up front."""

    item: EditItem
    source_path: Path
    input_path: Path
    render_duration: float
    display_width: int = 0
    display_height: int = 0
    color_transfer: str = ""
    temp_file: Path | None = None
    use_libplacebo_tonemap: bool = False

    @property
    def input_args(self) -> list[str]:
        return ["-i", str(self.input_path)]

    @property
    def uses_vulkan(self) -> bool:
        return (
            self.item.media_type != "photo"
            and self.use_libplacebo_tonemap
            and is_hdr_transfer(self.color_transfer)
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_render_items(
    segment: Segment, ctx: RenderContext
) -> list[ResolvedRenderItem]:
    """Resolve probe/decode work for a segment before graph construction."""
    resolved: list[ResolvedRenderItem] = []
    fps = ctx.settings.fps
    for item in segment.items:
        source = Path(item.source_file)
        render_duration = item_render_seconds(item, fps)

        if item.media_type == "photo":
            decoded, was_temp = decode_heic_for_filter(source)
            resolved.append(
                ResolvedRenderItem(
                    item=item,
                    source_path=source,
                    input_path=decoded,
                    render_duration=render_duration,
                    temp_file=decoded if was_temp else None,
                )
            )
            continue

        width, height = ctx.probe.probe_dimensions(source)
        color_transfer = ctx.probe.probe_color_transfer(source)
        resolved.append(
            ResolvedRenderItem(
                item=item,
                source_path=source,
                input_path=source,
                render_duration=render_duration,
                display_width=width,
                display_height=height,
                color_transfer=color_transfer,
                use_libplacebo_tonemap=ctx.capabilities.vulkan_tonemap,
            )
        )

    return resolved


def build_segment_graph(
    resolved_items: list[ResolvedRenderItem],
    settings: RenderSettings,
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
    if len(resolved_items) != len(fade_params):
        raise ValueError(
            f"resolved item count {len(resolved_items)} != fade count {len(fade_params)}"
        )
    fps = settings.fps
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
    item_durations: list[float] = []
    uses_vulkan = False
    for item_idx, resolved in enumerate(resolved_items):
        item = resolved.item
        fade_in, fade_out = fade_params[item_idx]
        idx = len(inputs)
        filter_line = len(filters)
        render_dur = resolved.render_duration
        item_durations.append(render_dur)

        uses_vulkan = uses_vulkan or resolved.uses_vulkan

        if item.media_type == "photo":
            inputs.append(resolved.input_args)
            filters.append(
                _photo_filter(idx, item, settings, fade_in, fade_out, language)
            )
            filters.append(f"{_silence(render_dur)} [a{idx}]")
        else:
            inputs.append(resolved.input_args)
            filters.append(
                _video_filter(idx, resolved, settings, fade_in, fade_out, language)
            )

            # Audio: preserve speech (atrim+atempo) or generate silence. Both
            # resolve to render_dur of final-video length.
            trim_start, trim_dur, speed = _trim_params(item)
            if item.keep_audio:
                parts = [
                    f"[{idx}:a] atrim=start={trim_start}:duration={trim_dur}",
                    f"atempo={speed}" if speed != 1.0 else None,
                    f"asetpts=PTS-STARTPTS [a{idx}]",
                ]
                filters.append(",".join(p for p in parts if p))
            else:
                filters.append(f"{_silence(render_dur)} [a{idx}]")

        v_a_pairs.append((f"[v{idx}]", f"[a{idx}]"))
        item_map.append((idx, resolved.source_path.name, filter_line))

    # --- Outro card (appended to last segment) ---
    if outro_card_path and outro_card_path.exists():
        _add_static_card(
            inputs, filters, v_a_pairs, outro_card_path, outro_duration, fps
        )

    # --- Concat all v/a pairs ---
    n = len(v_a_pairs)
    concat_in = "".join(f"{v}{a}" for v, a in v_a_pairs)
    filters.append(
        f"{concat_in} concat=n={n}:v=1:a=1 [vcat][aout];[vcat] {_SDR_SET_PARAMS} [vout]"
    )

    return SegmentGraph(
        inputs=inputs,
        script=";\n".join(filters),
        item_map=item_map,
        uses_vulkan=uses_vulkan,
        item_durations=item_durations,
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


def loop_photo(frames: int, fps: int) -> str:
    """Loop a single decoded image frame, with PTS normalization.

    The ``loop`` filter decodes once and duplicates the frame buffer (~20x
    faster than ``-loop 1`` which re-decodes per frame).  ``setpts`` and
    ``fps`` normalize timestamps to match ``-loop 1`` output exactly.
    """
    repeats = max(frames - 1, 0)
    return f"loop=loop={repeats}:size=1:start=0,setpts=N/{fps}/TB,fps={fps}"


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
    """Video normalization: format + SAR + PTS reset + fades + hard trim."""
    parts = ["format=yuv420p", "setsar=1", "setpts=PTS-STARTPTS"]
    if fade_in > 0:
        parts.append(f"fade=t=in:d={fade_in}")
    if fade_out > 0:
        st = max(0, duration - fade_out)
        parts.append(f"fade=t=out:st={st:.3f}:d={fade_out}")
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
    settings: RenderSettings,
    fade_in: float,
    fade_out: float,
    language: str,
) -> str:
    """Photo filter: blurred-bg composite + Ken Burns."""
    w, h, fps = settings.w, settings.h, settings.fps
    frames = int(item.display_duration * fps)
    exact_dur = frames / fps

    overlay_vf = _overlay_vf(item, language, h)
    fade = _fade_expr(exact_dur, fade_in, fade_out)
    direction = _EFFECT_DIRECTIONS.get(item.effect, "in")
    ken_burns_vf = ken_burns_filter(frames, w, h, fps, direction=direction)

    return (
        f"[{idx}:v] {loop_photo(frames, fps)},split [bg{idx}][fg{idx}];"
        f"{_blurred_bg(idx, w, h, C.BG_BLUR_SIGMA)},"
        f"{ken_burns_vf}{overlay_vf}{fade} [v{idx}]"
    )


def _video_filter(
    idx: int,
    resolved: ResolvedRenderItem,
    settings: RenderSettings,
    fade_in: float,
    fade_out: float,
    language: str,
) -> str:
    """Video filter: trim + speed + aspect fill or direct scale."""
    item = resolved.item
    w, h, fps = settings.w, settings.h, settings.fps

    # Derive trim/speed from item
    trim_start, trim_dur, speed = _trim_params(item)
    output_dur = trim_dur / speed

    speed_vf = f",setpts={1 / speed:.4f}*PTS" if speed != 1.0 else ""
    trim_vf = (
        f"trim=start={trim_start}:duration={trim_dur},setpts=PTS-STARTPTS,"
        if trim_dur > 0
        else ""
    )

    overlay_vf = _overlay_vf(item, language, h)
    src_w, src_h = resolved.display_width, resolved.display_height
    fade = _fade_expr(output_dur, fade_in, fade_out)
    post_vf = f"{speed_vf}{overlay_vf}{fade},fps={fps}".lstrip(",")

    # HDR→SDR tone-map for BT.2020 PQ/HLG sources (phones, DJI drones). Runs
    # first on the 10-bit HDR frames; empty for SDR clips. Without it the output
    # is too bright and oversaturated. libplacebo (color-correct) when a Vulkan
    # device is available, else zscale (CPU). Trailing comma only when present.
    tonemap = hdr_to_sdr_filter(
        resolved.color_transfer,
        use_libplacebo=resolved.use_libplacebo_tonemap,
    )
    tonemap_vf = f"{tonemap}," if tonemap else ""

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
            f"[{idx}:v] {trim_vf}{tonemap_vf}format=yuv420p,split [bg{idx}][fg{idx}];"
            f"{_blurred_bg(idx, w, h, C.BG_BLUR_SIGMA)},"
            f"{post_vf} [v{idx}]"
        )
    else:
        direct_parts = [
            f"scale={w}:{h}:force_original_aspect_ratio=decrease",
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2",
        ]
        return (
            f"[{idx}:v] {trim_vf}{tonemap_vf}format=yuv420p,{','.join(direct_parts)},"
            f"{post_vf} [v{idx}]"
        )
