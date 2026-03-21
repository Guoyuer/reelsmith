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

---

## Issue #3: Parallelize music generation and clip rendering

### Problem
Pipeline is `plan → generate_music → assemble`. Music generation takes ~2min (6 Lyria calls).
Clip rendering (Phase 1 of assemble) also takes ~2min. They're independent but run serially.

### Solution
Change Dagster DAG: `generate_music` and `assemble` both depend on `plan`, not on each other.
Assemble loads music file at Phase 3 (mix), not at start. If music isn't ready, Phase 1+2
run first (clip render + concat), then Phase 3 waits for music file.

### Implementation
1. `definitions.py`: Change assemble's dependency from `generate_music` to `plan`
2. `definitions.py`: Add `generate_music` as a separate branch (not blocking assemble)
3. `assemble.py`: At Phase 3, check if music file exists. If not, wait/skip.
4. Or simpler: keep assemble depending on generate_music but run clip rendering
   as a separate asset that both can depend on.

Actually simplest: just make generate_music and assemble both depend on plan:
```
plan → generate_music
plan → assemble (reads music file at mix step, skips if not ready)
```
But Dagster materializes assets in dependency order. If both depend on plan,
Dagster can run them in parallel if configured with multi-process executor.

### Files to change
- `pipeline/definitions.py`: Change assemble dependency, configure parallel execution
- `pipeline/assemble.py`: Make Phase 3 tolerant of missing music (wait or skip)

### Risk: Low
- Music and assemble already share no state except the EDL file
- If music gen fails, assemble still produces video (just no music)

---

## Issue #2: Gemini single-call fault tolerance

### Problem
One API call decides everything. Failures:
- JSON parse error → plan crashes
- Item count too low → short vlog
- Hallucinated file paths → clips fail to render
- Network timeout → total failure

### Solution
Add three layers of defense:

#### Layer 1: Auto-retry on parse failure
If `EDL.model_validate_json()` throws, retry the Gemini call once.
Same prompt, fresh attempt. Gemini's output is non-deterministic,
second try often succeeds.

#### Layer 2: Fix hallucinated paths
After parsing EDL, validate every `source_file` exists.
For missing files, try fuzzy matching:
- Strip numeric ID prefix: `87681_20250613_002052.heic` → `20250613_002052.heic`
- Search workspace/media/ for similar filenames
- If no match, remove the item from EDL and log a warning
Don't crash — degrade gracefully.

#### Layer 3: Duration check
After parsing, check `edl.estimated_duration()` vs `target_duration`.
If < 80% of target, log a warning: "EDL is {X}s, target is {Y}s — underfilled".
Optionally: re-call Gemini with "You only selected {X}s, add more items to reach {Y}s."

### Implementation
```python
# In plan():
for attempt in range(2):
    try:
        edl_content = _gemini_call(...)
        edl_content = strip_markdown_fences(edl_content)
        edl = EDL.model_validate_json(edl_content)
        break
    except (json.JSONDecodeError, ValidationError) as e:
        _log(f"Parse failed (attempt {attempt+1}): {e}")
        if attempt == 1:
            raise

# Fix paths
for seg in edl.segments:
    seg.items = [item for item in seg.items if _validate_source(item, media_dir, _log)]

# Check duration
actual = edl.estimated_duration()
if actual < target_duration * 0.8:
    _log(f"WARNING: EDL is {actual:.0f}s, target is {target_duration}s")
```

### Files to change
- `pipeline/plan.py`: Add retry loop, path validation, duration check

### Risk: Low
- Retry adds at most one extra API call ($0.03)
- Path fixing is read-only (no side effects)
- Duration warning is informational

---

## Issue #5: Post-assemble output validation

### Problem
Pipeline produces output file without checking if it's valid. Possible issues:
- No audio stream (speech track build failed silently)
- Duration much shorter than expected (xfade truncation)
- File size zero or corrupt
- Missing video stream

### Solution
Add validation at the end of `assemble()`, before returning.

### Implementation
```python
# At end of assemble(), after output_path is created:
def _validate_output(output_path: Path, expected_duration: float, has_speech: bool) -> list[str]:
    """Validate the output video. Returns list of warnings (empty = OK)."""
    warnings = []

    if not output_path.exists() or output_path.stat().st_size < 1000:
        warnings.append(f"Output file missing or empty: {output_path}")
        return warnings

    duration = _probe_duration(output_path)
    if duration < expected_duration * 0.5:
        warnings.append(f"Duration {duration:.0f}s is <50% of expected {expected_duration:.0f}s — possible truncation")

    # Check streams
    probe = run_subprocess(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
         "-of", "csv=p=0", str(output_path)],
        capture_output=True, text=True,
    )
    streams = probe.stdout.strip().split("\n")
    if "video" not in streams:
        warnings.append("No video stream in output")
    if "audio" not in streams and has_speech:
        warnings.append("No audio stream but speech clips were expected")

    return warnings
```

Call it at the end of assemble:
```python
warnings = _validate_output(output_path, tl.total_duration(), bool(speech_entries))
for w in warnings:
    print(f"WARNING: {w}")
if any("truncation" in w or "missing" in w for w in warnings):
    raise RuntimeError(f"Output validation failed: {'; '.join(warnings)}")
```

### Files to change
- `pipeline/assemble.py`: Add `_validate_output()`, call after Phase 3

### Risk: None
- Read-only checks (ffprobe)
- Only raises on critical issues (truncation, missing file)
- Warnings logged for non-critical issues

---

## Issue #7: Externalize prompts

### Problem
~160 lines of Gemini prompt text hardcoded in plan.py. Changing prompt wording
requires code change → commit → restart Dagster.

### Solution
Move prompts to `pipeline/prompts/` directory as markdown files.
Load at runtime. Hot-reloadable without code change.

### File structure
```
pipeline/prompts/
  visual_planner_system.md     # _visual_system_prompt content
  narrative_guidance.yaml      # per-trip-type guidance
  lang_instructions.yaml       # en/cn/both templates
```

### Implementation
```python
# In plan.py:
_PROMPTS_DIR = Path(__file__).parent / "prompts"

def _visual_system_prompt(trip_type: str, language: str = "en") -> str:
    template = (_PROMPTS_DIR / "visual_planner_system.md").read_text()
    guidance = yaml.safe_load((_PROMPTS_DIR / "narrative_guidance.yaml").read_text())
    lang = yaml.safe_load((_PROMPTS_DIR / "lang_instructions.yaml").read_text())
    return template.format(
        guidance=guidance.get(trip_type, guidance["general"]),
        lang_instruction=lang.get(language, lang["en"]),
    )
```

### Trade-offs
- Pro: iterate on prompts without touching Python code
- Pro: prompts are readable markdown, not escaped f-strings
- Con: adds yaml dependency (or just use .md + .json, no new dep)
- Con: prompt variables ({guidance}, {lang_instruction}) need documentation

### Files to change
- New: `pipeline/prompts/visual_planner_system.md`
- New: `pipeline/prompts/narrative_guidance.json`
- New: `pipeline/prompts/lang_instructions.json`
- Modify: `pipeline/plan.py` (load from files instead of inline strings)

### Risk: Low
- Prompts are loaded fresh each call (no caching issues)
- If file missing, fall back to inline default with warning
