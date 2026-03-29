# GPU & Parallel Acceleration Experiments

## Goal
Accelerate rendering while maintaining output quality (VMAF >= 97).

## Experiment results

### Micro-benchmarks (120 frames, 720p, single item)

| Test | CPU | Vulkan | CUDA | Winner |
|------|-----|--------|------|--------|
| boxblur 720p (synthetic) | 0.69s | **0.41s** | - | Vulkan 1.7x |
| Photo blur bg composite (real) | 1.59s | **0.49s** | - | Vulkan 3.2x |
| Video blur bg composite (real) | 2.22s | 2.19s | - | Tie (decode-bound) |
| Scale 720p (video) | **1.57s** | 2.01s | 3.62s | CPU (hwupload overhead) |
| Full photo pipeline (blur+KenBurns+color) | 1.59s | 1.49s | - | ~Tie (KenBurns dominates) |

### End-to-end experiments

| Exp | Change | Phase 1 | Total | vs baseline | Verdict |
|-----|--------|---------|-------|-------------|---------|
| Baseline | monolithic filter_complex, 3 NVENC workers | ~190s | ~200s | - | - |
| Per-item parallel | 50 separate FFmpeg calls, 3 NVENC workers | 239s | 255s | **28% SLOWER** | REVERTED |

### Analysis

#### Why Vulkan filters don't help end-to-end
1. **Photo items**: Vulkan gblur is 3.2x faster in isolation, but the full pipeline
   speedup is only ~6% because Ken Burns (per-frame CPU crop expression + lanczos)
   dominates at 88% of render time. This filter CANNOT move to GPU.
2. **Video items**: hwupload/hwdownload per frame cancels out any GPU filter speedup.
   Videos are decode-bound, not filter-bound.
3. **Scale operations**: CPU is faster than GPU due to hwupload/hwdownload overhead.

#### Why per-item parallel rendering is slower
1. With 3 NVENC sessions max, the monolithic approach processes 3 segments (each with
   ~8 items) in parallel = 2 batches of 3. The per-item approach processes 50 items
   in batches of 3 = 17 batches. **More batches = more GPU scheduling overhead.**
2. Each FFmpeg invocation has ~100ms startup cost. 50 invocations = 5s overhead.
3. Per-item concat adds 6 concat demuxer calls (~1s each).
4. The title card (rendered with -an) required adding a silent audio stream for
   concat compatibility, adding another FFmpeg call per title card.

#### What IS the actual bottleneck?
The render pipeline is **CPU-bound on per-frame filter expression evaluation**:
- Ken Burns: cosine expression + lanczos resample per frame
- Color grade: eq + colorbalance per frame
- Fades: expression evaluation per frame

These cannot be offloaded to GPU because FFmpeg's expression evaluator is CPU-only.
The GPU encoder (NVENC) is underutilized — it waits for CPU filter output.

## Conclusion
GPU acceleration via Vulkan/CUDA filters does not provide meaningful end-to-end
speedup for this pipeline. The bottleneck is FFmpeg's CPU expression evaluator,
which has no GPU alternative. The existing optimizations (loop filter, boxblur,
skip pre-validation) already capture the low-hanging fruit.

Further acceleration would require:
1. **Pre-computing Ken Burns keyframes** outside FFmpeg (generate crop coordinates,
   apply via a lookup table instead of per-frame expression evaluation)
2. **Custom GPU shader** for the Ken Burns effect (outside FFmpeg's filter system)
3. **Splitting the pipeline** into decode→filter→encode stages with async queues

All of these are architectural changes beyond the scope of FFmpeg filter tuning.
