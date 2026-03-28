# AI Agent Loop for FFmpeg Feature Exploration

## How to run

Use Claude Code's `/loop` command to run the agent iteratively:

```bash
# Run every 30 minutes, iterating on FFmpeg features
/loop 30m "Run the next FFmpeg feature experiment"
```

Or use `/schedule` for a cron-based approach:

```bash
/schedule create --cron "0 */2 * * *" --prompt "Run the next FFmpeg feature experiment from benchmark/experiments_queue.json"
```

## How the loop works

Each iteration, the AI agent:

### 1. Read current state
- `benchmark/experiments.jsonl` — history of all past experiments
- `benchmark/experiments_queue.json` — pending experiments to try
- `benchmark/results/` — detailed per-run JSON results

### 2. Pick next experiment
Based on the queue, or generate a new one by:
- Reading FFmpeg changelog for untried features
- Checking which pipeline areas haven't been optimized
- Prioritizing based on past experiment results (what worked)

### 3. Implement the change
- Create a git branch: `experiment/{feature-name}`
- Modify the relevant pipeline code (e.g., `_encoder.py`, `_graph.py`)
- Keep changes minimal and isolated

### 4. Run benchmark
```python
from benchmark.experiment import Experiment, run_experiment

exp = Experiment(
    name="vulkan-hevc-encoder",
    description="Add Vulkan HEVC as fallback between NVENC and libx264",
    edl_path=Path("workspace/runs/test/edl_v1.json"),
    resolution="1080p30",
    branch="experiment/vulkan-hevc",
)
report = run_experiment(exp)
```

### 5. Evaluate and decide
- **Accept**: merge branch, update queue, move to next
- **Reject**: log reason, revert branch, try variant or next item
- **Inconclusive**: add to retry queue with parameter variations

### 6. Update knowledge base
Append to `experiments.jsonl` with structured results for future reference.

## Experiment queue format

`benchmark/experiments_queue.json`:
```json
[
  {
    "name": "vulkan-hevc-fallback",
    "description": "Add Vulkan HEVC encoding as fallback for AMD/Intel GPUs",
    "target_files": ["pipeline/assemble/_encoder.py"],
    "ffmpeg_feature": "hevc_vulkan encoder",
    "min_ffmpeg": "7.1",
    "priority": "high",
    "status": "pending"
  },
  {
    "name": "whisper-speech-detection",
    "description": "Use FFmpeg whisper filter in prepare stage for local speech timestamps",
    "target_files": ["pipeline/prepare/_prepare.py", "pipeline/_types.py"],
    "ffmpeg_feature": "af_whisper filter",
    "min_ffmpeg": "8.0",
    "priority": "high",
    "status": "pending"
  },
  {
    "name": "av1-vulkan-encoder",
    "description": "Add AV1 Vulkan encoding option for smaller files",
    "target_files": ["pipeline/assemble/_encoder.py"],
    "ffmpeg_feature": "av1_vulkan encoder",
    "min_ffmpeg": "8.0",
    "priority": "medium",
    "status": "pending"
  }
]
```

## Key principles

1. **One change at a time** — each experiment isolates exactly one variable
2. **Always compare against baseline** — same EDL, same media, same resolution
3. **Quality gate** — never accept a speed improvement that drops VMAF > 1.0
4. **Accumulate knowledge** — experiments.jsonl is the memory across iterations
5. **Fail fast** — if FFmpeg doesn't have the feature, skip immediately
6. **Reproducible** — git branches + fixed test data = anyone can re-run
