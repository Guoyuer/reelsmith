# Claude Code Notes

## Pipeline execution

Always run pipeline stages through Dagster (via `python run.py` CLI or Dagster UI), never by calling pipeline functions directly in Python. Running stages directly bypasses Dagster's run tracking and causes process management issues.

```bash
# Correct
python run.py -n singapore full -f 2025-06-13 -t 2025-06-17 --duration 60
python run.py -n singapore resume

# Wrong — don't do this
python -c "from pipeline.analyze import analyze; analyze(cfg)"
```

## Debugging & iteration

- After submitting a Dagster run, check logs within 10-20 seconds to verify each stage works
- For analyze: check analysis.json after ~10s to see if vision results are non-null
- If any stage shows errors, kill the run immediately and fix before retrying
- Check `.dagster_home/dagster_dev.log` for detailed errors

## Key gotchas

- `.env` VISION_MODEL must match an installed Ollama model — mismatch causes silent 404s
- Dagster `in_process_executor` is required on Windows (multiprocess gRPC is flaky)
- Ollama `keep_alive` must be set to prevent model unloading between pipeline stages
- On Windows, WinGet installs FFmpeg/Ollama to paths not on default PATH — `media_utils.py` handles this
- `scene_type` from vision can be a list or string — always handle both types
