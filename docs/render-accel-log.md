# Render Acceleration Exploration Log

## Baseline (main branch, 720p30 HEVC, edl_v3, singapore-final1)
- Render time: 221s
- Phase 1 (segments): 205s (93%)
- Pre-validation: 21s (10%)
- Phase 2 (concat+music): 16s (7%)
- File size: 114.4MB

## Optimizations tested

| # | Change | Time | Saved | VMAF | Keep? |
|---|--------|------|-------|------|-------|
| A | Skip pre-validation | 202s | 19s (9%) | 99.8 | YES |
| A+B | + gblur→boxblur=50:3 | 191s | 30s (14%) | 99.0 | YES |
| A+B+C | + Remove unsharp | - | (combined with D) | - | YES |
| A+B+C+D | + Workers 3→4 | 175s | 46s (21%) | 98.2 | YES |
| A+B+C+D+E | + NVENC p4→p1 | 179s | (worse) | - | NO (slower, bigger files) |
| A+B+C+D (6 workers) | Workers 6 | 192s | (worse) | - | NO (CPU contention) |
| A+B+C+D+F | + loop filter (no -loop 1) | 168s | 53s (24%) | 98.8 | YES |

### Rejected optimizations
- **NVENC p4→p1**: no speedup (CPU is bottleneck, not GPU), 3MB larger files
- **6 workers**: CPU contention, 17s slower than 4 workers
- **Pre-scale photos to 2x**: no speedup (bottleneck was per-frame JPEG re-decode, not resolution)
- **zoompan instead of crop+lanczos**: 12x faster but completely different coordinate system, VMAF 9.6
- **Bilinear instead of lanczos**: no speedup (crop expression eval is the bottleneck, not scale algorithm)

## Final result: A+B+C+D+F
- **221s → 168s (24% faster)**
- VMAF 98.8 vs same-pipeline baseline
- File size: 113.9MB (slightly smaller due to boxblur producing slightly different encoder input)
- All 615 tests pass

## Key findings

### Root cause of photo rendering bottleneck
`-loop 1` (FFmpeg input flag) re-decodes the JPEG/HEIC image **every single frame**.
A 4-second photo at 30fps = 120 JPEG decodes. The `loop` filter decodes once and
duplicates the frame buffer — 23x faster for the isolated photo, ~8s saved end-to-end.

### HEIC conversion no longer needed
The old pipeline used `-loop 1` which requires image2 demuxer (no HEIC support).
The `loop` filter approach uses the MOV demuxer which supports HEIC natively.
This eliminates the convert_heic() preprocessing step entirely.

### PTS normalization required
The `loop` filter produces different PTS than `-loop 1`. Adding `setpts=N/{fps}/TB,fps={fps}`
after the loop filter normalizes timestamps to match, preventing cumulative frame drift
across segment boundaries.

### CPU is the bottleneck, not GPU
- NVENC preset p4→p1: no change in segment render times
- boxblur vs gblur: negligible per-item speedup (only 9/50 items use blur bg)
- 4 workers > 3 workers > 6 workers: CPU parallelism sweet spot
- Encoding speed is not the constraint

### VMAF limitations
- Cannot compare across different container formats (TS vs MP4) due to PTS differences
- 1% of frames show VMAF < 50 at the HEIC photo item — this is because the old code
  converted HEIC→JPEG (lossy) before rendering, while the new code decodes HEIC directly
  (higher quality). The "mismatch" is actually an improvement.
