# Changelog

All notable changes to this project will be documented in this file.
Format based on [Keep a Changelog](https://keepachangelog.com/).

## [1.0.0] — 2026-03-28

First public release. Major simplification pass for open-sourcing: removed NAS/Synology
coupling and dead abstractions, added model presets and hardware-accelerated decoding,
and raised test coverage to 80%.

### Added

- **Model presets** — `--model fast` (~$0.24/run), `balanced` (~$0.48/run), or
  `quality` (~$1.92/run). Also accepts `model:thinking` for custom combinations
  (e.g. `gemini-2.5-flash:medium`).
- **Free-form instructions** — `--instruct 'no text overlays; prefer slow-motion'`
  passes creative directions straight to Gemini.
- **Hardware-accelerated decoding** — automatically uses VideoToolbox (macOS) or
  CUDA (Linux) when available. ~3x speedup on 4K60 HEVC sources.
- **Two-pass loudness normalization** — measures actual loudness first, then applies
  correction, replacing the less accurate single-pass estimation.
- **Filter graph pre-validation** — decodes every input before rendering to catch
  corrupt files early, with item-level error messages.
- **Rich config panel** — all run parameters displayed in a styled box at pipeline
  start, replacing verbose log lines.
- **Gemini API dashboard** — Rich table after each API call showing token breakdown
  by modality, cost estimate, confidence score, and safety ratings. Gemini thinking
  content rendered as a collapsible Markdown panel.
- **EDL decision stats** — logs selection coverage by location and displays
  narrative rationale in a Rich tree after planning.
- **Postprocess quality gates** — warns when >30% of items are removed or >20% of
  paths are hallucinated; hard-fails above 50% removal.
- **Run config history** — every run auto-saves a timestamped YAML config with
  `# default` annotations. Replay any previous run with `--use-cfg-file`. Strict
  validation on load catches unknown keys, missing fields, and type mismatches.
- **Graceful Ctrl+C** — first interrupt finishes the current FFmpeg process cleanly;
  second force-quits. No more noisy tracebacks.
- **Input validation progress** — Rich panel shows a progress bar during the
  pre-validation phase of assemble.
- **GitHub Actions CI** — ruff lint, pyright type check, and pytest on every push.

### Changed

- **Default trip type** — changed from `family` to `general` so untagged runs don't
  get genre-specific narrative bias.
- **Sidechain compressor release** — reduced from 1000ms to 500ms for tighter music
  ducking around speech.
- **EDL `trip_type` and `style` are now required fields** — no more silent defaults
  that could produce unexpected narrative choices.
- **Single `analysis.json` per run** — replaced the per-file `analysis_cache/`
  directory with one consolidated file.
- **Transition model simplified** — merged `Transition` and `SegmentTransition` into
  one enum, removing 7 unused values.
- **Build artifact cleanup** — filter scripts, concat lists, and title card
  intermediates are now deleted after assemble completes.
- **Workspace directory naming** — `clips_dir` → `render_dir`,
  `preview_clips_dir` → `previews_dir` to better reflect their purpose.
- **Field renames** — `taken_iso` → `taken_at`, `iso` → `iso_speed` for clarity.
- **`target_duration` removed from Gemini response schema** — the model no longer
  outputs this field; it is set exclusively from `--duration`.
- **Default branch** — renamed `master` → `main`.

### Removed

- **NAS/Synology fetch backend** — `--path` (local folder) is now the only media
  source. The `httpx` dependency has been dropped.
- **Family detection** — the `persons` metadata field was always empty without NAS
  face recognition and has been removed.
- **`IntroStyle` / `OutroStyle` enums** — title cards now always render; the style
  selection was unused.
- **`Effect.STATIC`** — merged into `Effect.NONE`. Fixed a bug where the
  `none → zoom-in` fallback silently added Ken Burns to still photos.
- **`VLOG_MODEL` and `WORKSPACE` environment variables** — configuration is now
  handled entirely through CLI flags and `.env`.
- **Dead EDL fields** — `filename` (derivable from path), `id` (replaced by
  `cache_id()`), `fade_black` compatibility shim, `music_duck_ratio` (ducking is
  dynamic via sidechaincompress).

### Fixed

- **Distorted audio on slow-mo speech clips** — slow-motion videos with
  `keep_audio=true` produced pitch-shifted audio. Now uses `atempo` for natural speed.
- **`id` field hash collision** — same-name files in different directories could
  collide. Replaced with path-based `cache_id()`.
- **`--force` ignored in fetch stage** — re-running with `--force` did not regenerate
  the manifest.
- **Title card background** — previously used the first photo alphabetically instead
  of the first EDL item. Now also supports video backgrounds.
- **`--use-cfg-file` path resolution** — relative paths now fall back to
  `workspace/runs/{name}/` when not found at the given path.
- **`target_duration` false error** — validation reported "Invalid target_duration: 0"
  because it ran before the field was set from `--duration`.
- **Missing music on assemble** — if `music_mode=auto` but the music file was missing,
  assemble now auto-triggers music generation instead of failing silently.
- **Music version mismatch** — `vlog assemble -v 2` with auto-generated music now
  generates from that version's segment moods, not the latest EDL.
- **Duplicate terminal output** — stage headers, run config parameters, and music
  generation details were each printed twice.
- **YAML document-end markers** — stripped spurious `---` from saved config files.
- **Rich panel overflow** — widened the panel and sub-stage columns to prevent text
  wrapping on narrow terminals.
- **Segment progress bar** — now visible immediately on render start instead of
  appearing only after the first segment finishes.

### Internal

- Extracted magic numbers to named constants across all modules.
- Split large functions (validators, preview builder, filter graph, beat sync,
  composite music) into focused sub-functions.
- Simplified filter graph builder: deduplicated encode args, inlined single-use
  helpers, switched to list-join pattern for filter strings.
- Extracted stage runner boilerplate into a `_stage()` context manager.
- Demoted verbose per-item logs to DEBUG; added photo thumbnail size and burst dedup
  savings to INFO output.
- Extracted `_ApiStats` dataclass for structured API response logging.
- Removed unnecessary defensive logic across pipeline after audit.
- Consolidated 37 scattered constants into `pipeline/constants.py`.
- Added `EDL.summary()` method and `stderr_console()` helper to reduce duplication.
- Unified `PHOTO_EXTENSIONS` / `VIDEO_EXTENSIONS` into a single source of truth.
- Stage boundary Pydantic validation at prepare → plan handoff.
- Test coverage raised from 64% to 80%. Merged 6 small test files, extracted shared
  fixtures, removed 70 redundant tests (−745 LOC). 40 test files, ~9,000 LOC total.

---

## [0.1.0] — 2026-03-25

Internal milestone: first complete, stable pipeline. Takes raw photos and videos, plans
a narrative with Gemini, generates per-segment music via Lyria, and renders a polished
highlight reel — all from a single CLI command for ~$0.03 per run.

### Highlights

- **5-stage pipeline** — `fetch → prepare → plan → generate_music → assemble`, running
  in one Python process via the `vlog` CLI.
- **Gemini visual planning** — Gemini sees photo thumbnails and watches a concatenated
  video preview (with audio), then designs the narrative arc, selects items, and
  outputs a structured EDL in a single API call.
- **AI-generated music** — per-segment mood descriptions fed to Gemini Lyria RealTime,
  crossfaded into one composite track with automatic ducking during speech.
- **Beat-synchronized transitions** — cuts snap to music beats via autocorrelation BPM
  detection. Speech segments are preserved without snapping.
- **Multi-resolution rendering** — 4K60, 1080p30, and custom resolutions via
  `--resolution` presets. GPU-accelerated encoding (NVENC, VideoToolbox) with
  automatic fallback to libx264.
- **Rich terminal UI** — live-updating progress panel with per-stage status, sub-stage
  progress bars, animated spinner, cost tracking, and a summary table on completion.

### Added

- **CLI commands** — `vlog full`, `vlog prepare`, `vlog plan`, `vlog assemble`,
  `vlog config`, `vlog workspace`.
- **Local folder source** — `--path PATH` scans a directory for photos and videos,
  extracting EXIF dates and GPS coordinates. Reverse geocoding to city/country names.
- **Prepare stage** — generates 400px JPEG thumbnails, runs ffprobe on videos, and
  builds 480p 1fps preview clips with audio. HSV histogram-based burst photo dedup.
- **Plan stage** — single-pass Gemini planning with chain-of-thought. Prompts are
  externalized in `pipeline/prompts/` (Markdown + JSON) and editable without code
  changes. Structured output via `response_schema` eliminates JSON parse failures.
- **Postprocessing pipeline** — fuzzy filename matching for hallucinated paths,
  preview timestamp → local trim conversion, trim point clamping, deduplication,
  and media type validation with auto-fix.
- **Music generation** — per-segment mood descriptions from EDL fed to Gemini Lyria
  RealTime. Segments crossfaded into one composite track. Supports
  `--music auto|none|/path/to/file`.
- **Assemble stage** — per-segment FFmpeg `filter_complex_script` rendering with
  parallel workers. TS demuxer concatenation (no re-encode). Music mixing with
  dynamic ducking via `sidechaincompress`. YouTube chapter markers. 6 post-render
  validation checks.
- **Ken Burns effects** — cosine-eased crop with lanczos scaling and 5 motion
  directions (zoom-in, zoom-out, pan-left, pan-right, tilt-up) for photos.
- **Blurred background fill** — videos and photos with non-matching aspect ratios get
  a darkened blurred background instead of black bars.
- **Text overlays** — baked into clips via `drawtext` with drop shadow. Duration
  scales to clip length. Content-driven placement decided by Gemini.
- **Run config persistence** — every run auto-saves parameters to
  `workspace/runs/{name}/run_config.json`, reloadable via `--use-cfg-file`.
- **Narrative guidance** — per-trip-type narrative rules (family, solo, food,
  adventure, architecture, general) in `narrative_guidance.json`.
- **Language support** — `--lang en|cn|both` for text overlays, titles, and chapter
  markers. CJK font auto-selected for Chinese.
- **Gemini API logging** — every API call logs model, token counts by modality,
  timing, cost estimate, and a response preview.

### Fixed

- **5 prompt ↔ code mismatches** — the prompt described behaviors the renderer didn't
  implement (e.g., video effects that were force-overridden to `none`).
- **16 prompt inaccuracies** — montage duration formula, pillarbox handling, photo
  duration range, text overlay position, and others.
- **Burst dedup used RGB histograms** — switched to HSV, which is more robust for
  exposure-variant burst shots.
- **Music cache key collision** — different mood strings could hash to the same file.
- **Audio-video sync** — multiple fixes across the concat pipeline: exact frame
  durations, SAR normalization, TS intermediate format, speech track alignment.

### Changed

- **Dynamic music ducking** replaces static `amix` — music volume raised from 0.15
  to 0.40 since `sidechaincompress` handles ducking automatically.
- **Beat sync** snaps both segment boundaries and intra-segment transitions, with
  per-item speech preservation.
- **Duration target** tightened from 100–120% to ±5% of the user's `--duration`.
  Gemini decides content-driven display_duration per clip.
- **Prompt architecture** — consolidated from a conflicting two-pass/four-step
  framework into a single 5-step flow: SCAN → FIND PEAKS → DESIGN ARC → SELECT &
  FILL → VERIFY.
- **Gemini parameters** — `max_output_tokens` raised to 65K, temperature set to 1.0,
  `MEDIA_RESOLUTION_LOW` enabled (saves ~190K input tokens per call).

### Internal

- Repository structured as `pipeline/` package with `fetch/`, `prepare/`, `plan/`,
  `music/`, `assemble/`, `cli/`, `utils/` subpackages.
- `StrEnum` for all categorical values. `TypedDict`s for cross-stage data contracts.
  Pyright strict mode with zero errors.
- Consistent lazy logging throughout. Pre-commit hooks: ruff lint/format + pytest.
- 28 test files (~6,600 LOC) covering all stages, plus integration tests behind
  `@pytest.mark.integration`.

### Project History

This project evolved through 5 architectural phases over 11 days (743 commits):

1. **Local AI** (Mar 14–16) — Ollama (llava:7b/13b + llama3:8b), Whisper for speech
   detection, OpenCV YuNet face detection, local MusicGen, Dagster orchestration.
   ~2,000 lines of compensatory code to work around weak local models.
2. **Gemini multimodal** (Mar 17–18) — Switched to Gemini Flash for visual planning.
   Added Lyria RealTime music. Systematic removal of Ollama, OpenCV, Whisper, and
   hand-written scoring algorithms.
3. **Single-pass + audio** (Mar 19–20) — Merged 3 Gemini API calls into 1 (cost
   $0.05 → $0.03). Speech detection via Gemini audio. `sidechaincompress` ducking.
   Beat-synchronized transitions. Per-segment music with crossfade composite.
   Removed Dagster — single-process direct execution.
4. **Architecture rewrite** (Mar 21–22) — Split monolithic files into focused
   subpackages. Rich terminal UI with live progress. Per-segment FFmpeg rendering.
   TS demuxer concat. `vlog` CLI entry point with resolution presets.
5. **Polish** (Mar 23–25) — Structured output (`response_schema`). Ken Burns cosine
   easing. Blurred background fill. 148 tests (52% → 81% coverage). Prompt/code
   alignment audit. Type safety pass. Pre-commit hooks.

[1.0.0]: https://github.com/Guoyuer/vlog/releases/tag/v1.0.0
[0.1.0]: https://github.com/Guoyuer/vlog/releases/tag/v0.1.0
