# Remove xfade: Per-clip fades + demuxer concat

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace fragile xfade filter chains with per-clip fade-in/fade-out baked during render, then simple demuxer concat. Eliminates NVENC crashes, group splitting, and audio-video sync drift.

**Architecture:** During Phase 1 clip rendering, each clip gets a fade-out appended (and fade-in for inter-segment transitions). Phase 2 becomes a single demuxer concat — no xfade, no groups, no re-encoding. Timeline simplifies to sequential offset accumulation with no overlap math.

**Tech Stack:** FFmpeg `fade` video filter (already used in title cards), concat demuxer

---

## What changes

| Component | Before | After |
|-----------|--------|-------|
| **render_photo/video** | No fades | Fade-out at end, fade-in at start for segment boundaries |
| **concat.py** | xfade groups + fallback + re-encode | Single demuxer concat (`-c:v copy`) |
| **timeline.py** | Complex group-based offset with overlap | Simple sequential: `offset += duration` |
| **grouping.py** | Shared between concat + timeline | Deleted |
| **_assemble.py** | transition_duration used for overlap | transition_duration used for fade length |

## What gets deleted

- `concat_xfade()` — entire function
- `_concat_filter()` — entire function
- `_grouping.py` — entire module
- `partition_into_groups` — all references
- `XFADE_MAP` — no longer needed
- `Timeline.build()` (mathematical estimate) — only `build_actual` needed, and it simplifies
- `_compute_group_offsets()` — no groups

---

### Task 1: Add fade filters to render_photo and render_video

**Files:**
- Modify: `pipeline/assemble/_render.py`
- Test: `tests/test_assemble.py`

The clip dict now carries `fade_in` and `fade_out` durations. render_photo/render_video append FFmpeg `fade` filters when these are > 0.

- [ ] **Step 1: Write failing test for photo fade-out**

```python
# In tests/test_assemble.py
class TestClipFades:
    @pytest.mark.integration
    def test_photo_with_fade_out(self, tmp_path):
        """Photo clip should have fade-to-black at end when fade_out > 0."""
        from pipeline.assemble._render import render_photo
        from pipeline.assemble._encoder import RenderContext
        from pipeline.edl import EditItem
        from PIL import Image

        img = tmp_path / "photo.jpg"
        Image.new("RGB", (320, 180), "red").save(img, "JPEG")
        item = EditItem(source_file=str(img), media_type="photo", display_duration=3.0)
        ctx = RenderContext(w=320, h=180, fps=24)
        out = tmp_path / "clip.mp4"
        render_photo(item, out, ctx=ctx, fade_out=0.5)
        assert out.exists()
        assert out.stat().st_size > 1000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_assemble.py::TestClipFades::test_photo_with_fade_out -v`
Expected: FAIL — `render_photo() got an unexpected keyword argument 'fade_out'`

- [ ] **Step 3: Add fade_in/fade_out params to render_photo and render_video**

In `pipeline/assemble/_render.py`, add `fade_in: float = 0.0, fade_out: float = 0.0` to both functions. Append to the video filter chain:

```python
# After color grade + text overlay, before closing the -vf/-filter_complex:
fade_filters = ""
if fade_in > 0:
    fade_filters += f",fade=t=in:d={fade_in}"
if fade_out > 0:
    fade_filters += f",fade=t=out:st={item.display_duration - fade_out}:d={fade_out}"
```

For render_video, the fade_out start time is `output_duration - fade_out` where output_duration = source_duration / speed.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_assemble.py::TestClipFades -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pipeline/assemble/_render.py tests/test_assemble.py
git commit -m "feat: add fade_in/fade_out to render_photo and render_video"
```

---

### Task 2: Pass fade durations from _assemble.py to render

**Files:**
- Modify: `pipeline/assemble/_assemble.py`

Instead of storing `transition`/`transition_duration` on all_clips for xfade, compute fade_in/fade_out per clip at render time:

- `fade_out`: applies to every clip except the last. Duration = the NEXT clip's transition_duration.
- `fade_in`: applies to the first clip of each segment (except seg 0). Duration = segment_transition_duration.

- [ ] **Step 1: Modify _do_render to accept and pass fade params**

In `_render_clips`, compute fade_out for each clip based on the next clip's transition. Pass `fade_in` and `fade_out` to render_photo/render_video.

```python
# In _do_render():
if item.media_type == "photo":
    render_photo(item, clip_path, ctx=job.ctx, color_temp=color_temp,
                 text_overlay=item.text_overlay, language=job.lang,
                 fade_in=fade_in, fade_out=fade_out)
```

The fade values come from the all_clips dict built in the transition assignment loop. Change the dict to carry `fade_in`/`fade_out` instead of `transition`/`transition_duration`.

- [ ] **Step 2: Update transition assignment loop**

Replace the current transition/td logic with fade_in/fade_out:

```python
# For each clip:
fade_in = 0.0
fade_out = 0.0

if item_idx == 0 and seg_idx > 0:
    # First clip of new segment: fade in
    fade_in = segment.segment_transition_duration
if not is_last_clip_overall:
    # Fade out = next clip's transition duration (or segment boundary)
    fade_out = next_transition_duration
```

- [ ] **Step 3: Run full test suite**

Run: `pytest tests/ -x -q`
Expected: PASS (some tests may need transition_duration → fade_out adjustments)

- [ ] **Step 4: Commit**

```bash
git add pipeline/assemble/_assemble.py
git commit -m "feat: compute fade_in/fade_out per clip, pass to render"
```

---

### Task 3: Replace concat with simple demuxer

**Files:**
- Modify: `pipeline/assemble/_concat.py`

Replace the entire `concatenate()` function with a simple demuxer concat. Delete `concat_xfade`, `_concat_filter`, and all group logic.

- [ ] **Step 1: Rewrite concatenate()**

```python
def concatenate(clips: list[dict], output_path: Path, **_kwargs) -> None:
    """Concatenate pre-faded clips via demuxer (no re-encoding)."""
    if len(clips) == 1:
        shutil.copy(str(clips[0]["path"]), str(output_path))
        return

    list_path = output_path.with_suffix(".txt")
    with open(list_path, "w") as f:
        for clip in clips:
            f.write(f"file '{clip['path'].resolve()}'\n")

    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(list_path),
        "-c:v", "copy", "-c:a", "copy",
        "-fflags", "+genpts",
        "-avoid_negative_ts", "make_zero",
        str(output_path),
    ]
    result = run_subprocess(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Concat failed: {result.stderr[-300:]}")
```

- [ ] **Step 2: Delete concat_xfade, _concat_filter, all XFADE_MAP usage**

- [ ] **Step 3: Run tests**

Run: `pytest tests/ -x -q`

- [ ] **Step 4: Commit**

```bash
git add pipeline/assemble/_concat.py
git commit -m "feat: replace xfade with demuxer-only concat"
```

---

### Task 4: Simplify Timeline — no groups, no overlap

**Files:**
- Modify: `pipeline/assemble/_timeline.py`
- Delete: `pipeline/assemble/_grouping.py`

Timeline becomes trivial: each clip starts where the previous one ends.

- [ ] **Step 1: Rewrite Timeline**

```python
def _compute_offsets(self) -> None:
    offset = 0.0
    for e in self.entries:
        e.video_offset = offset
        e.visible_offset = offset
        e.end_time = offset + e.actual_duration
        offset += e.actual_duration
```

Delete `build_actual`, `_compute_group_offsets`, `_compute_offsets_actual`. Only `build()` needed — it just probes durations and accumulates.

- [ ] **Step 2: Remove grouping import and _grouping.py**

- [ ] **Step 3: Update _assemble.py to use simplified Timeline.build()**

Replace `Timeline.build_actual(all_clips, output_dir)` with `Timeline.build(all_clips, ctx=job.ctx)`.

- [ ] **Step 4: Run tests, fix any timeline test failures**

Run: `pytest tests/ -x -q`

- [ ] **Step 5: Commit**

```bash
git add pipeline/assemble/_timeline.py pipeline/assemble/_assemble.py
git rm pipeline/assemble/_grouping.py
git commit -m "feat: simplify timeline — sequential offsets, no groups"
```

---

### Task 5: Clean up EDL and dead code

**Files:**
- Modify: `pipeline/edl.py` — remove `XFADE_MAP`
- Modify: `pipeline/assemble/__init__.py` — remove grouping from exports if any
- Modify: `tests/` — fix any broken test imports/assertions

- [ ] **Step 1: Remove XFADE_MAP from edl.py**

The transition Literal types stay (Gemini still uses them for semantic intent), but XFADE_MAP is no longer needed since we don't map to FFmpeg xfade filter names.

- [ ] **Step 2: Update validate_edl — transition check uses Literal, not XFADE_MAP**

- [ ] **Step 3: Fix all broken tests**

- [ ] **Step 4: Run full suite + ruff + pyright**

Run: `pytest tests/ -x -q && python -m ruff check pipeline/ cli.py tests/ && python -m pyright pipeline/ cli.py`

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore: remove xfade dead code — XFADE_MAP, grouping, concat_xfade"
```

---

### Task 6: Integration test — end-to-end with fades

**Files:**
- Modify: `tests/test_integration.py`

- [ ] **Step 1: Update existing concat integration tests**

The `TestXfadeConcatenation` tests need updating — they test xfade behavior. Replace with tests that verify demuxer concat produces valid output with expected duration.

- [ ] **Step 2: Add test for fade_black transition rendering**

Verify that a 3-clip sequence with fade_black transitions produces a video where fades are visible (probe first/last frames for near-black pixels, or just check duration math).

- [ ] **Step 3: Run full suite**

Run: `pytest tests/ -x -q`

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "test: update integration tests for demuxer-only concat"
```
