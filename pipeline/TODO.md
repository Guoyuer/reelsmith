# Pipeline Refactor Plan

## Merge preprocess + analyze → prepare

Current: `fetch_media → preprocess → analyze → plan → generate_music → assemble` (6 stages)
Target: `fetch_media → prepare → plan → generate_music → assemble` (5 stages)

### prepare stage (merge of preprocess + analyze)
- Family member auto-detection
- Timeline construction (day → time_block → location)
- Photo thumbnails (512px JPEG, cached)
- EXIF extraction (focal_length, aperture, ISO, cached)
- Video duration probing (cached)
- Preview clip generation (320p 10fps, shared cache in workspace/preview_clips/)
- Contact sheet generation (6/sheet @ 600px, shared cache in workspace/contact_sheets/)

### plan stage (pure AI, no FFmpeg/Pillow)
- Read all data from prepare
- Single Gemini API call with thinking mode
- Output: EDL with segments, items, music_mood, text overlays

### Why
- preprocess and analyze are both "prepare data" — no reason to separate
- Preview clips and contact sheets are per-file (not per-plan), belong in prepare
- plan becomes a pure AI stage: read prepared data → call Gemini → return EDL
- Re-plan is instant: prepare is fully cached, only Gemini API call runs

### Files to change
- New `pipeline/prepare.py` (merge preprocess.py + analyze.py + clip/sheet gen from plan.py)
- Simplify `pipeline/plan.py` (remove _build_visual_content_blocks, _generate_video_clips_parallel)
- Update `pipeline/definitions.py` (3 assets instead of 4, update deps)
- Update `run.py` (PrepareConfig instead of PreprocessConfig + AnalyzeConfig)
- Update tests
- Delete `pipeline/preprocess.py` and `pipeline/analyze.py`

### Not changing
- generate_music stays after plan (needs music_mood from EDL)
- assemble unchanged
- Timeline module unchanged
