# Claude Code Notes

## Pipeline execution

Run pipeline via `python run.py` CLI. Stages execute directly in a single Python process — no external services needed. Each stage caches its output; re-running `full` is fast.

```bash
# Local folder source (all files, no date filtering)
python run.py -n singapore full --source workspace/media --duration 180 --lang cn --tz 8

# NAS source (date range)
python run.py -n singapore full -f 2025-06-13 -t 2025-06-17 --duration 180

# Re-plan only (uses cached media + analysis)
python run.py -n singapore plan --duration 180 --lang cn

# Re-render only
python run.py -n singapore assemble
```

Logs go to terminal AND `workspace/runs/{name}/run.log`. Run summary in `workspace/runs/{name}/run_status.json`.

## Pipeline stages

`fetch_media -> prepare -> plan -> generate_music -> assemble`

5 Dagster assets. All stages use Gemini API (no local AI models needed). Requires `GEMINI_API_KEY` in `.env`.

## Module structure

The assemble stage is split into focused modules:

| Module | Responsibility |
|--------|---------------|
| `assemble.py` | Orchestration: Phase 1 (parallel clip render), Phase 2 (concat), Phase 3 (audio mix), Phase 4 (validation) |
| `encoder.py` | RenderContext, GPU encoder detection, bitrate calculation, ffprobe caching |
| `filters.py` | Color grade, text overlay (drawtext), portrait photo filter, font detection |
| `render.py` | render_photo, render_video, render_title_card |
| `concat.py` | Xfade concatenation, group splitting (MAX_GROUP=10), demuxer fallback |
| `audio.py` | BPM estimation, beat sync, speech track, music mixing, chapter markers |
| `parallel.py` | Shared batched ThreadPoolExecutor runner (used by plan.py and assemble.py) |

## Gemini API call in the plan stage

Single-pass planning with chain-of-thought: Gemini designs narrative arc, selects items,
and self-reviews in one API call. Contact sheets use 12 photos/sheet at 600px, q=75.

Prompts are externalized to `pipeline/prompts/` (editable without code changes):
- `visual_planner_system.md` — main system prompt template
- `narrative_guidance.json` — per-trip-type narrative rules
- `lang_instructions.json` — language directives (en/cn/both)

Fault tolerance: fuzzy path matching for hallucinated file paths, duration check
(warns if EDL is underfilled).

| Input | What Gemini does |
|-------|-----------------|
| Contact sheets (12/sheet @ 600px, inline) + 1 concatenated video preview (360p 1fps, Files API) + metadata | Design narrative arc → select items → assign music_mood/keep_audio/playback_speed/transitions/color_temp → self-review |

Model: `gemini-2.5-flash` (stable). Total cost: ~$0.06 per run.

Every API call is logged with: model, input token count, output tokens, wall time, response preview.

## What Gemini controls (e2e)

- **Photo/video selection** — Gemini sees actual photos via contact sheets, judges visually
- **Video clips with audio** — Gemini watches 1 concatenated 360p 1fps preview (all videos stitched together with offset table), judges motion/framing/speech
- **keep_audio** — Gemini sets `keep_audio=true` on videos where it hears meaningful speech/laughter
- **Chapter structure** — narrative chapters by story beat, not location/time buckets
- **Video trim points** — `start_time`/`end_time` for selecting best moments from video clips
- **Speed ramps** — `playback_speed` per item (0.5 slow-mo, 1.0 normal, 1.5 fast)
- **Music mood** — per-segment descriptions fed to Lyria RealTime prompt
- **Text overlays** — evocative titles, not just "Day 1 - Marina Bay"
- **Pacing** — display_duration per item, effect choices
- **Transitions** — 7 xfade types (crossfade, dissolve, smoothleft, smoothright, circlecrop, fade_black, wipe_left) varied per segment
- **Montage mode** — segment `mode: "montage"` for quick-cut energy bursts
- **Color temperature** — per-segment `color_temp` (warm/cool/neutral)

## What's still hard-coded

- **Family detection** — family_count from NAS face data, used for Dagster metadata display only (Gemini judges visually)
- **FFmpeg rendering** — parallel clip assembly from EDL (3 NVENC workers by default, `VLOG_PARALLEL_CLIPS` env var)
- **Ken Burns effects** — applied per EDL effect field (forced to "none" for videos)
- **Thumbnail/keyframe generation** — Pillow resize, FFmpeg extraction
- **Codec** — HEVC (hevc_nvenc) on GPU, H.264 (libx264) on CPU; auto-detected
- **Bitrate** — HEVC at 65% of H.264 YouTube rates with `--quality` multiplier
- **Audio ducking** — music volume drops to 30% during speech clips (fixed ratio)
- **Color grading** — subtle contrast/saturation boost, temperature shift per segment
- **YouTube chapter markers** — timestamps from EDL segment boundaries
- **Text overlays** — baked into clips during render (single FFmpeg pass, no separate overlay step)

## Debugging & iteration

- After submitting a Dagster run, check logs within 10-20 seconds to verify each stage works
- Check `contact_sheets/` directory for generated grid images sent to Gemini
- Every Gemini API call logs: model, tokens in/out, timing, response preview
- Every FFmpeg command logged at INFO level in Dagster and to `output/ffmpeg_commands.log`
- Use `--force-prepare` to force re-analysis (bypasses cached analysis.json)
- Check Dagster UI at localhost:3000 for run events and logs
- YouTube chapter markers saved to `output/chapters_v{N}.txt`
- Post-assemble validation: 6 automated checks (file size, duration, streams, codec, A/V sync, resolution)

## Key gotchas

- Contact sheets must stay under 2000px in any dimension — large chapters auto-split into multiple sheets (max 12 per sheet)
- Video previews: individual 360p 1fps CRF40 clips concatenated into 1 mega-preview, uploaded via Files API. Images sent inline (~44MB). Inline base64 limit is 100MB (~75MB raw).
- Preview generation uses `-hwaccel auto -skip_frame nokey` for ~22x speedup (only decodes keyframes)
- Photo thumbnails cached in `workspace/thumbnails/`, video analysis cached in `workspace/analysis_cache/`
- Preview clips cached in `workspace/preview_clips/` — orphaned files from old runs auto-cleaned
- `--source` flag for local folder (alternative to NAS `-f`/`-t` date range)
- tqdm auto-disabled when stderr is not a TTY
- Stale cache auto-invalidation: prepare re-runs if upstream file is newer (mtime check)
- FFmpeg subprocesses have a 5-minute timeout (prevents hanging on corrupt files)
- Ken Burns is forced to "none" on video items (native motion conflicts with zoompan)
- `--music auto` uses Gemini Lyria RealTime; `--music local` uses MusicGen — single flag controls both backend and intent
- `--lang en|cn|both` controls text language (title, overlays, chapters); cn/both auto-selects CJK font
- Clip rendering is parallel via `parallel.run_parallel()`: 3 workers for NVENC, cpu_count/2 for libx264
- HEVC NVENC auto-detected; falls back to H.264 NVENC → libx264. CPU stays H.264 (H.265 CPU is too slow)
- ffprobe results cached per assemble run via RenderContext (dimensions + duration)
- Text overlays baked into clips via drawtext filter (no separate encode pass)
- Render settings (resolution, fps, quality) stored in EDL at plan time, read by assemble
- RenderReport tracks per-clip status (ok/skipped/failed with reason)
