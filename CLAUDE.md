# Claude Code Notes

## Pipeline execution

Run pipeline via `python run.py` CLI. Stages execute directly in a single Python process — no external services needed. Each stage caches its output; re-running `full` is fast.

```bash
# Full pipeline from local folder
python run.py -n singapore full -s local -p ./photos -r 4k60 --duration 180 --lang cn --tz 8

# Full pipeline from NAS
python run.py -n singapore full -s nas -f 2025-06-13 -t 2025-06-17 -r 1080p30 --duration 180

# Prepare only (fetch + media processing)
python run.py -n singapore prepare -s local -p ./photos --tz 8

# Re-plan only (no render)
python run.py -n singapore plan --duration 180 --lang cn

# Render at 1080p30 (output: vlog_v1_1080p30.mp4)
python run.py -n singapore assemble -r 1080p30

# Render at 4K60 (output: vlog_v1_2160p60.mp4, reuses 1080p clips won't conflict)
python run.py -n singapore assemble -r 4k60

# Custom resolution
python run.py -n singapore assemble -r 2560x1440x60
```

Logs go to terminal AND `workspace/runs/{name}/run_{timestamp}.log`. Run summary in `workspace/runs/{name}/run_status.json`.

## Pipeline stages

`fetch -> prepare -> plan -> generate_music -> assemble`

5 stages in a single Python process. Only `plan` and `generate_music` call Gemini API. Requires `GEMINI_API_KEY` in `.env`.

## Module structure

Rendering modules (primarily assemble, `parallel.py` also used by plan):

| Module | Responsibility |
|--------|---------------|
| `assemble.py` | Orchestration: Phase 1 (parallel clip render), Phase 2 (concat), Phase 3 (audio mix), Phase 4 (validation) |
| `encoder.py` | RenderContext, GPU encoder detection, bitrate calculation, ffprobe caching |
| `filters.py` | Color grade, text overlay (drawtext), portrait photo filter, font detection |
| `render.py` | render_photo, render_video, render_title_card |
| `grouping.py` | Group splitting (MAX_GROUP=10) for xfade reliability |
| `concat.py` | Xfade concatenation, demuxer fallback |
| `audio.py` | BPM estimation, beat sync, speech track, music mixing, chapter markers |
| `parallel.py` | Shared batched ThreadPoolExecutor runner (used by plan.py and assemble.py) |

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
| Individual photos (400px thumbnails, inline) + 1 concatenated video preview (360p 1fps with #XX labels, Files API) + per-item metadata | Design narrative arc → select items → assign music_mood/keep_audio/playback_speed/transitions/color_temp → self-review |

Model: `gemini-3-flash-preview` (default, override via `VLOG_MODEL` env var or `--model` flag).

Every API call is logged with: model, input token count, output tokens, wall time, response preview.

## What Gemini controls (e2e)

- **Photo/video selection** — Gemini sees actual photos (400px thumbnails inline), judges visually
- **Video clips with audio** — Gemini watches 1 concatenated 360p 1fps preview (all videos stitched together with offset table), judges motion/framing/speech
- **keep_audio** — Gemini sets `keep_audio=true` on videos where it hears meaningful speech/laughter
- **Chapter structure** — narrative chapters by story beat, not location/time buckets
- **Video trim points** — `start_time`/`end_time` for selecting best moments from video clips
- **Speed ramps** — `playback_speed` per item (0.5 slow-mo, 1.0 normal, 1.5 fast)
- **Music mood** — per-segment descriptions fed to Lyria RealTime prompt
- **Text overlays** — evocative titles, not just "Day 1 - Marina Bay"
- **Pacing** — display_duration per item, effect choices
- **Transitions** — 8 xfade types (crossfade, dissolve, smoothleft, smoothright, circlecrop, fade_black, wipe_left, fadewhite); separate intra-segment and inter-segment transition fields
- **Montage mode** — segment `mode: "montage"` for quick-cut energy bursts
- **Color temperature** — per-segment `color_temp` (warm/cool/neutral)

## What's still hard-coded

- **Family detection** — family_count from NAS face data (top 5 persons appearing in ≥3% of items)
- **FFmpeg rendering** — parallel clip assembly from EDL (3 NVENC workers by default, `VLOG_PARALLEL_CLIPS` env var)
- **Ken Burns effects** — cosine-eased zoompan per EDL effect field (photos only; videos use a separate render path with no zoompan)
- **Thumbnail/keyframe generation** — Pillow resize, FFmpeg extraction
- **Codec** — HEVC (hevc_nvenc/hevc_videotoolbox) on GPU, H.264 (libx264) on CPU; auto-detected
- **Bitrate** — HEVC at 65% of H.264 YouTube rates with `--quality` multiplier
- **Audio ducking** — music ramps down (300ms attack) to 30% during speech, ramps up (1000ms release) after; configurable via `music_duck_ratio` in EDL
- **Color grading** — subtle contrast/saturation boost, temperature shift per segment
- **YouTube chapter markers** — timestamps from EDL segment boundaries
- **Text overlays** — baked into clips during render (single FFmpeg pass, no separate overlay step)

## Debugging & iteration

- Every Gemini API call logs: model, tokens in/out, timing, response preview
- Every FFmpeg command logged at INFO level and to `output/ffmpeg_commands.log`
- Use `--force` on `prepare` or `full` to force re-analysis (bypasses cached analysis.json)
- YouTube chapter markers saved to `output/chapters_v{N}_{res_label}.txt`
- Post-assemble validation: 6 automated checks (file size, duration, streams, codec, A/V sync, resolution)
- Live progress display with per-stage status (icons: ○ pending, ⏳ running, ✅ done, ❌ failed)

## Key gotchas

- Photos sent to Gemini as individual 400px thumbnails inline. Videos as 1 concatenated mega-preview via Files API. Inline base64 limit is 100MB (~75MB raw).
- Preview generation uses `-hwaccel auto`; `-skip_frame nokey` (~22x speedup) only when keyframe interval ≤2s, otherwise full decode
- Photo thumbnails cached in `workspace/thumbnails/`, video analysis cached in `workspace/analysis_cache/`
- Preview clips cached in `workspace/preview_clips/` — orphaned files from old runs auto-cleaned
- tqdm auto-disabled when stderr is not a TTY
- Stale cache auto-invalidation: prepare re-runs if upstream file is newer (mtime check)
- FFmpeg subprocesses have a 5-minute timeout (prevents hanging on corrupt files)
- Ken Burns uses cosine easing (ease-in/ease-out); only applies to photos (videos use a separate render path)
- `--music auto` uses Gemini Lyria RealTime; `--music local` uses MusicGen — single flag controls both backend and intent
- `--lang en|cn|both` controls text language (title, overlays, chapters); cn/both auto-selects CJK font
- Clip rendering is parallel via `parallel.run_parallel()`: 3 workers for NVENC, 2 for VideoToolbox, cpu_count/2 for libx264
- HEVC auto-detected: hevc_nvenc (Win/Linux) or hevc_videotoolbox (macOS); falls back to H.264 NVENC → libx264
- ffprobe results cached per assemble run via RenderContext (dimensions + duration)
- Text overlays baked into clips via drawtext filter with drop shadow (no separate encode pass)
- Title card uses first EDL photo as blurred background (fallback: purple gradient)
- CLI `prepare` = fetch + prepare (family detection, thumbnails, EXIF, video probing); CLI `plan` = plan + generate_music (when `--music` is not `none`); CLI `assemble` = render. `full` = all stages
- `--source local --path PATH` / `--source nas -f -t` — source type is a flag, `--path` required for local, `-f`/`-t` required for NAS
- `--resolution` / `-r` is required for both `full` and `assemble` — no default. Presets: 4k60, 4k30, 2k60, 2k30, 1080p60, 1080p30, 720p30, or custom WxHxFPS
- Clips cached per resolution (`seg00_item00_1080p30.mp4`); switching resolution doesn't re-render existing clips
- Output files include resolution: `vlog_v1_1080p30.mp4` — different resolutions coexist
- `workspace --clean safe|cache|media|all` — `safe` removes old outputs + intermediates only
- RenderReport tracks per-clip status (ok/skipped/failed with reason)
