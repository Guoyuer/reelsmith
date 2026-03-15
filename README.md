# vlog

Automated vlog generation from Synology Photos. Downloads trip photos, analyzes them with local AI, plans a narrative, and renders a highlight reel — all locally. Orchestrated by [Dagster](https://dagster.io) with a web UI for monitoring, resume, and re-materialization.

## Architecture

```mermaid
flowchart LR
    subgraph Orchestration
        direction TB
        DG[Dagster UI<br/>localhost:3000]
        CLI[CLI<br/>python run.py]
        DG & CLI -->|submit to<br/>webserver| MAT{Materialize}
    end

    subgraph "Dagster Assets (auto-skip if output exists)"
        direction LR
        FM[fetch_media] --> PP[preprocess]
        PP --> AN[analyze]
        PP --> PL[plan]
        AN --> PL
        PL --> AS[assemble]
    end

    subgraph "Jobs"
        IT[iterate<br/>self-critique /<br/>feedback /<br/>variations]
    end

    MAT --> FM
    AS -.-> IT -.->|mutates| PL

    style FM fill:#42A5F5,color:#fff
    style PP fill:#66BB6A,color:#fff
    style AN fill:#FFA726,color:#fff
    style PL fill:#AB47BC,color:#fff
    style AS fill:#EF5350,color:#fff
```

## Quick Start

```bash
# Start all services (Ollama, Synology API, Dagster)
./start.sh

# Open Dagster UI
open http://localhost:3000

# Run pipeline from CLI (also visible in UI)
python run.py -n singapore auto -f 2025-06-13 -t 2025-06-17 --style upbeat --duration 180 --item-types 0

# Stop all services
./start.sh stop
```

## Workspace Structure

```
workspace/
  media/                          ← shared: raw photos/videos (downloaded once)
  analysis_cache/                 ← shared: per-file vision results ({item_id}.json)
  keyframes/                      ← shared: extracted video keyframes
  runs/
    singapore/                    ← per-run: isolated pipeline outputs
      manifest.json
      preprocessed.json
      analysis.json               ← assembled from shared cache
      edl.json
      clips/
      output/vlog_v1.mp4
    singapore-cinematic/          ← another run, same source data
      edl.json
      clips/
      output/vlog_v1.mp4
```

Media files and analysis results are shared across runs. A second run for the same trip (or overlapping dates) reuses all downloads and vision results — only plan + assemble re-run.

## Usage

### Web UI (Dagster)

After `./start.sh`, open **http://localhost:3000**.

**Run the full pipeline:** Jobs → full_pipeline → Launchpad → paste config:

```yaml
ops:
  fetch_media:
    config:
      from_date: "2025-06-13"
      to_date: "2025-06-17"
      item_types: [0]
  plan:
    config:
      style: upbeat
      target_duration: 180
      focus: happiness with family
resources:
  io_manager:
    config:
      run_name: singapore
```

**Resume:** Materialize All with defaults — auto-skips stages with existing outputs.

**Force re-run a stage:** Click any asset → Materialize → set `force: true` in config.

**Monitor progress:** Run event log shows per-item status for every stage, with ETA for analyze and assemble.

**View results:** Click any asset → Metadata tab shows tier tables, top-scored photos, segment summaries.

### CLI

All CLI commands submit to the Dagster webserver — runs appear in the UI.

```bash
# Resume from where you stopped (auto-skips completed stages)
python run.py -n singapore resume

# Full pipeline from scratch
python run.py -n singapore auto -f 2025-06-13 -t 2025-06-17 --style upbeat --duration 180 --item-types 0

# Force re-plan with different style (cascades to re-assemble)
python run.py -n singapore plan --style cinematic --duration 120

# Re-assemble only
python run.py -n singapore assemble

# Self-critique loop
python run.py -n singapore iterate --rounds 2

# Human feedback
python run.py -n singapore iterate --feedback "more family shots at the beach"

# Style variations
python run.py -n singapore variations

# Parallel runs (different trips or styles, isolated workspaces)
python run.py -n tokyo auto -f 2025-07-01 -t 2025-07-05 --style cinematic
```

### Stop / Resume

Stop anytime (Ctrl+C in CLI, or Terminate in UI). Resume with:

```bash
python run.py -n singapore resume    # CLI
# or Materialize All in UI           # UI
```

Each stage checks if its output exists and skips. Within analyze, per-file results are cached — even a killed analysis resumes from the last item.

## Pipeline Stages

```mermaid
flowchart TD
    subgraph "Stage 1: fetch_media"
        F1[Synology Photos API<br/>localhost:8000] -->|POST /api/collect| F2[Query by date range<br/>+ optional filters]
        F2 -->|GET /api/media/id| F3[Download to<br/>workspace/media/]
        F3 --> F4[manifest.json]
    end

    subgraph "Stage 2: preprocess"
        F4 --> P1[Count family members<br/>per photo from Synology<br/>face detection]
        P1 --> P2{Assign tiers}
        P2 -->|"2+ family"| PA[Tier A — emotional core]
        P2 -->|"1 family"| PB[Tier B — supporting]
        P2 -->|"0 people + location"| PC[Tier C — B-roll]
        P2 -->|"screenshots/junk"| PD[Tier D — skip]
        PA & PB & PC --> P3[Cluster near-duplicates<br/>within 10s window]
        P3 --> P4[Build timeline<br/>day → time_block → location]
        P4 --> P5[preprocessed.json]
    end

    subgraph "Stage 3: analyze"
        P5 --> A1{Route by tier}
        A1 -->|Tier A+B| A2[Ollama llava:7b<br/>family-tuned prompt<br/>togetherness · emotion<br/>story_beat · visual_quality]
        A1 -->|Tier C| A3[Ollama llava:7b<br/>scene-only prompt<br/>scene_type · visual_quality]
        A1 -->|Tier D| A4[Skip]
        A2 & A3 --> A5[analysis.json<br/>+ per-file cache]
    end

    subgraph "Stage 4: plan"
        P5 & A5 --> PL1[Build structured prompt<br/>chapters with scored candidates]
        PL1 --> PL2[Ollama qwen2.5-coder:7b<br/>select best items per chapter<br/>arrange into narrative]
        PL2 --> PL3[edl.json<br/>Edit Decision List]
    end

    subgraph "Stage 5: assemble"
        PL3 --> AS0{Portrait?}
        AS0 -->|yes| AS0a[Blurred BG + sharp overlay<br/>+ gentle Ken Burns]
        AS0 -->|no| AS0b[Ken Burns zoompan<br/>or scale + pad]
        AS0a & AS0b --> AS1[Render each item as clip]
        AS1 --> AS2[Concatenate with xfade<br/>transitions between clips]
        AS2 --> AS3{Music?}
        AS3 -->|yes| AS4[Mix background track<br/>volume + fade in/out]
        AS3 -->|no| AS5[vlog_v1.mp4<br/>4K 60fps]
        AS4 --> AS5
    end

    subgraph "iterate (job)"
        AS5 --> IT1[Extract 8 frames → vision critique → revised EDL → re-render]
        AS5 --> IT5[Human feedback → LLM revises EDL → re-render]
        AS5 --> IT8[Generate variations: energetic / reflective / cinematic]
    end

    style PA fill:#4CAF50,color:#fff
    style PB fill:#8BC34A,color:#fff
    style PC fill:#FFC107,color:#000
    style PD fill:#9E9E9E,color:#fff
```

## Requirements

- **Python 3.11+** with venv
- **FFmpeg** — video processing (`brew install ffmpeg`)
- **Ollama** — local LLM (`brew install ollama`)
  - `ollama pull llava:7b` — vision analysis
  - `ollama pull qwen2.5-coder:7b` — narrative planning
- **Synology Photos API** — the [synology-photos-project](../synology-photos-project) backend running on `:8000`
- **Dagster** — workflow orchestration (installed automatically via `pip install -e .`)

All services start with `./start.sh` (Ollama, Synology API, Dagster).

### 24GB MacBook constraints

| Component | Model | RAM |
|-----------|-------|-----|
| Vision | llava:7b | ~5GB |
| Planning | qwen2.5-coder:7b | ~5GB |
| Whisper | mlx-whisper medium | ~1.5GB |

Only one Ollama model loaded at a time. Fits comfortably in 24GB. Dagster uses `concurrency_key` to prevent Ollama contention between concurrent runs.

## Key design decisions

- **Dagster asset model** — each stage is a Dagster asset that produces a file. Auto-skips when output exists. Re-materialize from the UI to force re-run + downstream cascade.
- **CLI and UI are unified** — CLI submits runs to the Dagster webserver via GraphQL. All runs appear in the UI with full logs, metadata, and progress.
- **Shared media + analysis cache** — raw files and per-file vision results are shared across runs. A second run for the same trip reuses all downloads and analysis. Only plan + assemble re-run.
- **Per-run isolation** — each run gets its own directory (`workspace/runs/{name}/`) for manifest, EDL, clips, and output. Different styles or date ranges don't interfere.
- **Interruptible everything** — all subprocess calls (FFmpeg, sips) use `Popen` with signal forwarding. All Ollama calls use streaming. Dagster's terminate button kills within seconds.
- **EDL is the central artifact** — a JSON file that flows between plan/assemble/iterate. Changing the edit never re-analyzes media.
- **Synology metadata > AI scoring** — person face tags and GPS locations from Synology are more reliable than a 7B vision model's judgment. AI fills in what metadata can't (emotion, composition, scene type).
- **Tiered analysis** — family-together photos (tier A) get a detailed prompt; scene shots (tier C) get a minimal one. Cuts GPU time ~40%.
- **Portrait-aware rendering** — portrait photos/videos get blurred background + sharp foreground overlay instead of black bars or awkward center crops.
- **HEIC via sips** — Apple HEIC photos are converted using macOS `sips` (not FFmpeg, which decodes HEIC grid tiles as 512x512 thumbnails).

## Testing

```bash
source venv/bin/activate
python -m pytest tests/ -v                    # all 107 tests (~2s)
python -m pytest tests/ -m integration -v     # integration tests only (requires FFmpeg)
python -m pytest tests/ -m "not integration"  # unit/mocked tests only
```
