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
