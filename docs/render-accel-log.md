# Render Acceleration Exploration Log

## Baseline (current code, 720p30 HEVC, edl_v3, singapore-final1)
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

## Final result: A+B+C+D
- **221s → 175s (21% faster)**
- VMAF 98.2 (transparent quality, threshold ≥ 93)
- File size: 114.3MB (unchanged)

## Key findings
- CPU filter graph processing is the bottleneck, not NVENC encoding
- NVENC preset has negligible impact (p1 vs p4: same segment times)
- 4 workers is the sweet spot; 6 causes CPU contention
- boxblur is visually identical to gblur on darkened backgrounds
- unsharp is imperceptible on compressed video output
- Pre-validation is pure overhead when filter graphs are well-tested
