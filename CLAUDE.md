# Claude Code Notes

## Pipeline execution

Always run pipeline stages through Dagster (via `python run.py` CLI or Dagster UI), never by calling pipeline functions directly in Python. Running stages directly bypasses Dagster's run tracking and causes process management issues.

Use `dagster dev` (not just `dagster-webserver`) — the daemon is needed for the run queue coordinator.

```bash
# Correct
python run.py -n singapore full -f 2025-06-13 -t 2025-06-17 --duration 180
python run.py -n singapore resume

# Wrong — don't do this
python -c "from pipeline.prepare import prepare; prepare(cfg)"
```

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
| `filtergraph.py` | Typed FilterGraph builder with label validation for FFmpeg filter_complex |
| `render.py` | render_photo, render_video, render_title_card |
| `concat.py` | Xfade concatenation, group splitting (MAX_GROUP=10), demuxer fallback |
| `audio.py` | BPM estimation, beat sync, speech track, music mixing, chapter markers |
| `parallel.py` | Shared batched ThreadPoolExecutor runner (used by plan.py and assemble.py) |

## Gemini API call in the plan stage

Single-pass planning with chain-of-thought: Gemini designs narrative arc, selects items,
and self-reviews in one API call. Contact sheets use 6 photos/sheet at 600px.

Prompts are externalized to `pipeline/prompts/` (editable without code changes):
- `visual_planner_system.md` — main system prompt template
- `narrative_guidance.json` — per-trip-type narrative rules
- `lang_instructions.json` — language directives (en/cn/both)

Fault tolerance: auto-retry on parse failure, fuzzy path matching for hallucinated
file paths, duration check (warns if EDL is underfilled).

| Input | What Gemini does |
|-------|-----------------|
| Contact sheets (6/sheet @ 600px) + video clips (320p 10fps with audio) + metadata | Design narrative arc → select items → assign music_mood/keep_audio/playback_speed/transitions/color_temp → self-review |

Total cost: ~$0.03 per run on Gemini 3 Flash.

Every API call is logged with: model, input token count, output tokens, wall time, response preview.

## What Gemini controls (e2e)

- **Photo/video selection** — Gemini sees actual photos via contact sheets, judges visually
- **Video clips with audio** — Gemini watches 320p 10fps MP4 samples with audio, judges motion/framing/speech
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

- **Prepare tiering** — face count -> A/B/C/D (but Gemini ignores tiers, judges visually)
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

- Contact sheets must stay under 2000px in any dimension (Gemini API limit for multi-image requests) — large chapters auto-split into multiple sheets (max 28 per sheet)
- Video clips sent to Gemini are 320p 10fps MP4 samples with audio (CRF35, ~80% smaller than 480p)
- Photo thumbnails cached in `workspace/thumbnails/`, video analysis cached in `workspace/analysis_cache/`
- Dagster: use `dagster dev` not `dagster-webserver` (daemon needed for run queue)
- tqdm auto-disabled when stderr is not a TTY (prevents BrokenPipeError in Dagster)
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
