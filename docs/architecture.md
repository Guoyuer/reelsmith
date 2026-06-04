# ReelSmith Pipeline Architecture

End-to-end data flow: raw media → cached artifacts → EDL → rendered output.

---

## High-Level Pipeline

```mermaid
flowchart LR
    P[Prepare<br/>scan + probe] --> PL[Plan<br/>Gemini EDL]
    PL --> M[Music<br/>Lyria gen]
    M --> A[Assemble<br/>FFmpeg render]

    P -.- p_out["manifest.json<br/>analysis.json<br/>thumbnails/<br/>previews/"]
    PL -.- pl_out["edl_v{N}.json<br/>_mega_preview.mp4"]
    M -.- m_out["composite_music.wav"]
    A -.- a_out["reelsmith_v{N}_{res}.mp4"]

    style P fill:#4a9eff,color:#fff
    style PL fill:#7c5cff,color:#fff
    style M fill:#e85d9a,color:#fff
    style A fill:#2dba4e,color:#fff
```

---

## Workspace Layout

```
workspace/
├── thumbnails/                          # SHARED — cached across all runs
│   └── {stem}_{path_hash}_thumb.jpg     #   400px JPEG, keyed by source path
│
├── previews/                            # SHARED — cached across all runs
│   ├── preview_{md5[:12]}.mp4           #   480p 1fps video preview (with audio)
│   ├── _mega_preview.mp4                #   all previews concatenated + #XX labels
│   └── _mega_preview.json               #   offset table (item# → duration, abs timestamp)
│
├── music/                               # SHARED — cached across all runs
│   ├── gemini_{type}_{style}_{dur}s_{hash}.wav   # per-segment Lyria music
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
        │   ├── intro_title_{res}.mp4    #     title card
        │   └── outro_title_{res}.mp4    #     outro card
        └── output/                      #   final deliverables
            ├── reelsmith_v{N}_{res}.mp4 #     final output video
            └── chapters_v{N}_{res}.txt  #     YouTube chapter markers
```

**Cache key strategy:**

| Cache | Key | Example |
|-------|-----|---------|
| Thumbnails | original stem + path hash | `IMG_0123_a1b2c3d4e5f6_thumb.jpg` |
| Previews | `md5(local_path)[:12]` | `preview_a1b2c3d4e5f6.mp4` |
| Music | mood + generation params hash | `gemini_family_upbeat_180s_f7e8d9.wav` |
| Render clips | segment index + resolution | `seg_0_1080p30.mp4` |

---

## Stage 1: Prepare

Scan source media → extract metadata → generate thumbnails & preview clips.

```mermaid
flowchart TD
    SRC["Source folder<br/>(photos + videos)"] --> SCAN["Phase 0: Scan<br/>EXIF, GPS, dates<br/>reverse geocode"]
    SCAN --> MAN["manifest.json<br/>(ManifestEntry[])"]

    MAN --> META["Phase 1: Metadata<br/>thumbnails (photo)<br/>ffprobe (video)<br/>EXIF extraction"]
    META --> ANA["analysis.json<br/>(AnalysisEntry[])"]
    META --> THM["thumbnails/{stem}_{path_hash}_thumb.jpg<br/>400px JPEG"]

    MAN --> PREV["Phase 2: Previews<br/>480p 1fps + audio<br/>parallel workers"]
    PREV --> PRV["previews/preview_{hash}.mp4"]

    style SRC fill:#f5a623,color:#fff
    style MAN fill:#4a9eff,color:#fff
    style ANA fill:#4a9eff,color:#fff
    style THM fill:#7c5cff,color:#fff
    style PRV fill:#7c5cff,color:#fff
```

**Data contracts produced:**

| Artifact | Schema | Consumers |
|----------|--------|-----------|
| `manifest.json` | `ManifestEntry[]` — `{taken_at, local_path, filesize?, city?, country?}` | prepare (internal) |
| `analysis.json` | `AnalysisEntry[]` — manifest + thumbnail_path, exif, video_* | plan stage |
| `thumbnails/{stem}_{path_hash}_thumb.jpg` | 400px JPEG | plan (inline to Gemini via `thumbnail_path`) |
| `previews/preview_{hash}.mp4` | 480p 1fps, mono 64kbps AAC | plan (mega-preview) |

---

## Stage 2: Plan

Build visual content → call Gemini → postprocess into validated EDL.

```mermaid
flowchart TD
    ANA["analysis.json"] --> DEDUP["1. Burst dedup<br/>HSV histogram, cosine sim > 0.92"]
    THM["thumbnails/"] --> BUILD
    PRV["previews/"] --> BUILD

    DEDUP --> BUILD["2. Build content blocks<br/>+ mega-preview concat<br/>+ offset table"]

    BUILD --> GEMINI["3. Gemini API call<br/>system prompt + inline photos<br/>+ mega-preview (Files API)"]

    GEMINI --> POST["4. Postprocess pipeline"]

    POST --> PP1["a. parse timestamps<br/>preview MM:SS → local seconds"]
    PP1 --> PP2["b. fix hallucinated paths<br/>fuzzy filename matching"]
    PP2 --> PP3["c. validate trims<br/>clamp to duration, min 2s"]
    PP3 --> PP4["d. deduplicate<br/>+ force video effect=none"]
    PP4 --> PP5["e. quality check<br/>warn >30%, fail >50% removed"]

    PP5 --> EDL["edl_v{N}.json"]

    style GEMINI fill:#7c5cff,color:#fff
    style EDL fill:#2dba4e,color:#fff
```

**Gemini input assembly:**

```mermaid
flowchart LR
    subgraph SYS["System prompt"]
        SP["visual_planner_system.md<br/>(templated)"]
        NG["narrative_guidance.json<br/>[trip_type]"]
        LI["lang_instructions.json<br/>[language]"]
    end

    subgraph USR["User content (multimodal)"]
        TXT["Text metadata<br/>#01: Alice at=Marina Bay 50mm f/2.0<br/>#02: street at=Chinatown<br/>#03: family video=45s 1920x1080"]
        IMG["Photo thumbnails<br/>inline base64 ≤75MB"]
        VID["_mega_preview.mp4<br/>Files API upload<br/>480p 1fps WITH AUDIO"]
    end

    SYS --> CALL["Gemini API"]
    USR --> CALL
    CALL --> JSON["JSON EDL response"]

    style CALL fill:#7c5cff,color:#fff
    style VID fill:#e85d9a,color:#fff
```

**Key timestamp conversion:**

preview_start/preview_end (MM:SS in mega-preview, Gemini output)
→ offset table lookup →
start_time/end_time (seconds in original source video)

---

## Stage 3: Music

Generate per-segment music from EDL mood descriptions, composite into single track.

```mermaid
flowchart TD
    EDL["edl_v{N}.json<br/>segment.music_mood"] --> GEN

    subgraph GEN["Per-segment generation"]
        S1["seg 0: 'warm acoustic'"] --> L1["Lyria RealTime API"]
        S2["seg 1: 'uplifting energy'"] --> L2["Lyria RealTime API"]
        S3["seg 2: 'gentle closing'"] --> L3["Lyria RealTime API"]
    end

    L1 --> W1["music/gemini_..._hash1.wav"]
    L2 --> W2["music/gemini_..._hash2.wav"]
    L3 --> W3["music/gemini_..._hash3.wav"]

    W1 --> COMP["Composite<br/>acrossfade (2s crossfade)"]
    W2 --> COMP
    W3 --> COMP

    COMP --> OUT["composite_music.wav<br/>+ update edl.music.file"]

    style GEN fill:#e85d9a,color:#fff
    style OUT fill:#2dba4e,color:#fff
```

Skipped if `music_mode="none"` or `--music /path/to/track.mp3` (user-provided).

---

## Stage 4: Assemble

Render per-segment clips → concatenate → mix music → validate.

```mermaid
flowchart TD
    EDL["edl_v{N}.json"] --> BEAT
    MEDIA["Original media files"] --> RENDER
    MUSIC["composite_music.wav"] --> BEAT

    BEAT["Phase 0: Beat Sync<br/>BPM estimation → half-beat grid<br/>snap transitions (skip keep_audio items)"]
    BEAT --> RENDER

    subgraph RENDER["Phase 1: Render Segments (parallel)"]
        direction TB
        PHOTO["Photo pipeline<br/>loop → split → bg blur + fg Ken Burns<br/>→ overlay + color grade + text<br/>audio = silence"]
        VIDEO["Video pipeline<br/>trim → split → bg blur + fg scale+speed<br/>→ overlay + color grade + text<br/>audio = atrim+atempo or silence"]
        TITLE["Title cards<br/>blurred bg + animated text<br/>+ fade in/out"]
    end

    RENDER --> CLIPS["render/seg_0_{res}.mp4<br/>render/seg_1_{res}.mp4<br/>render/intro_title_{res}.mp4"]

    CLIPS --> CONCAT["Phase 2: Concat + Music Mix<br/>concat demuxer (no re-encode)<br/>+ sidechaincompress ducking<br/>+ loudnorm (two-pass)"]

    CONCAT --> VALID["Phase 3: Validation<br/>file size, duration, streams<br/>codec, A/V sync, resolution"]

    VALID --> FINAL["output/reelsmith_v{N}_{res}.mp4<br/>output/chapters_v{N}_{res}.txt"]

    style RENDER fill:#2dba4e,color:#fff
    style FINAL fill:#f5a623,color:#fff
```

**Encoding chain (auto-detection):**

```mermaid
flowchart LR
    AUTO["--codec auto<br/>(default)"] --> HEVC_HW{"hevc_nvenc<br/>or hevc_vtb?"}
    HEVC_HW -->|yes| USE_HW["Use hardware HEVC"]
    HEVC_HW -->|no| LIBX265["libx265 (CPU)"]

    AV1["--codec av1"] --> AV1_HW{"av1_nvenc<br/>(RTX 40+)?"}
    AV1_HW -->|yes| USE_AV1["Use hardware AV1"]
    AV1_HW -->|no| SVT["libsvtav1 (CPU)"]

    H264["--codec h264"] --> H264_HW{"h264_nvenc<br/>or h264_vtb?"}
    H264_HW -->|yes| USE_264["Use hardware H.264"]
    H264_HW -->|no| LIBX264["libx264 (CPU)"]
```

**Bitrate calculation:** `base_rate[resolution] × codec_ratio × fps_multiplier × --quality`

| Resolution | H.264 base | HEVC (×0.65) | AV1 (×0.45) |
|------------|-----------|-------------|-------------|
| 4K | 45 Mbps | 29 Mbps | 20 Mbps |
| 2K | 16 Mbps | 10 Mbps | 7 Mbps |
| 1080p | 8 Mbps | 5 Mbps | 4 Mbps |
| 720p | 5 Mbps | 3 Mbps | 2 Mbps |

fps > 30 → ×1.5 bump

---

## Cross-Stage Data Flow

```mermaid
flowchart TB
    SRC["Source Media"] --> PREPARE

    subgraph PREPARE["Prepare"]
        P1["photos → thumbnail_path → inline to Gemini"]
        P2["photos → exif (focal, aperture, ISO) → text metadata"]
        P3["videos → ffprobe (dur, w, h, fps, orient) → text metadata"]
        P4["videos → preview clip (480p 1fps + audio) → mega-preview"]
        P5["all → taken_at, location → text metadata"]
    end

    PREPARE -->|"analysis.json"| PLAN

    subgraph PLAN["Plan"]
        G1["Gemini sees: thumbnails (visual) + mega-preview (motion+audio)"]
        G2["Gemini hears: speech/laughter/ambient — ONLY listener in pipeline"]
        G3["Gemini outputs →"]
        G3 --> O1["keep_audio → assemble audio path"]
        G3 --> O2["music_mood → music generation"]
        G3 --> O3["effect (photos) → Ken Burns direction"]
        G3 --> O4["playback_speed → atempo filter"]
        G3 --> O5["text_overlay → drawtext filter"]
        G3 --> O6["color_temp → eq filter"]
    end

    PLAN -->|"edl_v{N}.json"| MUSIC_S

    subgraph MUSIC_S["Music"]
        M1["segment.music_mood → Lyria → per-segment WAV → composite"]
    end

    MUSIC_S -->|"composite_music.wav"| ASSEMBLE

    subgraph ASSEMBLE["Assemble"]
        A1["edl.items → per-item FFmpeg filter graph → segment clips"]
        A2["keep_audio → atrim+atempo (true) or silence (false)"]
        A3["music → sidechaincompress ducking → final mix"]
        A4["keep_audio → beat sync skip (true = locked to speech)"]
    end

    ASSEMBLE --> FINAL["reelsmith_v{N}_{res}.mp4"]

    style PREPARE fill:#4a9eff,color:#fff
    style PLAN fill:#7c5cff,color:#fff
    style MUSIC_S fill:#e85d9a,color:#fff
    style ASSEMBLE fill:#2dba4e,color:#fff
    style FINAL fill:#f5a623,color:#fff
```

---

## EDL: The Central Data Contract

The EDL (Edit Decision List) is the single artifact that bridges planning and rendering.

```mermaid
classDiagram
    class EDL {
        title: str
        target_duration: float
        trip_type: str
        style: str
        language: Language
        intro_duration: float
        outro_duration: float
        date_range: str
        music_mode: MusicMode
        music: MusicTrack?
        segments: Segment[]
    }

    class Segment {
        name: str
        narrative_rationale: str
        music_mood: str
        mode: narrative | montage
        transition: crossfade | cut
        transition_duration: float
        segment_transition_duration: float
        color_temp: warm | cool | neutral
        items: EditItem[]
    }

    class EditItem {
        source_file: str
        media_type: photo | video
        start_time: float?
        end_time: float?
        display_duration: float
        keep_audio: bool
        playback_speed: float
        effect: Effect
        text_overlay: TextOverlay?
    }

    class MusicTrack {
        file: str
        volume: float
        fade_in: float
        fade_out: float
    }

    class TextOverlay {
        text: str
        position: top | center | bottom
        font_size: int
    }

    EDL "1" --> "*" Segment
    EDL "1" --> "0..1" MusicTrack
    Segment "1" --> "*" EditItem
    EditItem "1" --> "0..1" TextOverlay
```

---

## Audio Path Decision Tree

```mermaid
flowchart TD
    Q1{"Photo or Video?"}
    Q1 -->|Photo| SILENCE["aevalsrc = 0 (silence)<br/>music at full volume (0.40)<br/>beat sync: eligible"]

    Q1 -->|Video| Q2{"keep_audio?"}

    Q2 -->|false| VSILENCE["aevalsrc = 0 (silence)<br/>music at full volume<br/>beat sync: eligible"]

    Q2 -->|true| VAUDIO["atrim(start, end) + atempo(speed)<br/>original audio preserved<br/>music ducked to ~15%<br/>beat sync: SKIPPED (locked)"]

    style SILENCE fill:#4a9eff,color:#fff
    style VSILENCE fill:#4a9eff,color:#fff
    style VAUDIO fill:#e85d9a,color:#fff
```

---

## Caching & Resumability

```mermaid
flowchart LR
    subgraph SHARED["Shared Caches (persist across runs)"]
        T["thumbnails/<br/>photo stem → 400px JPEG"]
        PR["previews/<br/>md5(path)[:12] → 480p preview"]
        MU["music/<br/>mood+params hash → Lyria WAV"]
    end

    subgraph PERRUN["Per-Run State (isolated per run_name)"]
        MA["manifest.json<br/>regenerated each prepare"]
        AN["analysis.json<br/>regenerated each prepare"]
        ED["edl_v{N}.json<br/>versioned, N++ per plan"]
        RE["render/<br/>keyed by resolution, coexist"]
        OU["output/<br/>keyed by version + resolution"]
    end

    style SHARED fill:#7c5cff,color:#fff
    style PERRUN fill:#4a9eff,color:#fff
```

**Re-run behavior:**

| Command | Reuses | Regenerates |
|---------|--------|-------------|
| `reelsmith full` | cached thumbnails + previews | manifest, analysis, EDL, render, output |
| `reelsmith plan` | all prepare artifacts | new EDL version |
| `reelsmith assemble` | EDL + clips at same resolution | output |
| `--force` | nothing | full regeneration |
