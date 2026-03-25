# Pipeline TODO

## Completed

- **Merge preprocess + analyze → prepare** — single `prepare` stage (5 stages instead of 6)
- **A8. Split monolithic assemble.py** — encoder.py, filters.py, render.py, concat.py, audio.py
- **A5. Integration tests** — 255 tests covering BPM, beat sync, Timeline, xfade, speech, music, e2e render
- **A2. Remove global state** — RenderContext dataclass replaces 4 scattered globals
- **A6. Consistent error handling** — ClipStatus/RenderReport with per-clip status tracking
- **A4. Config pass-through** — render settings (quality) stored in EDL, read by assemble
- ~~**A1. FFmpeg filter abstraction**~~ — Added then removed. FilterGraph wrapped raw strings in objects without catching real bugs. Deleted.
- **A3. Shared parallel utility** — parallel.run_parallel() replaces duplicate ThreadPoolExecutor patterns
- **Issue #2. Gemini fault tolerance** — fuzzy path matching, trim point clamping, deduplication, duration check
- **Issue #5. Post-assemble validation** — 6 automated checks (file, duration, streams, codec, sync, resolution)
- **Issue #7. Externalize prompts** — pipeline/prompts/ with .md/.json files
- **Remove tier concept** — replaced with family_count, "unknown" label for missing data
- **FFmpeg command logging** — all commands logged at INFO + output/ffmpeg_commands.log
- **Comprehensive Gemini logging** — full I/O logged with [Gemini] prefix
- **B1. concat.py 4K bitrate** — `get_encoder(w, h, fps)` now receives actual resolution; demuxer uses `-c:v copy`
- **B2. Video trim point validation** — Layer 2b in plan.py clamps start_time/end_time against video_duration
- **B3. `selected_items` in prepare** — analysis exported to analysis.json, item count logged
- **B4. Hard-coded SGT timezone** — defaults to system timezone, configurable via `--tz` CLI flag
- **B5. `%-d` Windows crash** — replaced with `{first.day}` property access (cross-platform)
- **V5. Gemini-controlled inter-segment transitions** — `segment_transition` field reads from EDL, no longer hard-coded
- **V7. Portrait background darkening** — `eq=brightness=-0.15` after `gblur` in portrait filter
- **E1. Unified clip timing** — Timeline is the single source of truth for concat, speech, and chapters
- **E4. Render failure capture** — RenderReport structures all errors from parallel workers
- **E5. Gemini payload optimization** — single mega-preview via Files API, individual photo thumbnails inline
- **F3. Folder-based media source** — `fetch_local.py` with `--source` flag, EXIF/date/GPS extraction
- **V1. Eased Ken Burns** — cosine easing in `filters.py:zoompan_filter` (all 5 directions + portrait)
- **V2. Smooth ducking ramps** — 300ms attack + 1000ms release via `clip()` expressions in `audio.py:add_music`
- **V3. Drop shadow text** — `shadowcolor` replaces `borderw` in `filters.py:drawtext_filter`
- **V6. fadewhite transition** — added to `edl.py` Literal + XFADE_MAP + prompt
- **V8. Shorter default durations** — photos 2.5-4s, videos 4-8s in `visual_planner_system.md`
- **V9. Hero-photo title card** — first EDL photo as blurred background in `render.py:render_title_card`
- **V10. Montage beat sync** — `max_shift=0.2` for montage segments in `audio.py:beat_snap_edl`
- **E2. Speed-aware duration** — `estimated_duration()` divides by `playback_speed`
- **E3. Remove dead intro/outro styles** — `highlight_montage`/`last_hero` removed from Literal types + refs

- **Split cli.py into package** — `pipeline/cli/` with `__init__.py`, `_commands.py`, `_display.py`, `_runner.py`, `_workspace.py`

---

## Pending

### Open source essentials

- Add LICENSE (MIT)
- Add GitHub Actions CI (ruff + pytest)
- Add CHANGELOG.md
- Complete README (install guide, badges, screenshots)

---

## Feature Ideas (Aspirational)

### F1. Ambient sound effects layer

Layer ambient audio (waves, crowd, birds) keyed to scene type under photo-only sections.
Achievable in FFmpeg with amix. Would add atmosphere to silent photo sequences.

### F2. J-cuts / L-cuts

Start next scene's audio 0.5-1s before video cuts (J-cut), or let previous audio linger (L-cut).
Creates seamless transitions. Requires splitting audio/video timelines.

### F4. Issue #3: Parallelize music gen + clip rendering

Music generation and Phase 1 clip rendering are independent. Run in parallel via
threading/multiprocessing. ~2x speedup for the slowest pipeline stages.

### F5. Onset-based beat sync — partially done

BPM grid snapping is implemented via energy envelope autocorrelation (no external deps).
Remaining: replace BPM grid with actual transient/onset detection via librosa for snapping
transitions to drum hits instead of mathematical beats.

### F6. Travel map animation

Animated map showing the trip route between locations. Requires map renderer (Mapbox/OSM).
Not achievable in pure FFmpeg.