# Claude Code Notes

## Repository structure

```
reelsmith/
├── pipeline/                  # Main source package
│   ├── _types.py              # Shared TypedDicts (ManifestEntry, AnalysisEntry, PreprocessedData)
│   ├── config.py              # Config dataclass (workspace paths, env loading)
│   ├── edl.py                 # EDL Pydantic model + all enums (MediaType, Effect, Transition, etc.)
│   ├── prepare/               # Stage 2: media preprocessing
│   │   └── _prepare.py        #   Thumbnails, ffprobe, preview clips
│   ├── plan/                  # Stage 3: Gemini EDL generation
│   │   ├── _gemini.py         #   Raw Gemini API interaction + logging
│   │   ├── _orchestrate.py    #   Plan orchestrator (prepare input → call Gemini → postprocess)
│   │   ├── _preview.py        #   Mega-preview video builder + inline thumbnails + metadata
│   │   ├── _postprocess.py    #   Timestamp conversion, fuzzy paths, trim clamping, dedup
│   │   └── _prompts.py        #   System prompt loading + trip-type/language templating
│   ├── music/                 # Stage 4: Lyria music generation
│   │   ├── _gemini.py         #   Lyria RealTime API wrapper
│   │   ├── _orchestrate.py    #   Per-segment music generation from EDL moods
│   │   └── _prompts.py        #   Music mood templates
│   ├── assemble/              # Stage 5: FFmpeg rendering
│   │   ├── _assemble.py       #   Orchestrator (render → concat → music mix → validation)
│   │   ├── _encoder.py        #   RenderContext, GPU detection, bitrate calculation
│   │   ├── _filters.py        #   Ken Burns, color grade, text overlay, portrait filter
│   │   ├── _graph.py          #   FFmpeg filter graph builder (video/audio chains, concat)
│   │   ├── _render.py         #   render_photo, render_video, render_title_card
│   │   └── _audio.py          #   BPM detection, beat sync, music ducking, chapters
│   ├── cli/                   # CLI package (Click-based)
│   │   ├── _commands.py       #   CLI group + command definitions (full/prepare/plan/assemble/workspace)
│   │   ├── _runner.py         #   Pipeline stage orchestration + PipelineContext
│   │   ├── _display.py        #   Rich UI (progress bars, stage status icons)
│   │   ├── _config_io.py      #   Run config save/load (YAML persistence)
│   │   └── _workspace.py      #   Workspace list/clean commands
│   ├── utils/                 # Shared utilities
│   │   ├── image.py           #   gen_thumbnail(), extract_exif() (Pillow)
│   │   ├── media.py           #   probe_video(), gen_preview(), ffmpeg_cmd() (FFmpeg wrappers)
│   │   └── parallel.py        #   run_parallel() batched ThreadPoolExecutor
│   └── prompts/               # External prompt templates (editable without code changes)
│       ├── visual_planner_system.md    # Main Gemini system prompt (templated)
│       ├── narrative_guidance.json     # Per-trip-type narrative rules
│       └── lang_instructions.json      # Language directives (en/cn/both)
├── tests/                     # 28 test files (~6600 LOC)
├── docs/                      # Architecture decisions, plans, metrics
├── pyproject.toml             # Dependencies, entry points, tool config
├── .pre-commit-config.yaml    # Ruff lint/format + pytest hooks
├── .env.example               # GEMINI_API_KEY
├── CHANGELOG.md               # Version history
└── workspace/                 # Generated artifacts (gitignored)
    ├── runs/{name}/           #   Per-run: manifest, analysis.json, EDL, clips, output, logs
    ├── thumbnails/            #   400px JPEG cache
    ├── previews/         #   480p 1fps preview videos
    └── music/                 #   Generated music tracks
```

## Environment

- Python 3.11+ required
- When installing Python packages, always use the project's virtual environment (e.g., `source venv/bin/activate` or `.venv/Scripts/activate` on Windows), never install to system Python
- External dependency: FFmpeg (required for prepare + assemble stages)
- Requires `GEMINI_API_KEY` in `.env` (plan + music stages)

## Dependencies

**Runtime:** pydantic, click, Pillow, python-dotenv, google-genai, pillow-heif, reverse_geocode, rich, pyyaml

**Dev:** pytest (≥8.0), ruff (≥0.8), pyright (≥1.1), pre-commit (≥4.0)

Entry point: `reelsmith = "pipeline.cli:cli"` (pyproject.toml)

## Code Style / General Rules

- When making changes, do NOT abbreviate, shorten, or simplify user-provided content (commands, flag names, text strings) unless explicitly asked. Preserve original wording exactly.
- Use `logger.info("msg", arg)` formatting — no f-strings in log calls (lazy logging).
- `StrEnum` for all categorical values in `edl.py` — not raw `Literal` types.
- `TypedDict` for cross-stage data contracts in `_types.py`.

## Platform Compatibility

This project targets both Mac and Windows. Always consider cross-platform compatibility: use `os.path` or `pathlib`, handle both Unix and Windows venv paths, and test PATH handling for both platforms.

## Testing

- After refactoring or removing code, always run the full test suite (`pytest`) before committing. Removing defensive patterns like `.get()` has previously exposed hidden bugs requiring fixture updates across multiple test files.
- Default `pytest` excludes integration tests. Run `pytest -m integration` for FFmpeg tests.
- Tests are organized by pipeline stage: `test_prepare.py`, `test_plan.py`, `test_assemble.py`, etc.
- Shared fixtures in `tests/conftest.py` (sample_manifest, sample_edl, mock configs).

## Pre-commit hooks

Configured in `.pre-commit-config.yaml`:
- **ruff check** — lint with auto-fix (`--fix`)
- **ruff format** — code formatting
- **pytest** — runs before commit

## Workflow Rules

When making excessive or multi-file changes, pause and confirm scope with the user before proceeding. Do not restructure formats, add credentials, or change architectural approaches without asking first.

## ⚠️ End-to-end thinking required

**All code and all prompts MUST be aligned.** End-to-end means every prompt claim matches
actual code behavior, and every code behavior is accurately described in the prompt.

**Any behavior change MUST be evaluated across the full pipeline (prepare → plan → assemble).**
Stages are tightly coupled through data contracts — a change in one stage can silently break
or degrade another. Before modifying any stage:

1. Trace the data flow: what does this stage consume? What does it produce? Who consumes that?
2. Check if the prompt tells Gemini something that the renderer handles differently
3. Check if postprocessing silently overrides what Gemini outputs
4. Never optimize a single stage in isolation — verify the end-to-end effect
5. When changing code, update the prompt if it describes the changed behavior
6. When changing prompts, verify the code actually implements what the prompt claims

Examples of past bugs caused by local-only thinking:
- Prompt said "pick ~45 items" but math produced 252s for a 180s target (budget formula didn't account for video/photo duration mix)
- Prompt told Gemini to choose `effect` for videos, but assemble forced `effect="none"` on all videos (dead instruction wasting tokens)
- Prompt showed `start_time/end_time` in schema, but Gemini was told to output `preview_start/preview_end` (postprocess converts between them)
- Prompt said "select for visual quality, not audio" which made Gemini ignore speech — but Gemini is the ONLY component that hears audio in the entire pipeline

## Pipeline execution

Run pipeline via `reelsmith` CLI. Stages execute directly in a single Python process — no external services needed. Each stage caches its output; re-running `full` is fast.

```bash
# Full pipeline from local folder
reelsmith full -n singapore -p ./photos -r 4k60 --duration 180 --lang cn

# Prepare only (scan + media processing)
reelsmith prepare -n singapore -p ./photos

# Re-plan only (no render)
reelsmith plan -n singapore --duration 180 --lang cn

# Render at 1080p30 (output: reelsmith_v1_1080p30.mp4)
reelsmith assemble -n singapore -r 1080p30

# Render at 4K60 (output: vlog_v1_2160p60.mp4, reuses 1080p clips won't conflict)
reelsmith assemble -n singapore -r 4k60

# Custom resolution
reelsmith assemble -n singapore -r 2560x1440x60
```

Logs go to terminal AND `workspace/runs/{name}/run_{timestamp}.log`.

## Pipeline stages

`prepare -> plan -> generate_music -> assemble`

4 stages in a single Python process. Only `plan` and `generate_music` call Gemini API. Requires `GEMINI_API_KEY` in `.env`.

## End-to-end data flow (prepare → plan → assemble)

Understanding exactly what each stage produces and consumes is critical for prompt engineering
and behavior changes. Data contracts between stages are implicit — breaking them causes silent
degradation, not crashes.

### Stage 1: prepare (`pipeline/prepare/_prepare.py`)

**Input:** Raw media files (photos + videos) from a local folder.

**What it does per photo:**
- Generate 400px JPEG thumbnail (cached in `workspace/thumbnails/`)
- Extract EXIF: focal_length, aperture, ISO
- Extract location (country, region, district) and timestamp

**What it does per video:**
- ffprobe: duration, width, height, fps, orientation (landscape/portrait)
- **NO audio analysis** — no speech detection, no loudness, no transcript, no speech timestamps
- Generate preview clip (480p 1fps WITH AUDIO, mono 64kbps AAC, via parallel workers)

**What it does globally:**
- Write all results to `workspace/runs/{name}/analysis.json`

**Output:** analysis.json + thumbnails + preview clips.

**Critical implication:** Gemini is the ONLY component in the entire pipeline that hears video
audio. There is no speech-to-text, no audio segmentation, no "speech at 5s-12s" metadata.
If the prompt doesn't tell Gemini to listen carefully and trim around speech, nobody else will.

### Stage 2: plan (`pipeline/plan/`)

**Input to Gemini (built by `_preview.py`):**
- System prompt from `visual_planner_system.md` (templated with trip_type guidance + language)
- Intro text with: trip summary, focus directive, duration target, item count, video ratio, step-by-step + self-review checklist
- Text metadata block: flat numbered list (#01, #02, ...) with per-item metadata
  - Photos: `#01: Alice at=Marina Bay 50mm f/2.0 ISO400 file=IMG_2025.heic`
  - Videos: `#03: family at=Singapore video=45s 1920x1080 (landscape) 24fps preview=02:07-03:52 file=DJI_001.mp4`
- Inline photo thumbnails (400px JPEG, base64, up to 75MB limit)
- 1 mega-preview video (all clips concatenated, #XX labels burned in, WITH AUDIO) via Files API

**What Gemini outputs:** JSON EDL with segments → items, each with:
`source_file, media_type, display_duration, preview_start/preview_end (MM:SS in mega-preview), effect, playback_speed, keep_audio, text_overlay`

**Postprocessing pipeline (`_postprocess.py`):**
1. `parse_and_convert_timestamps` — convert preview MM:SS → local trim seconds using offset table. Fuzzy: midpoint matching, best-overlap fallback, min-2s guard (widens short clips)
2. `fix_hallucinated_paths` — fuzzy match filenames (glob, underscore normalization). Picks `candidates[0]` if multiple match
3. `validate_trim_points` — clamp to actual video duration, ensure min 2s, recalculate display_duration from trim+speed
4. `deduplicate_items` — remove duplicate source_file (keep first occurrence)
5. `validate_and_fix_edl` — fix media_type mismatches (video extension on photo item)
6. Force `effect="none"` on all video items (in `_orchestrate.py`)

**Output:** Versioned EDL JSON saved to `workspace/runs/{name}/edl_v{N}.json`.

**Critical implications:**
- Gemini outputs preview_start/preview_end (mega-preview timestamps), postprocess converts to start_time/end_time (local video seconds). The prompt must teach Gemini to use preview timestamps.
- Fuzzy path matching is silent and can pick wrong files. Postprocess logs warnings when multiple candidates match.
- display_duration for videos is auto-corrected from trim points — Gemini's value is guidance, not law.
- Items with unfixable paths or invalid trims are REMOVED (item count can shrink).

### Stage 3: assemble (`pipeline/assemble/`)

**Input:** EDL JSON + original media files + generated music (if any).

**Phase 1: Render segments** (`_graph.py` builds FFmpeg filter graphs)
- Per-photo: Ken Burns (crop + lanczos scale with cosine easing) + color grade + optional text overlay → video stream. Audio = `aevalsrc=0` (silence).
- Per-video with `keep_audio=true`: `atrim=start:duration` + `atempo` (if speed≠1.0) + `asetpts` → preserves original audio from trim window.
- Per-video with `keep_audio=false`: video trimmed + speed-adjusted. Audio = `aevalsrc=0` (silence).
- All items concat'd with `concat=n=N:v=1:a=1` (audio locked to video).
- Encoded as AAC 192k + HEVC/H.264. Output: per-segment `.ts` files.

**Phase 2: Concat + music mix** (`_assemble.py`)
- TS demuxer concatenation (no re-encode: `-c:v copy -c:a copy`)
- Music overlay: `sidechaincompress` dynamic ducking + `amix`
  - **Dynamic ducking** — music automatically fades down when speech plays, fades back up when speech stops
  - `sidechaincompress=threshold=0.02:ratio=6:attack=200:release=500` on music, keyed by speech track
  - Default music volume: 0.40 (ducked to ~15% during speech, full 40% during silence)

**Phase 3: Beat sync** (`_audio.py`)
- Aligns transitions to music beats (autocorrelation BPM detection, half-beat grid)
- Per-item speech skip: only skips transitions where the item being adjusted has keep_audio=true
- Snaps both intra-segment transitions AND segment boundaries

**Phase 4: Validation**
- 6 checks: file size, duration, streams, codec, A/V sync, resolution

**Critical implications for prompt engineering:**
- `keep_audio=true` preserves audio for the ENTIRE trim window, not just speech portions. Gemini should trim tightly around speech to minimize music suppression.
- `keep_audio=false` = complete silence (not quiet music — SILENCE). Only background music plays.
- Beat sync skips per-item (not per-segment): only keep_audio=true items are anchored, other items in the same segment can still snap to beats.
- Photos are always silent — `keep_audio` on photos is a validation error.
- effect field is ignored for videos (forced to "none") — don't waste prompt tokens teaching Gemini to pick effects for videos.
- `playback_speed` affects both video AND audio (atempo filter). 0.5x = slow-mo with pitch-preserved audio.

### Cross-stage data contracts

| Producer | Data | Consumer | Gotcha |
|----------|------|----------|--------|
| prepare | video fps ≥48 | prompt | Gemini uses this to decide playback_speed=0.5 |
| prepare | preview offset table | plan postprocess | Converts Gemini's preview MM:SS to local trim seconds |
| prepare | NO audio metadata | plan prompt | Gemini must listen to preview audio — no other component does |
| plan | preview_start/preview_end | postprocess | Converted to start_time/end_time; original fields are popped |
| plan | keep_audio flag | assemble _graph.py | Determines atrim+atempo (preserve) vs aevalsrc=0 (silence) |
| plan | effect field (videos) | _orchestrate.py | Force-overwritten to "none" — prompt should not ask for video effects |
| plan | display_duration | postprocess | Auto-corrected from trim+speed — Gemini's value is advisory |
| plan | music_mood | generate_music | Sent directly to Lyria as text prompt |
| assemble | segment .ts files | concat | Demuxer concat, no re-encode |
| assemble | keep_audio flag | beat sync | Per-item: keep_audio=true items skip beat snap, others in same segment still eligible |

## Module structure

Rendering modules (`pipeline/assemble/` package, `parallel.py` also used by plan):

| Module | Responsibility |
|--------|---------------|
| `assemble/_assemble.py` | Orchestration: Phase 1 (parallel clip render), Phase 2 (concat), Phase 3 (audio mix), Phase 4 (validation) |
| `assemble/_encoder.py` | RenderContext, GPU encoder detection, bitrate calculation, ffprobe caching |
| `assemble/_filters.py` | Color grade, text overlay (drawtext), portrait photo filter, font detection |
| `assemble/_render.py` | render_photo, render_video, render_title_card |
| `assemble/_graph.py` | FFmpeg filter graph builder: per-item video/audio chains, concat |
| `assemble/_audio.py` | BPM estimation, beat sync, speech track, music mixing, chapter markers |
| `parallel.py` | Shared batched ThreadPoolExecutor runner (used by plan and assemble) |

## Gemini API call in the plan stage

Single-pass planning with chain-of-thought: Gemini designs narrative arc, selects items,
and self-reviews in one API call.

Prompts are externalized to `pipeline/prompts/` (editable without code changes):
- `visual_planner_system.md` — main system prompt template
- `narrative_guidance.json` — per-trip-type narrative rules (family/solo/food/adventure/architecture/general)
- `lang_instructions.json` — language directives (en/cn/both)

Fault tolerance: fuzzy path matching for hallucinated file paths, trim point clamping,
deduplication, duration check with optional follow-up Gemini call to fill gaps.

| Input | What Gemini does |
|-------|-----------------|
| Individual photos (400px thumbnails, inline) + 1 concatenated video preview (480p 1fps with audio, #XX labels, Files API) + per-item metadata | Design narrative arc → select items → assign music_mood/keep_audio/playback_speed/transitions/color_temp → self-review |

Model: `--model` is required. Presets: `fast` (gemini-3.1-flash-lite-preview), `balanced` (gemini-3-flash-preview), `quality` (gemini-3.1-pro-preview). Custom: `model:thinking` (e.g. `gemini-2.5-flash:medium`). Default: `gemini-3-flash-preview`.

Every API call is logged with: model, input token count, output tokens, wall time, response preview.

## What Gemini controls (e2e)

- **Photo/video selection** — Gemini sees actual photos (400px thumbnails inline), judges visually
- **Video clips with audio** — Gemini watches 1 concatenated 480p 1fps preview with audio (all videos stitched together with offset table), judges motion/framing/speech
- **keep_audio** — Gemini sets `keep_audio=true` on videos where it hears meaningful speech/laughter
- **Chapter structure** — narrative chapters by story beat, not location/time buckets
- **Video trim points** — `start_time`/`end_time` for selecting best moments from video clips
- **Speed ramps** — `playback_speed` per item (0.5 slow-mo, 1.0 normal, 1.5 fast)
- **Music mood** — per-segment descriptions fed to Lyria RealTime prompt
- **Text overlays** — evocative titles, not just "Day 1 - Marina Bay"
- **Pacing** — display_duration per item, effect choices
- **Transitions** — transition type field stored in EDL but renderer uses opacity fades only; **transition_duration** is the actual creative lever (controls fade length)
- **Montage mode** — segment `mode: "montage"` for quick-cut energy bursts
- **Color temperature** — per-segment `color_temp` (warm/cool/neutral)

## What's still hard-coded

- **FFmpeg rendering** — parallel segment rendering from EDL (3 NVENC workers, 2 VideoToolbox workers)
- **Ken Burns effects** — cosine-eased crop + lanczos scale per EDL effect field (photos only; videos use a separate render path)
- **Thumbnail/keyframe generation** — Pillow resize, FFmpeg extraction
- **Hardware acceleration** — Auto-detected: CUDA (NVIDIA) or VideoToolbox (macOS) for decode; NVENC/VideoToolbox for encode. Falls back to CPU when unavailable.
- **Codec** — HEVC (hevc_nvenc/hevc_videotoolbox) on GPU, H.264 (libx264) on CPU; auto-detected
- **Bitrate** — HEVC at 65% of H.264 YouTube rates with `--quality` multiplier
- **Audio ducking** — Dynamic via `sidechaincompress`: music auto-ducks when speech detected, recovers when speech stops. Default music volume 0.40, ducked to ~15% during speech. Tight trims = less music suppression.
- **Loudness normalization** — Two-pass `loudnorm` (pass 1 measures I/LRA/TP/thresh, pass 2 applies with `linear=true`). Falls back to single-pass if measurement fails.
- **Color grading** — subtle contrast/saturation boost, temperature shift per segment
- **YouTube chapter markers** — timestamps from EDL segment boundaries
- **Text overlays** — baked into clips during render (single FFmpeg pass, no separate overlay step)

## Debugging & iteration

- Every Gemini API call logs: model, tokens in/out, timing, cost estimate, response preview
- Every FFmpeg command logged at INFO level to the run log (`workspace/runs/{name}/run_*.log`)
- Use `--force` on `prepare` or `full` to force re-generation (bypasses per-item cache, thumbnails, previews)
- YouTube chapter markers saved to `output/chapters_v{N}_{res_label}.txt`
- Post-assemble validation: checks (file size, duration, streams, codec, resolution)
- Live progress display with Rich (per-stage status: ○ pending, ⏳ running, ✅ done, ❌ failed)

## Key gotchas

- Photos sent to Gemini as individual 400px thumbnails inline. Videos as 1 concatenated 480p 1fps mega-preview with audio via Files API. Inline base64 limit is 100MB (~75MB raw).
- Preview generation uses `-hwaccel auto`; `-skip_frame nokey` (~22x speedup) only when keyframe interval ≤2s, otherwise full decode
- Photo thumbnails cached in `workspace/thumbnails/`, analysis data written to per-run `analysis.json`
- Preview clips cached in `workspace/previews/` — orphaned files from old runs auto-cleaned
- Rich progress auto-adapts to terminal capabilities
- Prepare always recomputes metadata (EXIF + ffprobe are fast); thumbnails and previews are cached
- FFmpeg subprocesses have a 10-minute timeout for segment renders, 1-minute for concat (prevents hanging on corrupt files)
- Ken Burns uses cosine easing (ease-in/ease-out) via crop+lanczos; only applies to photos (videos use a separate render path)
- `--music auto` uses Gemini Lyria RealTime; `--music /path/to/file` uses custom audio; `--music none` disables music
- `--lang en|cn|both` controls text language (title, overlays, chapters); cn/both auto-selects CJK font
- Segment rendering is parallel via `parallel.run_parallel()`: 3 workers for NVENC, 2 for VideoToolbox
- HEVC auto-detected: hevc_nvenc (Win/Linux) or hevc_videotoolbox (macOS); falls back to H.264 NVENC → libx264
- ffprobe results cached per assemble run via RenderContext (dimensions + duration)
- Text overlays baked into clips via drawtext filter with drop shadow (no separate encode pass)
- Title card uses first EDL photo as blurred background (fallback: purple gradient)
- CLI `prepare` = scan + prepare (thumbnails, EXIF, video probing); CLI `plan` = plan + generate_music (when `--music` is not `none`); CLI `assemble` = render. `full` = all stages
- `--path PATH` is required for `prepare` and `full` commands
- `--resolution` / `-r` is required for both `full` and `assemble` — no default. Presets: 4k60, 4k30, 2k60, 2k30, 1080p60, 1080p30, 720p30, or custom WxHxFPS
- Clips cached per resolution (`seg00_item00_1080p30.mp4`); switching resolution doesn't re-render existing clips
- Output files include resolution: `reelsmith_v1_1080p30.mp4` — different resolutions coexist
- `workspace --clean safe|cache|media|all` — `safe` removes old outputs + intermediates only
- RenderReport tracks per-clip status (ok/skipped/failed with reason)
