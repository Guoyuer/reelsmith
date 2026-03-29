# FFmpeg Upgrade TODO

## Context

The rendering pipeline currently targets FFmpeg 4.0+ (implicit, no version check).
Modern FFmpeg (7.x / 8.x) introduces features that can meaningfully improve output
quality, encoding efficiency, and hardware coverage.

This document evaluates which features are worth adopting, guided by one architectural
principle:

> **Gemini owns perception and creative decisions. FFmpeg owns execution and rendering.**
>
> Features that make FFmpeg better at executing Gemini's decisions are safe investments.
> Features that duplicate or compete with Gemini's multimodal understanding are not --
> they introduce information loss and will become liabilities as the model improves.

## Design principle: don't compensate for model weakness

The pipeline is end-to-end by design: Gemini sees photos (400px thumbnails) and watches
videos (480p 1fps mega-preview with audio) in a single multimodal pass. It makes all
creative decisions -- item selection, trim points, keep_audio, pacing, narrative arc --
with full visual + audio context.

Adding preprocessing that extracts scalar features from what Gemini can directly perceive
is tempting but architecturally wrong:

| Rejected feature | What it does | Why it's worse than letting Gemini do it |
|------------------|-------------|------------------------------------------|
| Whisper speech detection (FFmpeg 8.0) | Extract speech timestamps + transcript in prepare stage | Gemini hears audio *in context of what it sees*. "Someone saying 'look at that building'" + seeing the building = keep. Whisper gives timestamps but loses the visual correlation. |
| `blurdetect` quality scoring (5.1) | Score each photo's sharpness, pass to Gemini as metadata | A scalar blur score can't distinguish "badly out of focus" from "beautiful shallow DoF bokeh". Gemini sees the actual image and understands intent. |
| `colordetect` dominant color (8.0) | Detect dominant colors, suggest color_temp | Gemini directly sees the photos and decides warm/cool. A hex color code is a lossy compression of what Gemini already perceives. |
| `grayworld` white balance in prepare (5.0) | Auto-correct color cast before Gemini sees thumbnails | May "fix" intentional warm golden-hour tones. Gemini should see the original and decide. |

**Rule of thumb:** if a feature creates a derived representation (score, timestamp, label)
of something Gemini can perceive directly (image, audio), don't add it. The model will
only get stronger at multimodal understanding; the derived representation won't.

**Exception:** features applied in the *assemble* stage to improve what the *viewer*
sees/hears (not what Gemini sees/hears) are fine. `dialogue enhance` on final output
improves viewer experience regardless of model capability.

## TODO: rendering and encoding improvements

### P0: AV1 hardware encoding

**FFmpeg version:** 6.0+ (NVENC AV1), 8.0+ (Vulkan AV1)

**What:** Add `av1_nvenc` and `av1_vulkan` to the encoder detection chain in `_encoder.py`.
AV1 offers ~30% bitrate savings over HEVC at equivalent quality. YouTube and Bilibili
natively support AV1, avoiding re-encoding on upload.

**Scope:**
- `_encoder.py`: add AV1 probe to `_detect_gpu_encoder()`, before HEVC fallback
- `_encoder.py`: add AV1 tier to `BITRATE_TIERS` (HEVC ratio * 0.7 ?)
- `_assemble.py`: TS intermediate format -- verify AV1-in-TS works or switch to fMP4
- Consider a `--codec auto|hevc|av1|h264` CLI flag

**Risk:** AV1 NVENC requires RTX 40-series+. Must fall back gracefully. TS container
may not support AV1 -- may need fragmented MP4 intermediates.

### P1: xfade cross-dissolve transitions

**FFmpeg version:** 4.3+ (`xfade` filter), 6.1+ (`xfade_vulkan` GPU variant)

**What:** Currently transitions are `fade=t=out` + `fade=t=in` (fade through black).
The `xfade` filter supports true cross-dissolves and ~40 transition types (dissolve,
wipeleft, circleopen, radial, smoothleft, etc.).

The EDL already has `transition` (Transition StrEnum) and `transition_duration` fields --
they're just not mapped to actual xfade effects in the renderer.

**Scope:**
- `_graph.py`: replace per-item fade-in/fade-out with `xfade` between consecutive items
  within a segment. This requires rethinking the concat approach -- items can no longer be
  independently rendered then concatenated; xfade needs overlapping frames from adjacent items.
- `edl.py`: verify Transition enum values map to xfade transition names
- Prompt: update if needed to reflect which transitions are actually available

**Risk:** Significant `_graph.py` refactor. The current architecture renders each item
independently then concats. xfade requires the output of item N and input of item N+1
to overlap in time. May need to shift from concat-filter to chained xfade filters.

### P1: drawvg vector graphics overlays

**FFmpeg version:** 8.1+

**What:** Replace `drawtext` + `drawbox` title cards with `drawvg` (Cairo-based vector
graphics). Supports gradient fills, rounded rectangles, path animations, styled text,
and dynamic expressions. A generational upgrade over drawtext for title cards.

**Scope:**
- `_render.py`: rewrite `render_title_card()` using drawvg scripts
- `_filters.py`: optionally upgrade `drawtext_filter()` for in-clip text overlays
- Need to design VGS (Vector Graphics Script) templates for title/subtitle styles

**Risk:** Requires FFmpeg built with `--enable-libcairo`. Not available in all FFmpeg
distributions. Must fall back to drawtext if drawvg is unavailable.

### P2: broader GPU hardware coverage

**FFmpeg version:** 7.0+ (D3D12VA decode), 7.1+ (Vulkan encode)

**What:** Currently only NVIDIA GPUs get hardware acceleration (CUDA decode + NVENC
encode on Windows/Linux, VideoToolbox on macOS). AMD and Intel GPUs are stuck on CPU.

- `D3D12VA` (7.0): hardware decoding on any modern Windows GPU (AMD, Intel, NVIDIA)
- Vulkan H.264/HEVC/AV1 encode (7.1/8.0): GPU encoding on AMD/Intel via Vulkan API

**Scope:**
- `_encoder.py`: add D3D12VA to hwaccel detection (Windows)
- `_encoder.py`: add Vulkan encoder probing after NVENC, before libx264
- Test on AMD and Intel GPUs

**Risk:** Vulkan encode quality/speed varies across vendors. Need benchmarking.

### P2: assemble-stage audio enhancement

**FFmpeg version:** 5.1+ (`dialoguenhance`), 6.1+ (`nlmeans_vulkan`)

**What:** Improve final output quality for the *viewer* (not for Gemini):
- `dialoguenhance`: boost speech clarity in keep_audio clips (travel videos often have
  noisy backgrounds -- wind, crowds, traffic)
- `nlmeans_vulkan`: GPU denoising for low-light/high-ISO footage in final output

**Scope:**
- `_graph.py`: add optional `dialoguenhance` to audio chain for keep_audio=true items
- `_graph.py`: add optional `nlmeans_vulkan` to video chain (gated by ISO metadata
  from analysis.json, or unconditional with light settings)
- Both should be toggleable via CLI flags or config

**Risk:** `dialoguenhance` may produce artifacts on non-speech audio. `nlmeans` is
slow even on GPU for 4K. Both need quality/performance testing.

### P3: FFmpeg 7.0 pipeline threading (free performance)

**FFmpeg version:** 7.0+

**What:** FFmpeg 7.0 automatically runs demux/decode/filter/encode/mux in separate
threads. Complex filter graphs (Ken Burns + blur bg + color grade + drawtext) benefit
significantly. No code changes needed -- just upgrading FFmpeg.

**Action:** Set minimum FFmpeg version to 7.0 in documentation and add a runtime version
check in CLI startup. Warn (don't block) if FFmpeg < 7.0.

### P3: libplacebo GPU color pipeline

**FFmpeg version:** 5.0+

**What:** Replace CPU-based `eq` + `colorbalance` + `unsharp` chain with `libplacebo`
for GPU-accelerated color grading. Also enables future HDR pipeline (tone mapping,
gamut mapping, 10-bit output).

**Scope:**
- `_filters.py`: add libplacebo variant of `color_grade_filter()` and `ken_burns_filter()`
- Requires Vulkan-capable GPU and FFmpeg built with `--enable-libplacebo`
- Must fall back to current CPU filters if unavailable

**Risk:** Large dependency (libplacebo + Vulkan). Not a priority until HDR input
sources become common enough to justify 10-bit pipeline support.

## Not planned

| Feature | Reason |
|---------|--------|
| Whisper in prepare | Competes with Gemini's multimodal audio understanding; information loss |
| blurdetect / colordetect metadata | Lossy scalar of what Gemini perceives directly |
| VVC encoding | Ecosystem too immature (browser/player support) |
| HDR10 passthrough | Requires full 10-bit pipeline rewrite; revisit when HDR sources are common |
| Filtergraph chaining (7.1) | Nice syntax sugar but doesn't change capability |
