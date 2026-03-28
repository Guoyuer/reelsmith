# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.1.0] — 2026-03-25

First public release. A fully functional AI-powered vlog generator that takes raw
photos and videos, plans a narrative with Gemini, generates music via Lyria, and
renders a polished highlight reel — all for ~$0.03 per run.

### Highlights

- **5-stage pipeline** (`fetch → prepare → plan → generate_music → assemble`) running
  in a single Python process via the `vlog` CLI
- **Gemini visual planning** — Gemini sees actual photo thumbnails and watches a
  concatenated video preview with audio, then designs the narrative arc, selects items,
  and outputs a structured EDL in one API call
- **Dynamic music ducking** — background music auto-ducks when speech is detected via
  FFmpeg `sidechaincompress`, no manual keyframing needed
- **Beat-synchronized transitions** — cuts snap to music beats via autocorrelation BPM
  detection; speech segments are preserved without snapping
- **Multi-resolution rendering** — 4K60, 1080p30, and custom resolutions coexist;
  clips are cached per resolution and never re-rendered
- **Cross-platform** — runs on macOS (VideoToolbox), Linux/Windows (NVENC), and
  CPU-only (libx264) with automatic codec detection

### Added

- **CLI commands**: `vlog full`, `vlog prepare`, `vlog plan`, `vlog assemble`,
  `vlog config`, `vlog workspace` with Rich progress display
- **Media source**: local folder (`--path PATH`) with automatic EXIF/GPS extraction
- **Prepare stage**: 400px JPEG thumbnails, ffprobe video analysis, family detection
  (top 5 persons in ≥3% of items), preview clip generation (480p 1fps with audio)
- **Plan stage**: single-pass Gemini planning with chain-of-thought; externalized
  prompts in `pipeline/prompts/` (Markdown + JSON, editable without code changes)
- **Structured output**: Gemini returns guaranteed valid JSON via `response_schema`,
  eliminating JSON parse failures and truncation issues
- **Postprocessing pipeline**: fuzzy filename matching for hallucinated paths, preview
  timestamp → local trim conversion, trim point clamping, deduplication, media type
  validation
- **Music generation**: per-segment mood descriptions fed to Gemini Lyria RealTime;
  supports `--music auto|none|/path/to/file`
- **Assemble stage**: parallel segment rendering (3 NVENC / 2 VideoToolbox workers),
  TS demuxer concatenation (no re-encode), music mixing with dynamic ducking,
  beat sync, YouTube chapter markers, 6 post-render validation checks
- **Ken Burns effects**: cosine-eased crop + lanczos scale with 5 motion directions
  (zoom-in, zoom-out, pan-left, pan-right, tilt-up) for photos
- **Blurred background fill**: all videos with non-matching aspect ratios get a
  darkened blurred background instead of black bars (`897f3bb`)
- **Text overlays**: baked into clips via `drawtext` with drop shadow; duration
  scales to clip length instead of a hardcoded 3s cap (`b2b2ebd`)
- **Save/load run config**: every run auto-saves parameters to
  `workspace/runs/{name}/run_config.json`; reload via `--use-cfg-file PATH` (`04d5281`)
- **Narrative guidance**: per-trip-type rules (family, solo, food, adventure,
  architecture, general) in `narrative_guidance.json`
- **Language support**: `--lang en|cn|both` for text overlays, titles, and chapters;
  CJK font auto-selected for Chinese
- **Comprehensive logging**: every Gemini API call logs model, tokens, timing, cost;
  every FFmpeg command logged; CLI parameters logged at pipeline start for
  reproducibility
- **28 test files** (~6600 lines): unit tests for all stages, beat sync (13 cases),
  structured output (17 cases), config save/load (20 cases), plus integration tests
  gated behind `@pytest.mark.integration`

### Fixed

- **5 end-to-end prompt ↔ code mismatches** — prompt claimed behaviors the renderer
  didn't implement (e.g., video effects that were force-overridden to "none") (`3763bea`)
- **16 prompt inaccuracies** — montage duration formula, pillarbox handling, photo
  duration range, text overlay position, and more (`41a90fc`, `109f970`, `8d3b721`)
- **Phantom prompt reference** — display_duration speed calculation referenced a
  removed field (`7898183`)
- **Burst dedup used RGB instead of HSV** — comment said HSV but code used RGB
  histograms; HSV is more robust for exposure-variant burst shots (`4b8cb57`)
- **Music cache key collision** — different mood strings could hash to the same
  cache file (`22881d1`)
- **Stage failure not shown in UI** — Rich panel didn't display ❌ on failure (`e59d4ee`)
- **"preprocess" → "prepare" naming inconsistency** — unified terminology across
  codebase (`61960db`)
- **Postprocessing order** — `validate_and_fix_edl` now runs before video effect
  override to catch errors earlier (`5bb866b`)

### Changed

- **Dynamic music ducking** replaces static `amix` — music volume raised from 0.15
  to 0.40 since `sidechaincompress` handles ducking automatically (threshold=0.02,
  ratio=6, attack=200ms, release=1000ms) (`3e84447`)
- **Beat sync** now snaps segment boundaries (not just intra-segment transitions) and
  skips per-item (not per-segment) for speech preservation (`822a809`)
- **Duration target** tightened from 100-120% to ±5% of user's `--duration` (`f3d0073`)
- **display_duration** is now content-driven (Gemini decides based on visual content)
  instead of fixed 2.5-8s range (`9ca168e`)
- **Text overlay placement** — overlays describe clip content, not segment titles;
  quantity limits removed to let Gemini decide freely (`dded626`, `fb33102`)
- **Prompt architecture** — eliminated conflicting two-pass vs four-step framework;
  consolidated into single 5-step thinking flow (SCAN → FIND PEAKS → DESIGN ARC →
  SELECT & FILL → VERIFY) (`317d0ad`)
- **Gemini parameters** — max_output_tokens increased from 32K to 65K; temperature
  set to 1.0 (Gemini 3 recommended default); `MEDIA_RESOLUTION_LOW` saves ~190K input
  tokens (`1cea7dd`, `30ae1b9`, `55bb422`)
- **Singapore family trip** replaces generic mountain example in prompt for more
  concrete guidance (`6be4b14`)
- **`--tz` option removed** — timezone was never used by Gemini (`bb0e57e`)

### Refactored

- **Repository structure**: `pipeline/utils/` package (image, media, parallel),
  `pipeline/prepare/` package, `pipeline/cli/` package; moved charts and TODO to
  `docs/`; removed root `cli.py` (`06509c7`)
- **Type safety**: `Literal` types replaced with `StrEnum` in `edl.py`; `TypedDict`s
  added for cross-stage data contracts (ManifestEntry, AnalysisEntry, ExifData,
  PreprocessedData); shared `ProgressCallback` type alias; pyright errors fixed
  (`1d734af`, `ee26a04`, `ab78972`)
- **Code style**: consistent `logger.info("msg", arg)` formatting (no f-strings in
  log calls); lazy logging throughout; missing type hints added (`38f692f`, `de2f5fc`)
- **Dead code removed**: unused imports, stale test fixtures, backward-compatibility
  re-exports, `generate_music()` passthrough wrapper, duplicate `probe_duration` and
  `_secs_to_timestamp` (`f56e152`, `116728d`, `efa362a`, `08697b4`, `09fb6eb`)
- **Log noise reduced**: burst dedup, per-video probe, and other verbose logs moved
  to DEBUG; duplicate log lines removed (`f8adccb`, `cfbcb41`, `1c8f0e5`, `e9bd5ce`)

### Project History

This project evolved through 5 architectural phases over 7 days (see `STORY.md`):

1. **v1 — Local AI** (commits 1-50): Ollama (llava:7b + llama3:8b) + Whisper +
   OpenCV face detection + HSV histogram dedup. ~2000 lines of compensatory code
   to work around weak local models.
2. **v2 — Gemini multimodal** (commits 50-80): Switched to Gemini Flash for vision.
   Systematic removal of Ollama, OpenCV, FFmpeg speech detection, and hand-written
   scoring algorithms.
3. **v3 — Single-pass planning** (commits 80-120): Merged 3 API calls into 1.
   Cost dropped from $0.05 to $0.03; quality improved from preserved context.
4. **v4 — Audio engineering** (commits 120-180): Timeline module for A/V sync,
   sidechaincompress ducking, beat-synchronized transitions, Ken Burns with
   cosine easing.
5. **v5 — Polish & open-source prep** (commits 180-202): Structured output,
   type safety, repo restructure, prompt/code alignment audit, comprehensive testing.

[0.1.0]: https://github.com/Guoyuer/vlog/releases/tag/v0.1.0
