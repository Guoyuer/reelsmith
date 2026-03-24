# Single FFmpeg Render — Implementation Plan

**Goal:** Replace the 77-call multi-phase assemble pipeline with a single FFmpeg invocation using `filter_complex_script`. Zero intermediate files, zero PTS sync issues, drastically simpler architecture.

**Architecture:** A Python function reads the EDL and generates a filter graph script file. One FFmpeg call reads all source files, applies per-item filters (Ken Burns, trim, speed, color grade, text, fades), concatenates via concat filter, mixes speech + music audio, and outputs the final video.

---

## What gets deleted

- `_render.py` — render_photo, render_video (filter chains move to graph builder)
- `_concat.py` — entire module (concat filter is in the graph)
- `_timeline.py` — entire module (offsets computed during graph generation)
- `_audio.py:build_speech_track` — speech is mixed in the graph
- `_audio.py:add_music` / `mix_final_audio` — music mix is in the graph
- `_assemble.py:_render_clips` — no more parallel clip rendering
- `_assemble.py:_concat_and_mix` — replaced by single FFmpeg call
- Clip caching logic — no intermediate clip files
- Timeline scaling hack — no PTS mismatch possible
- RenderReport/ClipStatus — single FFmpeg either succeeds or fails

## What stays

- `_encoder.py` — encoder detection, bitrate calculation, ffprobe
- `_filters.py` — filter string builders (zoompan, color_grade, drawtext, portrait)
- `_audio.py:estimate_bpm`, `beat_snap_edl`, `write_chapters` — pre-render EDL processing
- `render_title_card` — title card still needs its own render (generated content, no source file to reference in the graph)
- Validation (`_validate_output`)

## New modules

- `_graph.py` — filter graph builder: reads EDL + source files → writes filter_complex_script

---

## Tasks

### Task 1: Create _graph.py — filter graph builder

The core of the new architecture. Generates a filter_complex_script file from EDL.

For each EDL item, generates:
- **Photo:** `[N:v] scale=...,zoompan=...,eq=...,unsharp=...[,drawtext=...],fade=...[vN]`
- **Video:** `[N:v] scale=...,eq=...[,setpts=...][,drawtext=...],fade=...[vN]`
- **Video audio (keep_audio):** `[N:a] [atempo=...],adelay=OFFSET[aN]`

Then:
- `[v0][v1]...[vN] concat=n=N:v=1:a=0[vout]`
- Speech: `[a0][a1]...[aM] amix=inputs=M[speech]`
- Music: `[music_input] volume='ducking_expr'[bg]`
- Final: `[speech][bg] amix=inputs=2[aout]`

### Task 2: Create _render_graph.py — single FFmpeg invocation

Builds the FFmpeg command:
```
ffmpeg -y \
  -loop 1 -t 5 -i photo1.jpg \
  -ss 10 -t 8 -i video1.mp4 \
  ... (all inputs) \
  -i music.wav \
  -filter_complex_script graph.txt \
  -map "[vout]" -map "[aout]" \
  -c:v hevc_nvenc ... -c:a aac -b:a 192k \
  output.mp4
```

Title cards (intro/outro) are still pre-rendered as inputs since they're generated content.

### Task 3: Simplify _assemble.py orchestration

New flow:
1. Beat sync EDL
2. Render title cards (intro + outro, 2 FFmpeg calls)
3. Generate filter graph script
4. Run single FFmpeg
5. Write chapters
6. Validate output

### Task 4: Delete dead modules and update tests

### Task 5: Verify with existing EDL
