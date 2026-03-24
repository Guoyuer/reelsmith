# Rich UI Enhancements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Enhance terminal UI with Rich components: validation panel, Markdown thinking, stage separators, post-processing diff, video metadata table, cost breakdown, and completion bell.

**Architecture:** All changes are additive — enhance existing log/display output with Rich renderables. No pipeline logic changes. Terminal-only (file logs unchanged). All Rich usage guarded by `try/except ImportError` and `sys.stderr.isatty()`.

**Tech Stack:** Rich (Panel, Markdown, Rule, Table, Tree, Console.bell)

---

## File Map

| File | Changes |
|------|---------|
| `cli.py` | Add Rule separators between stages, bell on completion |
| `pipeline/plan/_gemini.py` | Render thinking as Markdown, cost breakdown table |
| `pipeline/plan/_postprocess.py` | Post-processing diff summary, already has tree |
| `pipeline/assemble/_assemble.py` | Validation results panel |
| `pipeline/prepare.py` | Video probe summary table |

## Constraints

- All Rich rendering guarded: `if sys.stderr.isatty()` + `try/except ImportError`
- File logs (DEBUG/INFO) unchanged — Rich output is terminal-only supplement
- No new dependencies (Rich already in requirements)
- Existing tests must pass unchanged

---

### Task 1: Stage Rule separators in CLI

**Files:** Modify `cli.py`

Add `rich.Rule` between stages in terminal output. When a stage starts, print a horizontal rule with the stage name.

In `_PipelineDisplay.start()`, after setting state, print a rule to the live console:

```python
def start(self, stage: str) -> None:
    self._current_stage = stage
    self._stage_t_start[stage] = time.monotonic()
    self._stage_data[stage].update(state="running", label="", current=0, total=0)
    # Print stage separator
    if self._live:
        from rich.rule import Rule
        self._live.console.print(Rule(f"[bold]{stage.replace('_', ' ')}[/bold]", style="dim"))
    self._refresh()
```

### Task 2: Validation results Panel

**Files:** Modify `pipeline/assemble/_assemble.py`

Replace plain log lines for validation with a Rich Panel containing ✓/✗ per check.

After `_validate_output` returns, render results:

```python
# In assemble(), after validation, add:
try:
    import sys
    if sys.stderr.isatty():
        from rich.console import Console
        from rich.panel import Panel
        from rich.text import Text
        lines = Text()
        for i in val_issues:
            icon = "✗" if i["level"] == "error" else "⚠"
            style = "red" if i["level"] == "error" else "yellow"
            lines.append(f" {icon} ", style=style)
            lines.append(f"[{i['check']}] {i['message']}\n")
        if not val_issues:
            lines.append(" ✓ All checks passed\n", style="green")
        Console(stderr=True).print(Panel(lines, title="Validation", border_style="green" if not val_issues else "yellow"))
except ImportError:
    pass
```

### Task 3: Gemini thinking as Markdown

**Files:** Modify `pipeline/plan/_gemini.py`

When thinking output is received, render it as Rich Markdown to terminal (keep plain text in file log).

In the thinking block (line ~185-188), add:

```python
if getattr(part, "thought", False) and part.text:
    logger.info(f"  [Thinking] {len(part.text)} chars")
    for line in part.text.split("\n"):
        logger.debug(f"  💭 {line}")
    # Rich Markdown to terminal
    try:
        import sys
        if sys.stderr.isatty():
            from rich.console import Console
            from rich.markdown import Markdown
            from rich.panel import Panel
            Console(stderr=True).print(Panel(Markdown(part.text), title="💭 Thinking", border_style="dim"))
    except ImportError:
        pass
```

### Task 4: Cost breakdown table per API call

**Files:** Modify `pipeline/plan/_gemini.py`

After computing cost, render a small inline table:

```python
try:
    import sys
    if sys.stderr.isatty():
        from rich.console import Console
        from rich.table import Table
        t = Table(show_header=True, border_style="dim", title="Gemini API", title_style="bold")
        t.add_column("", style="dim")
        t.add_column("Tokens", justify="right")
        t.add_column("Rate", justify="right")
        t.add_column("Cost", justify="right")
        t.add_row("Input", f"{input_tokens:,}", f"${in_rate}/M", f"${input_tokens * in_rate / 1e6:.3f}")
        t.add_row("Output", f"{output_tokens:,}", f"${out_rate}/M", f"${output_tokens * out_rate / 1e6:.3f}")
        t.add_section()
        t.add_row("[bold]Total", f"[bold]{input_tokens + output_tokens:,}", "", f"[bold]${cost_est:.3f}")
        Console(stderr=True).print(t)
except ImportError:
    pass
```

### Task 5: Post-processing diff summary

**Files:** Modify `pipeline/plan/_postprocess.py`

After all post-processing in `_plan_visual`, render a compact diff:

Add a function `print_postprocess_summary` that shows what changed:

```python
def print_postprocess_summary(
    n_path_fixed: int, n_path_removed: int,
    n_trim_fixed: int, n_trim_removed: int,
    n_dedup: int, original_items: int, final_items: int,
) -> None:
    try:
        import sys
        if sys.stderr.isatty():
            from rich.console import Console
            from rich.table import Table
            t = Table(title="Post-processing", border_style="dim", title_style="bold")
            t.add_column("Step")
            t.add_column("Result", justify="right")
            if n_path_fixed: t.add_row("Paths fixed", f"[green]{n_path_fixed}[/green]")
            if n_path_removed: t.add_row("Items removed (bad path)", f"[red]{n_path_removed}[/red]")
            if n_trim_fixed: t.add_row("Trim points clamped", f"[yellow]{n_trim_fixed}[/yellow]")
            if n_trim_removed: t.add_row("Items removed (bad trim)", f"[red]{n_trim_removed}[/red]")
            if n_dedup: t.add_row("Duplicates removed", f"[yellow]{n_dedup}[/yellow]")
            t.add_section()
            t.add_row("[bold]Items", f"[bold]{original_items} → {final_items}")
            Console(stderr=True).print(t)
    except ImportError:
        pass
```

Then call it from `_plan_visual` after post-processing.

### Task 6: Video probe summary table

**Files:** Modify `pipeline/prepare.py`

After video probe phase completes, show a summary table of video metadata.

```python
# After Phase 3 loop, add:
try:
    import sys
    if sys.stderr.isatty() and uncached_videos:
        from rich.console import Console
        from rich.table import Table
        t = Table(title=f"Video Probe ({len(uncached_videos)} videos)", border_style="dim", show_lines=False)
        t.add_column("File", max_width=30)
        t.add_column("Duration", justify="right")
        t.add_column("Resolution")
        t.add_column("FPS", justify="right")
        for entry, _, _, _ in uncached_videos[:20]:  # cap at 20 rows
            t.add_row(
                entry["filename"][:30],
                f"{entry.get('video_duration', 0):.0f}s",
                f"{entry.get('video_width', '?')}x{entry.get('video_height', '?')}",
                f"{entry.get('video_fps', '?')}",
            )
        if len(uncached_videos) > 20:
            t.add_row(f"... +{len(uncached_videos)-20} more", "", "", "")
        Console(stderr=True).print(t)
except ImportError:
    pass
```

### Task 7: Pipeline completion bell

**Files:** Modify `cli.py`

Add `console.bell()` when pipeline completes (success or failure):

In `_run_pipeline`, after the try/except/finally block, at the end of `print_summary`:

```python
def print_summary(self) -> None:
    # ... existing code ...
    Console(stderr=True).print(table)
    # Bell notification
    try:
        Console(stderr=True).bell()
    except Exception:
        pass
```

### Task 8: Test and commit

Run full test suite, verify no breakage, commit all changes.
