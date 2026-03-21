# Pipeline TODO

## Completed

- [x] **Merge preprocess + analyze → prepare** — single prepare stage (5 assets instead of 6)
- [x] **A8. Split monolithic assemble.py** — encoder.py, filters.py, render.py, concat.py, audio.py
- [x] **A5. Integration tests** — 200 tests covering BPM, beat sync, Timeline, xfade, speech, music, e2e render
- [x] **A2. Remove global state** — RenderContext dataclass replaces 4 scattered globals
- [x] **A6. Consistent error handling** — ClipStatus/RenderReport with per-clip status tracking
- [x] **A4. Config pass-through** — render settings (quality) stored in EDL, read by assemble
- [x] **A1. FFmpeg filter abstraction** — FilterGraph builder with label validation
- [x] **A3. Shared parallel utility** — parallel.run_parallel() replaces duplicate ThreadPoolExecutor patterns
- [x] **Issue #2. Gemini fault tolerance** — auto-retry, fuzzy path matching, duration check
- [x] **Issue #5. Post-assemble validation** — 6 automated checks (file, duration, streams, codec, sync, resolution)
- [x] **Issue #7. Externalize prompts** — pipeline/prompts/ with .md/.json files
- [x] **Remove tier concept** — replaced with family_count, "unknown" label for missing data
- [x] **FFmpeg command logging** — all commands logged to Dagster INFO + output/ffmpeg_commands.log
- [x] **Comprehensive Gemini logging** — full I/O logged with [Gemini] prefix

---

## Critical Bugs

### B1. concat.py uses 4K bitrate regardless of actual resolution

`concat.py:113,165` calls `get_encoder()` with no arguments → defaults to 3840x2160 → 45Mbps bitrate
even when rendering at 720p. Produces massively inflated intermediate group files.

**Fix**: Pass actual `(w, h, fps)` through to concat functions, or use `-c:v copy` since clips are
already encoded at the correct bitrate.

### B2. No validation of Gemini's video trim points

Gemini returns `start_time`/`end_time` but these are never checked against actual `video_duration`.
If Gemini hallucinates `start_time=120` on a 60s video, FFmpeg produces an empty clip → silent failure.

**Fix**: In plan.py post-processing (Layer 2), clamp trim points: `start_time = min(start_time, video_duration - 1)`.
Video durations are available in `analysis_by_id["video_duration"]`.

### B3. `selected_items` missing from prepare return value

`definitions.py:289` reads `result.get("selected_items", 0)` but prepare returns only `{family_names, timeline}`.
Dagster UI always shows 0 items for prepare.

**Fix**: Add item count to prepare's return dict or fix definitions.py to read from analysis.json.

### B4. Hard-coded SGT timezone breaks non-Singapore users

`prepare.py:32` hardcodes `SGT = timezone(timedelta(hours=8))`. Photos taken in NYC would be grouped
into wrong days. Morning photos appear in previous evening.

**Fix**: Default to system timezone, or infer from EXIF GPS, or make configurable via `--timezone` flag.

### B5. `_format_date_range` uses `%-d` which crashes on Windows

`plan.py:87` uses Unix-specific `%-d` format. Multi-month trips on Windows → `ValueError`.

**Fix**: Use `first.day` (Python int) instead of `strftime('%-d')`.

---

## Video Quality Improvements

### V1. Eased Ken Burns motion curves (HIGH IMPACT, LOW EFFORT)

Current zoompan is linear → looks like a screensaver. Professional Ken Burns uses ease-in/ease-out.

**Fix**: Replace `zoom+rate` with cosine ease: `zoom + rate * (1 - cos(PI * on / d)) / 2`
in `media_utils.py:_zoompan_filter`. FFmpeg zoompan evaluates expressions per frame.

### V2. Smooth audio ducking ramps (HIGH IMPACT, LOW EFFORT)

Current ducking is a hard step function — instant volume jump at speech boundaries sounds amateur.

**Fix**: Replace `if(between(t,start,end), duck, full)` with gradual ramps:
- 300ms attack (fade-down before speech starts)
- 1000ms release (fade-up after speech ends)
In `audio.py:add_music` volume expression.

### V3. Drop shadow instead of text border (MEDIUM, LOW EFFORT)

`borderw=2:bordercolor=black` looks like closed captioning.

**Fix**: Replace with `shadowcolor=black@0.6:shadowx=3:shadowy=3` in `filters.py:drawtext_filter`.
Add fade-in/fade-out alpha animation (copy approach from title card).

### V4. Stronger color grading (MEDIUM, LOW EFFORT)

- `contrast=1.02` is imperceptible. Add S-curve via `curves=m='0/0 0.25/0.20 0.75/0.83 1/1'`
- Double warm/cool color balance values (0.02 → 0.04) to be actually visible
- Add subtle vignette `vignette=PI/5` to all clips

**Fix**: Update `filters.py:color_grade` and add vignette to render.py photo/video filter chains.

### V5. Let Gemini control inter-segment transitions (MEDIUM, LOW EFFORT)

`assemble.py:220-221` hard-codes `fade_black` for all segment boundaries, overriding Gemini's choice.

**Fix**: Add `inter_transition` field to Segment model, or let the first item of each segment carry
its own transition from Gemini. Remove the hard-coded override.

### V6. Add `fadewhite` transition (LOW EFFORT)

Essential for bright outdoor scenes (beach → new scene). FFmpeg xfade supports `fadewhite` natively.

**Fix**: Add to `edl.py:Segment.transition` Literal type and `concat.py` transition map.

### V7. Portrait background darkening (LOW EFFORT)

Blurred portrait background should be darkened 15-20% for better subject separation.

**Fix**: Add `eq=brightness=-0.15` after `gblur` in `filters.py:build_portrait_photo_filter`.

### V8. Reduce default photo/video durations in prompt (LOW EFFORT)

Current: photos 3-5s, videos 5-10s. Modern YouTube travel vlogs use shorter cuts.

**Fix**: Update `pipeline/prompts/visual_planner_system.md`: photos 2.5-4s, videos 4-8s.

### V9. Hero-photo title card background (MEDIUM EFFORT)

Fixed purple gradient looks template-y and clashes with trip content.

**Fix**: Option to use the first photo from the EDL as title card background:
blur (sigma=40) + darken (brightness=-0.3) + vignette + text overlay.
Achievable in FFmpeg filter chain in `render.py:render_title_card`.

### V10. Beat sync: tighter max_shift for montage (LOW EFFORT)

0.4s shift on a 1.5s montage clip is a 27% duration change. Too aggressive.

**Fix**: In `audio.py:beat_snap_edl`, use `max_shift=0.2` when segment mode is "montage".

---

## Engineering Improvements

### E1. Unify clip timing to one source of truth

Three independent offset implementations must agree:
- `timeline.py:Timeline.build()` — used by `concat_xfade`
- `concat.py:compute_actual_offsets()` — used by assemble for speech
- `audio.py:write_chapters()` — its own accumulation loop

**Fix**: Make Timeline the single consumer. `compute_actual_offsets` and `write_chapters`
should read from Timeline, not re-derive offsets.

### E2. `playback_speed` not accounted for in duration estimates

`render_video` at speed=0.5 produces a 20s clip from a 10s source, but `edl.estimated_duration()`
and timeline placement use `display_duration` without speed adjustment.

**Fix**: Either adjust `display_duration` in plan post-processing, or account for speed in
`estimated_duration()` and timeline building.

### E3. `highlight_montage` and `last_hero` styles: implement or remove

Declared in EDL model, accounted for in duration math, but never rendered.
If Gemini sets them, the intro/outro is silently skipped but duration estimates are off.

**Fix**: Implement (highlight montage = quick cuts of best clips; last hero = final photo
held with fade) or remove from the Literal type to prevent Gemini from using them.

### E4. Render failures lost in parallel workers

`render_photo`/`render_video` print FFmpeg stderr to stdout and return silently.
In parallel execution, these prints interleave and the actual error is lost.

**Fix**: Return the error message from render functions so RenderReport captures it.

### E5. `FilterGraph` barely adopted

Built as an abstraction but only used for portrait photo filter. All other filter construction
is still raw string concatenation. Two competing paradigms confuse contributors.

**Fix**: Incrementally convert remaining filter chains (landscape photo, video, title card)
to use FilterGraph, or document that FilterGraph is for complex multi-node chains only.

---

## Feature Ideas (Aspirational)

### F1. Ambient sound effects layer

Layer ambient audio (waves, crowd, birds) keyed to scene type under photo-only sections.
Achievable in FFmpeg with amix. Would add atmosphere to silent photo sequences.

### F2. J-cuts / L-cuts

Start next scene's audio 0.5-1s before video cuts (J-cut), or let previous audio linger (L-cut).
Creates seamless transitions. Requires splitting audio/video timelines.

### F3. Folder-based media source

Alternative to Synology NAS — scan a local folder of photos/videos. Build manifest from
EXIF (date, GPS → reverse geocode to location). No NAS required.

### F4. Issue #3: Parallelize music gen + clip rendering

Music generation and Phase 1 clip rendering are independent. Run in parallel via Dagster
multi-process executor. ~2x speedup for the slowest pipeline stages.

### F5. Onset-based beat sync

Replace BPM grid with actual transient detection via librosa. Snaps transitions to drum hits
instead of mathematical beats. Much better sync with real music.

### F6. Travel map animation

Animated map showing the trip route between locations. Requires map renderer (Mapbox/OSM).
Not achievable in pure FFmpeg.
