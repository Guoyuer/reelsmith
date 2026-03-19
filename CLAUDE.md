# Claude Code Notes

## Pipeline execution

Always run pipeline stages through Dagster (via `python run.py` CLI or Dagster UI), never by calling pipeline functions directly in Python. Running stages directly bypasses Dagster's run tracking and causes process management issues.

Use `dagster dev` (not just `dagster-webserver`) — the daemon is needed for the run queue coordinator.

```bash
# Correct
python run.py -n singapore full -f 2025-06-13 -t 2025-06-17 --duration 180
python run.py -n singapore resume

# Wrong — don't do this
python -c "from pipeline.analyze import analyze; analyze(cfg)"
```

## Pipeline stages

`fetch_media → preprocess → analyze → plan → generate_music → assemble`

All stages use Gemini API (no local AI models needed). Requires `GEMINI_API_KEY` in `.env`.

## Gemini API calls in the plan stage

| # | Call | Input | What it does |
|---|------|-------|-------------|
| 1 | Pass 1: Arc | Text metadata only | Design narrative structure and chapter themes |
| 2 | Pass 2: Select | Contact sheets + filmstrips + metadata | See ALL photos/videos, pick items, assign music_mood |
| 3 | Pass 3: Review | Selected items at 768px | Refine pacing, check video/photo balance |

Total cost: ~$0.05 per run on Gemini 3 Flash.

Every API call is logged with: model, input token count, output tokens, wall time, response preview.

## What Gemini controls (e2e)

- **Photo/video selection** — Gemini sees actual photos via contact sheets, judges visually
- **Chapter structure** — narrative chapters by story beat, not location/time buckets
- **Video trim points** — `start_time`/`end_time` for selecting best moments from video clips
- **Music mood** — per-segment descriptions fed to Lyria RealTime prompt
- **Text overlays** — evocative titles, not just "Day 1 - Marina Bay"
- **Pacing** — display_duration per item, effect choices

## What's still hard-coded

- **Preprocess tiering** — face count → A/B/C/D (but Gemini ignores tiers, judges visually)
- **FFmpeg rendering** — mechanical clip assembly from EDL
- **Ken Burns effects** — applied per EDL effect field (forced to "none" for videos)
- **Thumbnail/keyframe generation** — Pillow resize, FFmpeg extraction
- **Bitrate** — based on resolution/fps with `--quality` multiplier

## Debugging & iteration

- After submitting a Dagster run, check logs within 10-20 seconds to verify each stage works
- Check `contact_sheets/` directory for generated grid images sent to Gemini
- Every Gemini API call logs: model, tokens in/out, timing, response preview
- Use `--force-analyze` to force re-analysis (bypasses cached analysis.json)
- Check Dagster UI at localhost:3000 for run events and logs

## Key gotchas

- Contact sheets must stay under 2000px in any dimension (Gemini API limit for multi-image requests) — large chapters auto-split into multiple sheets
- Video keyframes extracted in single FFmpeg pass (fps filter) — cached in `workspace/keyframes/`
- Photo thumbnails cached in `workspace/thumbnails/`, video analysis cached in `workspace/analysis_cache/`
- Dagster: use `dagster dev` not `dagster-webserver` (daemon needed for run queue)
- tqdm auto-disabled when stderr is not a TTY (prevents BrokenPipeError in Dagster)
- Stale cache auto-invalidation: preprocess/analyze re-run if upstream file is newer (mtime check)
