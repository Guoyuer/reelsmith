# Pipeline TODO

## Completed

- [x] **Merge preprocess + analyze → prepare** — single prepare stage (5 assets instead of 6)
- [x] **A8. Split monolithic assemble.py** — encoder.py, filters.py, render.py, concat.py, audio.py
- [x] **A5. Integration tests** — 80+ tests covering BPM, beat sync, Timeline, xfade, speech, music, e2e render
- [x] **A2. Remove global state** — RenderContext dataclass replaces 4 scattered globals
- [x] **A6. Consistent error handling** — ClipStatus/RenderReport with per-clip status tracking
- [x] **A4. Config pass-through** — render settings (quality) stored in EDL, read by assemble
- [x] **A1. FFmpeg filter abstraction** — FilterGraph builder with label validation
- [x] **A3. Shared parallel utility** — parallel.run_parallel() replaces duplicate ThreadPoolExecutor patterns
- [x] **Issue #2. Gemini fault tolerance** — auto-retry, fuzzy path matching, duration check
- [x] **Issue #5. Post-assemble validation** — 6 automated checks (file, duration, streams, codec, sync, resolution)
- [x] **Issue #7. Externalize prompts** — pipeline/prompts/ with .md/.json files

## Remaining

### Issue #3: Parallelize music generation and clip rendering

**Problem**: Pipeline is `plan → generate_music → assemble`. Music generation takes ~2min (6 Lyria calls).
Clip rendering (Phase 1 of assemble) also takes ~2min. They're independent but run serially.

**Solution**: Change Dagster DAG so `generate_music` and `assemble` both depend on `plan`, not on each other.
Assemble loads music file at Phase 3 (mix), not at start. If music isn't ready, Phase 1+2
run first (clip render + concat), then Phase 3 waits for music file.

**Files to change**:
- `pipeline/definitions.py`: Change assemble dependency, configure parallel execution
- `pipeline/assemble.py`: Make Phase 3 tolerant of missing music (wait or skip)

**Risk**: Low — music and assemble share no state except the EDL file.

### A7: Dagster usage decision

**Status**: Decision made — keep Dagster as-is. The UI value (run history, metadata, logs) justifies
the thin wrapper. Don't invest in partitions/sensors (overkill for single-user), don't rip it out.

The one future improvement: switch to `dg.multiprocess_executor` for Issue #3 parallelism.
