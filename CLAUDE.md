# Claude Code Notes

## Pipeline execution

Always run pipeline stages through Dagster (via `python run.py` CLI or Dagster UI), never by calling pipeline functions directly in Python. Running stages directly bypasses Dagster's run tracking and causes process management issues.

Use `dagster dev` (not just `dagster-webserver`) — the daemon is needed for the run queue coordinator.

```bash
# Correct
python run.py -n singapore full -f 2025-06-13 -t 2025-06-17 --duration 60
python run.py -n singapore resume

# Wrong — don't do this
python -c "from pipeline.analyze import analyze; analyze(cfg)"
```

## Three planner modes

```bash
# Algo: deterministic scoring, needs local vision model (llava:13b)
python run.py -n sg full --planner algo --duration 60

# API: Claude Sonnet plans from text descriptions, needs local vision model
python run.py -n sg full --planner api --duration 60

# Visual: Gemini sees actual photos, skips local vision model entirely (~2min vs ~2hr)
python run.py -n sg full --planner visual --duration 60
```

Visual mode uses Gemini 3 Flash (token-heavy visual pass) and Gemini 3 Pro (text passes). Requires `GEMINI_API_KEY` in `.env`.

## Debugging & iteration

- After submitting a Dagster run, check logs within 10-20 seconds to verify each stage works
- For analyze: check analysis.json after ~10s to see if vision results are non-null
- For visual planner: check contact_sheets/ directory for generated grid images
- If any stage shows errors, kill the run immediately and fix before retrying
- Check Dagster UI at localhost:3000 for run events and logs
- Use `--force-analyze` to force re-analysis (bypasses cached analysis.json)

## Key gotchas

- `.env` VISION_MODEL must match an installed Ollama model — mismatch causes silent 404s (only for `--planner algo/api`)
- `--planner visual` skips local vision model entirely — no Ollama needed
- Dagster `in_process_executor` is required on Windows (multiprocess gRPC is flaky)
- Ollama `keep_alive` must be set to prevent model unloading between pipeline stages
- On Windows, WinGet installs FFmpeg/Ollama to paths not on default PATH — `media_utils.py` handles this
- `scene_type` from vision can be a list or string — always handle both types
- Contact sheets must stay under 2000px in any dimension (Claude/Gemini API limit for multi-image requests)
- Dagster concurrency keys were removed — stuck CANCELING runs no longer block the queue
- tqdm is auto-disabled when stderr is not a TTY (prevents BrokenPipeError in Dagster)
