# ReelSmith Pipeline Architecture

End-to-end data flow diagram: raw media → cached artifacts → EDL → rendered output.

---

## High-Level Pipeline

```
 ┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
 │   PREPARE    │────▶│    PLAN      │────▶│    MUSIC     │────▶│  ASSEMBLE   │
 │  scan+probe  │     │  Gemini EDL  │     │  Lyria gen   │     │ FFmpeg render│
 └─────────────┘     └─────────────┘     └─────────────┘     └─────────────┘
       │                    │                    │                    │
       ▼                    ▼                    ▼                    ▼
  manifest.json        edl_v{N}.json      composite_music.wav   reelsmith_v{N}_{res}.mp4
  analysis.json        _mega_preview.mp4   per-segment .wav
  thumbnails/          offset table
  previews/
```

---

## Workspace Layout

```
workspace/
├── thumbnails/                          # SHARED — cached across all runs
│   └── {stem}_thumb.jpg                 #   400px JPEG, keyed by photo filename
│
├── previews/                            # SHARED — cached across all runs
│   ├── preview_{md5[:12]}.mp4           #   480p 1fps video preview (with audio)
│   ├── _mega_preview.mp4                #   all previews concatenated + burned-in #XX labels
│   └── _mega_preview.json               #   offset table (item# → duration, abs timestamp)
│
├── music/                               # SHARED — cached across all runs
│   ├── gemini_{type}_{style}_{dur}s_{hash}.wav   # per-segment music from Lyria
│   └── gemini_{type}_{style}_{dur}s_{hash}.json  # generation metadata
│
└── runs/
    └── {run_name}/                      # PER-RUN — isolated workspace
        ├── manifest.json                #   source file listing (scan output)
        ├── analysis.json                #   enriched metadata (prepare output)
        ├── edl_v1.json                  #   EDL version 1 (plan output)
        ├── edl_v2.json                  #   EDL version 2 (re-plan)
        ├── run_{timestamp}.log          #   full pipeline log
        ├── run_config_*.yaml            #   saved CLI parameters
        ├── render/                      #   intermediate clips
        │   ├── seg_0_{res}.mp4          #     per-segment rendered video
        │   ├── seg_1_{res}.mp4
        │   ├── intro_title_{res}.mp4    #     title card
        │   └── outro_title_{res}.mp4    #     outro card
        └── output/                      #   final deliverables
            ├── reelsmith_v{N}_{res}.mp4 #     final output video
            └── chapters_v{N}_{res}.txt  #     YouTube chapter markers
```

**Cache key strategy:**
- Thumbnails: `{original_stem}_thumb.jpg` — keyed by filename
- Previews: `preview_{md5(local_path)[:12]}.mp4` — keyed by full path hash
- Music: `gemini_{trip_type}_{style}_{duration}s_{mood_hash}.wav` — keyed by generation parameters
- Render clips: `seg_{idx}_{res_label}.mp4` — keyed by segment index + resolution (different resolutions coexist)

---

## Stage 1: Prepare

Scan source media → extract metadata → generate thumbnails & preview clips.

```
                         ┌──────────────────────┐
  Source folder          │       PREPARE         │
  (photos + videos)      │                      │
  ─────────────────────▶ │  Phase 0: Scan        │──▶ manifest.json
                         │    EXIF, GPS, dates   │       (ManifestEntry[])
                         │    reverse geocode    │
                         │                      │
                         │  Phase 1: Metadata    │──▶ analysis.json
                         │    thumbnails (photo) │       (AnalysisEntry[])
                         │    ffprobe (video)    │
                         │    EXIF extraction    │
                         │                      │
                         │  Phase 2: Previews    │──▶ previews/preview_{hash}.mp4
                         │    480p 1fps + audio  │       (parallel workers)
                         │    orphan cleanup     │
                         └──────────────────────┘

  Caching: thumbnails + previews persist across runs.
           Manifest + analysis regenerated per run.
           --force bypasses per-item cache.
```

**Data contracts produced:**

| Artifact | Schema | Consumers |
|----------|--------|-----------|
| `manifest.json` | `ManifestEntry[]` — `{taken_at, local_path, filesize?, city?, country?}` | prepare (internal) |
| `analysis.json` | `AnalysisEntry[]` — manifest fields + thumbnail_path, exif, video_* | plan stage |
| `thumbnails/{stem}_thumb.jpg` | 400px JPEG | plan (inline to Gemini) |
| `previews/preview_{hash}.mp4` | 480p 1fps, mono 64kbps AAC | plan (mega-preview) |

---

## Stage 2: Plan

Build visual content → call Gemini → postprocess into validated EDL.

```
  analysis.json ─────────────────┐
  thumbnails/ ───────────────────┤
  previews/ ─────────────────────┤
                                 ▼
                   ┌────────────────────────────┐
                   │           PLAN              │
                   │                            │
                   │  1. Burst dedup             │  Remove near-duplicate photos
                   │     (HSV histogram,         │  (cosine sim > 0.92, 10s window)
                   │      64x64 thumbs)          │
                   │              │              │
                   │              ▼              │
                   │  2. Build content blocks    │  Per-photo: metadata + inline thumbnail
                   │     + mega-preview          │  All videos: concat → _mega_preview.mp4
                   │     + offset table          │  Offset table: item# → (dur, abs_time)
                   │              │              │
                   │              ▼              │
                   │  3. Gemini API call         │  System prompt (templated)
                   │     model: flash/pro        │  + inline photos (base64, ≤75MB)
                   │                            │  + mega-preview (Files API upload)
                   │              │              │
                   │              ▼              │
                   │  4. Postprocess pipeline    │
                   │     a. parse timestamps     │  preview MM:SS → local trim seconds
                   │     b. fix hallucinated     │  fuzzy filename matching
                   │        paths               │
                   │     c. validate trims       │  clamp to [0, duration], min 2s
                   │     d. deduplicate          │  remove duplicate source_file
                   │     e. force video          │  effect="none" on all videos
                   │        effect=none          │
                   │     f. quality check        │  warn >30%, fail >50% removed
                   │              │              │
                   │              ▼              │
                   │  5. Save EDL                │──▶ edl_v{N}.json
                   └────────────────────────────┘

  Key: preview_start/preview_end (Gemini output, MM:SS in mega-preview)
       → converted to start_time/end_time (seconds in original source video)
       via offset table lookup
```

**Gemini input assembly:**

```
  ┌─ System prompt ──────────────────────────────┐
  │  visual_planner_system.md (templated)         │
  │  + narrative_guidance.json[trip_type]          │
  │  + lang_instructions.json[language]            │
  └───────────────────────────────────────────────┘

  ┌─ User content (multimodal) ──────────────────┐
  │                                               │
  │  Text:  intro (duration, count, video ratio)  │
  │         #01: Alice at=Marina Bay 50mm f/2.0   │
  │         #02: street at=Chinatown              │
  │         #03: family video=45s 1920x1080 ...   │
  │                                               │
  │  Images: photo thumbnails (inline base64)     │
  │          ≤75MB total                          │
  │                                               │
  │  Video:  _mega_preview.mp4 (Files API)        │
  │          480p 1fps, all clips, #XX labels     │
  │          WITH AUDIO — Gemini listens here     │
  └───────────────────────────────────────────────┘
```

---

## Stage 3: Music

Generate per-segment music from EDL mood descriptions, composite into single track.

```
  edl_v{N}.json ────────┐
  (segment.music_mood)  │
                        ▼
           ┌──────────────────────┐
           │        MUSIC          │
           │                      │
           │  Per segment:         │
           │    music_mood ──▶     │──▶ music/gemini_{...}_{hash}.wav
           │    Lyria RealTime API │       (cached per mood+duration)
           │                      │
           │  Composite:           │
           │    acrossfade all     │──▶ {workspace}/composite_music.wav
           │    segments (2s xfade)│
           │                      │
           │  Update EDL:          │
           │    edl.music.file =   │──▶ edl_v{N}.json (updated in place)
           │    composite path     │
           └──────────────────────┘

  Skipped if: music_mode="none" or --music /path/to/file (user-provided)
```

---

## Stage 4: Assemble

Render per-segment clips → concatenate → mix music → validate.

```
  edl_v{N}.json ──────────┐
  original media files ───┤
  composite_music.wav ────┤
                          ▼
     ┌───────────────────────────────────────────────────┐
     │                    ASSEMBLE                        │
     │                                                   │
     │  Phase 0: Beat Sync (optional, if music present)  │
     │    BPM estimation (energy envelope autocorrelation)│
     │    snap transitions to half-beat grid              │
     │    keep_audio=true items locked (skip snap)        │
     │                          │                        │
     │                          ▼                        │
     │  Phase 1: Render Segments (parallel)              │
     │    ┌─────────────────────────────────────────┐    │
     │    │  Per photo:                              │    │
     │    │    loop → split → [bg blur] + [fg KB] → │    │
     │    │    overlay + color grade + text          │    │
     │    │    audio = aevalsrc silence              │    │
     │    │                                          │    │
     │    │  Per video:                              │    │
     │    │    trim(start,end) → split →             │    │
     │    │    [bg blur] + [fg scale+speed] →        │    │
     │    │    overlay + color grade + text          │    │
     │    │    audio = atrim+atempo (keep_audio)     │    │
     │    │         or aevalsrc silence (!keep)      │    │
     │    │                                          │    │
     │    │  Title cards:                            │    │
     │    │    blurred bg photo (or gradient) +      │    │
     │    │    animated text + fade in/out           │    │
     │    │                                          │    │
     │    │  → concat all items in segment           │    │
     │    │  → encode: NVENC / VideoToolbox / CPU    │    │
     │    └─────────────────────────────────────────┘    │
     │          │                                        │
     │          ▼                                        │
     │    render/seg_0_{res}.mp4                         │
     │    render/seg_1_{res}.mp4                         │
     │    render/intro_title_{res}.mp4                   │
     │    render/outro_title_{res}.mp4                   │
     │          │                                        │
     │          ▼                                        │
     │  Phase 2: Concat + Music Mix                      │
     │    concat demuxer (no re-encode: -c copy)         │
     │    + music overlay with sidechaincompress          │
     │      (auto-duck music under speech)               │
     │    + loudnorm (two-pass)                          │
     │    + fade in/out                                  │
     │          │                                        │
     │          ▼                                        │
     │  Phase 3: Validation                              │
     │    file size, duration, streams,                   │
     │    codec, A/V sync, resolution                    │
     │          │                                        │
     │          ▼                                        │
     │    output/reelsmith_v{N}_{res}.mp4                │
     │    output/chapters_v{N}_{res}.txt                 │
     └───────────────────────────────────────────────────┘
```

**Encoding chain (auto-detection):**

```
  --codec auto (default)
  ┌──────────────────────────────────────────────┐
  │  HEVC preferred:                              │
  │    hevc_nvenc (NVIDIA) → hevc_videotoolbox    │
  │    (macOS) → libx265 (CPU fallback)           │
  │                                               │
  │  --codec av1:                                 │
  │    av1_nvenc (RTX 40+) → libsvtav1 (CPU)      │
  │                                               │
  │  --codec h264:                                │
  │    h264_nvenc → h264_videotoolbox → libx264   │
  └──────────────────────────────────────────────┘

  Bitrate = base_rate[resolution] × codec_ratio × fps_multiplier × --quality
  ┌───────────┬───────────┬──────────┬───────────┐
  │ Resolution│ H.264 base│ HEVC ×0.65│ AV1 ×0.45│
  ├───────────┼───────────┼──────────┼───────────┤
  │ 4K        │ 45 Mbps   │ 29 Mbps  │ 20 Mbps  │
  │ 2K        │ 16 Mbps   │ 10 Mbps  │  7 Mbps  │
  │ 1080p     │  8 Mbps   │  5 Mbps  │  4 Mbps  │
  │ 720p      │  5 Mbps   │  3 Mbps  │  2 Mbps  │
  └───────────┴───────────┴──────────┴───────────┘
  fps > 30 → ×1.5 bump
```

---

## Cross-Stage Data Flow

```
  SOURCE MEDIA
       │
       ▼
  ┌─ PREPARE ──────────────────────────────────────────────────────────┐
  │  photos ──▶ thumbnail_path ──────────────────▶ inline to Gemini    │
  │  photos ──▶ exif (focal, aperture, ISO) ─────▶ text metadata       │
  │  videos ──▶ ffprobe (dur, w, h, fps, orient) ▶ text metadata       │
  │  videos ──▶ preview clip (480p 1fps+audio) ──▶ mega-preview        │
  │  all ────▶ taken_at, location ───────────────▶ text metadata       │
  └────────────────────────────────────────────────────────────────────┘
       │ analysis.json
       ▼
  ┌─ PLAN ─────────────────────────────────────────────────────────────┐
  │  Gemini sees:  thumbnails (visual) + mega-preview (motion+audio)   │
  │  Gemini hears: speech/laughter/ambient in preview (ONLY listener)  │
  │  Gemini outputs: preview_start/end (MM:SS) ─▶ postprocess ─▶      │
  │                  start_time/end_time (local seconds)               │
  │  Gemini outputs: keep_audio ─────────────────▶ assemble audio path │
  │  Gemini outputs: music_mood ─────────────────▶ music generation    │
  │  Gemini outputs: effect (photos only) ───────▶ Ken Burns direction │
  │  Gemini outputs: playback_speed ─────────────▶ atempo filter       │
  │  Gemini outputs: text_overlay ───────────────▶ drawtext filter     │
  │  Gemini outputs: color_temp ─────────────────▶ eq filter           │
  └────────────────────────────────────────────────────────────────────┘
       │ edl_v{N}.json
       ▼
  ┌─ MUSIC ────────────────────────────────────────────────────────────┐
  │  segment.music_mood ──▶ Lyria prompt ──▶ per-segment WAV           │
  │  segment durations ───▶ trim + crossfade ──▶ composite_music.wav   │
  └────────────────────────────────────────────────────────────────────┘
       │ composite_music.wav (+ edl updated with music.file)
       ▼
  ┌─ ASSEMBLE ─────────────────────────────────────────────────────────┐
  │  edl.items ──▶ per-item FFmpeg filter graph ──▶ segment clips      │
  │  keep_audio ─▶ atrim+atempo (true) or silence (false)             │
  │  music ──────▶ sidechaincompress ducking ──▶ final mix             │
  │  keep_audio ─▶ beat sync skip (true = locked to speech)           │
  └────────────────────────────────────────────────────────────────────┘
       │
       ▼
  reelsmith_v{N}_{res}.mp4
```

---

## EDL: The Central Data Contract

The EDL (Edit Decision List) is the single artifact that bridges planning and rendering.

```
  EDL
  ├── title: str                          "Singapore Family Trip"
  ├── target_duration: float              180.0
  ├── trip_type: str                      "family"
  ├── style: str                          "upbeat"
  ├── language: Language                  "cn"
  ├── intro_duration: float               3.0
  ├── outro_duration: float               3.0
  ├── date_range: str                     "Mar 15-18, 2025"
  ├── music_mode: MusicMode               "auto"
  ├── music: MusicTrack | null
  │   ├── file: str                       path to composite_music.wav
  │   ├── volume: float                   0.40
  │   ├── fade_in: float                  2.0
  │   └── fade_out: float                 3.0
  │
  └── segments: Segment[]
      ├── name: str                       "Marina Bay at Dusk"
      ├── narrative_rationale: str        "Golden hour establishing shot..."
      ├── music_mood: str                 "warm acoustic, gentle"
      ├── mode: SegmentMode               "narrative" | "montage"
      ├── transition: Transition          "crossfade" | "cut"
      ├── transition_duration: float      0.4
      ├── segment_transition_duration     1.0
      ├── color_temp: ColorTemp           "warm" | "cool" | "neutral"
      │
      └── items: EditItem[]
          ├── source_file: str            absolute path to original media
          ├── media_type: MediaType       "photo" | "video"
          ├── start_time: float | null    video trim start (seconds)
          ├── end_time: float | null      video trim end (seconds)
          ├── display_duration: float     on-screen duration (seconds)
          ├── keep_audio: bool            preserve original audio?
          ├── playback_speed: float       0.5 (slow-mo) to 4.0
          ├── effect: Effect              Ken Burns direction (photos only)
          └── text_overlay: TextOverlay | null
              ├── text: str
              ├── position: OverlayPosition
              └── font_size: int
```

---

## Audio Path Decision Tree

```
  Is it a photo?
  ├── YES → aevalsrc=0 (silence for display_duration)
  │         music plays at full volume (0.40)
  │
  └── NO (video)
      │
      ├── keep_audio = false
      │   → aevalsrc=0 (silence)
      │     music plays at full volume
      │     beat sync: eligible for transition snap
      │
      └── keep_audio = true
          → atrim(start_time, end_time) + atempo(playback_speed)
            original audio preserved for ENTIRE trim window
            music auto-ducked via sidechaincompress (~15% volume)
            beat sync: SKIPPED (locked to speech timing)
```

---

## Caching & Resumability

```
  ┌─────────────────────────────────────────────────────────────┐
  │  SHARED CACHES (persist across runs, keyed by content)      │
  │                                                             │
  │  thumbnails/     photo stem → 400px JPEG                    │
  │  previews/       md5(path)[:12] → 480p preview              │
  │  music/          mood+params hash → Lyria WAV               │
  └─────────────────────────────────────────────────────────────┘

  ┌─────────────────────────────────────────────────────────────┐
  │  PER-RUN STATE (isolated per run_name)                      │
  │                                                             │
  │  manifest.json   regenerated on each prepare                │
  │  analysis.json   regenerated on each prepare                │
  │  edl_v{N}.json   versioned — each plan run increments N     │
  │  render/         keyed by resolution — coexist across -r    │
  │  output/         keyed by version + resolution              │
  └─────────────────────────────────────────────────────────────┘

  Re-run behavior:
    reelsmith full    → skips cached thumbnails/previews, re-plans, re-renders
    reelsmith plan    → reuses prepare artifacts, new EDL version
    reelsmith assemble → reuses EDL + clips at same resolution
    --force           → bypasses all caches (full regeneration)
```
