"""Pipeline display, logging, and progress helpers."""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import click

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STAGES = ["fetch", "prepare", "plan", "generate_music", "assemble"]

_ICON_PENDING = "\u25cb"  # ○
_ICON_RUNNING = "\u23f3"  # ⏳
_ICON_DONE = "\u2705"  # ✅
_ICON_FAILED = "\u274c"  # ❌


# ---------------------------------------------------------------------------
# Pipeline display
# ---------------------------------------------------------------------------


class _PipelineDisplay:
    """Terminal progress display with pinned status bar (rich.live).

    Status panel stays at the bottom; log output scrolls above it.
    Falls back to simple line-by-line output when stderr is not a TTY.
    """

    _SPINNER_FRAMES = "\u280b\u2819\u2838\u2830\u2826\u280e"  # braille spinner

    def __init__(self, run_name: str, headline: str, stages: list[str]):
        self._run_name = run_name
        self._headline = headline
        self._stages = stages
        self._t_start = time.monotonic()
        self._stage_t_start: dict[str, float] = {}  # stage → start time
        self.output_file: str = ""  # set by assemble stage
        self.api_cost: float = 0.0  # accumulated Gemini API cost
        # stage → (state, detail, progress_current, progress_total, duration)
        self._stage_data: dict[str, dict[str, Any]] = {}
        self._current_stage: str | None = None
        self._live = None
        self._tick = 0

        for s in stages:
            self._stage_data[s] = {
                "state": "pending",
                "label": "",
                "current": 0,
                "total": 0,
                "dur": 0,
                "subs": {},  # sub_name → {"current": N, "total": N}
                "sub_order": [],  # ordered sub-stage names
            }

        if sys.stderr.isatty():
            try:
                from rich.live import Live

                self._live = Live(
                    console=self._get_console(),
                    refresh_per_second=4,
                    get_renderable=self._render_panel,
                )
                self._live.start()
            except ImportError:
                pass

    def _get_console(self):
        from rich.console import Console

        return Console(stderr=True)

    def _render_panel(self):
        import shutil

        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text

        self._tick += 1
        elapsed = time.monotonic() - self._t_start
        term_w = shutil.get_terminal_size((80, 24)).columns
        panel_w = min(max(term_w - 2, 50), 120)

        table = Table.grid(padding=(0, 1))
        table.add_column(width=2)  # icon
        table.add_column(width=16)  # stage name
        table.add_column()  # detail + progress

        for s in self._stages:
            d = self._stage_data[s]
            name = s.replace("_", " ")

            if d["state"] == "pending":
                icon = Text(_ICON_PENDING, style="dim")
                label = Text(name, style="dim")
                info = Text("")
            elif d["state"] == "running":
                spinner = self._SPINNER_FRAMES[self._tick % len(self._SPINNER_FRAMES)]
                icon = Text(spinner, style="bold cyan")
                label = Text(name, style="bold cyan")
                stage_elapsed = time.monotonic() - self._stage_t_start.get(
                    s, self._t_start
                )
                if not d["subs"] and d["total"] > 0:
                    info = self._build_progress(d)
                elif d["label"]:
                    info = Text.assemble(
                        (d["label"], "cyan"),
                        ("  ", ""),
                        (f"{stage_elapsed:.0f}s", "dim cyan"),
                    )
                else:
                    info = Text(f"{stage_elapsed:.0f}s", style="dim cyan")
            elif d["state"] == "done":
                icon = Text(_ICON_DONE)
                label = Text(name, style="green")
                dur_s = f"{d['dur']:.0f}s"
                cached = " (cached)" if d["dur"] < 0.5 else ""
                detail = d["detail"]
                info = Text(f"{detail}  {dur_s}{cached}".strip(), style="green")
            else:  # failed
                icon = Text(_ICON_FAILED)
                label = Text(name, style="bold red")
                info = Text(d["detail"][:50], style="red")

            table.add_row(icon, label, info)

            # Sub-stage progress rows
            if d["state"] == "running" and d["subs"]:
                for sub_name in d["sub_order"]:
                    sub = d["subs"][sub_name]
                    sub_bar = self._build_sub_progress(sub_name, sub)
                    table.add_row(Text(""), Text(""), sub_bar)

        panel = Panel(
            table,
            title=f"[bold]\U0001f3ac {self._run_name}[/bold] \u2014 {self._headline}",
            subtitle=f"[dim]elapsed {elapsed:.0f}s[/dim]",
            border_style="bright_blue",
            width=panel_w,
            padding=(0, 1),
        )
        return panel

    def _build_progress(self, d: dict[str, Any]):
        """Build progress text with Rich ProgressBar for a running stage."""
        from rich.progress_bar import ProgressBar
        from rich.table import Table as InlineTable
        from rich.text import Text

        cur, total = d["current"], d["total"]
        label = d.get("label", "")

        if total > 0:
            row = InlineTable.grid(padding=(0, 1))
            row.add_column(width=20)
            row.add_column(width=12)
            row.add_column()
            bar = ProgressBar(
                total=total,
                completed=cur,
                width=20,
                complete_style="cyan",
                finished_style="green",
            )
            row.add_row(
                bar,
                Text(f"{cur}/{total} {cur / total:.0%}", style="cyan"),
                Text(label, style="dim"),
            )
            return row
        elif label:
            return Text(label, style="cyan")
        return Text("", style="cyan")

    def _build_sub_progress(self, name: str, sub: dict[str, Any]):
        """Build a sub-stage progress line with Rich ProgressBar."""
        from rich.progress_bar import ProgressBar
        from rich.table import Table as InlineTable
        from rich.text import Text

        cur, total = sub["current"], sub["total"]
        if total > 0:
            row = InlineTable.grid(padding=(0, 1))
            row.add_column(width=18)
            row.add_column(width=16)
            row.add_column()
            bar = ProgressBar(
                total=total,
                completed=cur,
                width=16,
                complete_style="bar.complete",
                finished_style="green",
            )
            row.add_row(
                Text(name, style="dim cyan"),
                bar,
                Text(f"{cur}/{total} {cur / total:.0%}", style="dim cyan"),
            )
            return row
        return Text(f"  {name}", style="dim cyan")

    def start(self, stage: str) -> None:
        self._current_stage = stage
        self._stage_t_start[stage] = time.monotonic()
        self._stage_data[stage].update(state="running", label="", current=0, total=0)
        # Stage separator: Rich Rule on terminal, DEBUG to log file only
        # (terminal handler is INFO level, file handler is DEBUG)
        if self._live:
            from rich.rule import Rule

            self._live.console.print(
                Rule(f"[bold]{stage.replace('_', ' ')}[/bold]", style="dim")
            )
        logging.getLogger("vlog").debug("--- %s ---", stage.replace("_", " "))
        self._refresh()

    def update(self, stage: str, detail: str) -> None:
        d = self._stage_data.get(stage)
        if not d:
            return
        # Sub-stage format: "sub_name:current/total"
        if ":" in detail and "/" in detail:
            try:
                sub_name, progress = detail.split(":", 1)
                cur_s, tot_s = progress.split("/")
                cur, tot = int(cur_s), int(tot_s)
                if sub_name not in d["subs"]:
                    d["sub_order"].append(sub_name)
                d["subs"][sub_name] = {"current": cur, "total": tot}
                self._refresh()
                return
            except (ValueError, IndexError):
                pass
        # Simple "current/total" format (no sub-stage)
        if "/" in detail:
            try:
                parts = detail.split("/")
                d["current"] = int(parts[0])
                d["total"] = int(parts[1])
            except (ValueError, IndexError):
                pass
        else:
            d["label"] = detail
        self._refresh()

    def done(self, stage: str, detail: str, duration: float) -> None:
        cached = " (cached)" if duration < 0.5 else ""
        self._stage_data[stage].update(
            state="done", detail=f"{detail}{cached}", dur=duration
        )
        self._current_stage = None
        self._refresh()

    def fail(self, stage: str, error: str) -> None:
        if stage in self._stage_data:
            self._stage_data[stage].update(state="failed", detail=error[:60])
        self._refresh()

    def stop(self) -> None:
        """Stop the live display (call in finally block)."""
        if self._live:
            self._live.update(self._render_panel())
            self._live.stop()
            self._live = None

    def print_summary(self) -> None:
        """Print a summary table after pipeline completes."""
        from rich.console import Console
        from rich.table import Table

        table = Table(
            title=f"\U0001f3ac {self._run_name} \u2014 summary",
            border_style="dim",
            title_style="bold",
        )
        table.add_column("Stage", style="bold")
        table.add_column("Status", justify="center")
        table.add_column("Duration", justify="right")
        table.add_column("Detail")

        total_dur = 0.0
        for s in self._stages:
            d = self._stage_data[s]
            name = s.replace("_", " ")
            state = d["state"]
            dur = d.get("dur", 0)
            detail = d.get("detail", "")

            if state == "done":
                icon = "[green]\u2713[/green]"
                dur_str = f"{dur:.0f}s" if dur >= 0.5 else "cached"
                total_dur += dur
            elif state == "failed":
                icon = "[red]\u2717[/red]"
                dur_str = ""
            else:
                icon = "[dim]\u2014[/dim]"
                dur_str = ""
                detail = "skipped"

            table.add_row(name, icon, dur_str, detail)

        table.add_section()
        footer_detail = []
        if self.api_cost > 0:
            footer_detail.append(f"API ~${self.api_cost:.2f}")
        if self.output_file:
            from pathlib import Path

            p = Path(self.output_file)
            if p.exists():
                size_mb = round(p.stat().st_size / 1024 / 1024, 1)
                footer_detail.append(f"{p.name} ({size_mb}MB)")
            else:
                footer_detail.append(p.name)
        table.add_row(
            "[bold]total",
            "",
            f"[bold]{total_dur:.0f}s",
            "  ".join(footer_detail),
        )

        con = Console(stderr=True)
        con.print(table)
        try:
            con.bell()
        except Exception:
            pass

    def _refresh(self) -> None:
        if self._live:
            self._live.update(self._render_panel())


# ---------------------------------------------------------------------------
# Logging & progress helpers
# ---------------------------------------------------------------------------


def _setup_logging(
    run_name: str, display: _PipelineDisplay | None = None
) -> logging.Logger:
    """Configure dual-output logger: terminal + run.log file.

    When display has a live panel, uses RichHandler so logs print above it.
    """
    logger = logging.getLogger("vlog")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    # Terminal — use RichHandler if live display active (prints above panel)
    if display and display._live:
        from rich.logging import RichHandler

        console = RichHandler(
            console=display._live.console,
            show_path=False,
            show_level=True,
            markup=True,
            rich_tracebacks=True,
            tracebacks_suppress=[click],
            log_time_format="%H:%M:%S",
        )
        console.setLevel(logging.INFO)
        console.setFormatter(logging.Formatter("%(message)s"))
    else:
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(logging.INFO)
        console.setFormatter(
            logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S")
        )
    logger.addHandler(console)

    # File — one log per run, timestamped
    from pipeline.config import Config

    log_dir = Path(Config.run_workspace(run_name=run_name))
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"run_{ts}.log"
    fh = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)

    return logger


def _progress_cb(
    logger: logging.Logger, display: _PipelineDisplay, stage: str, t0: float
):
    """Create a progress callback that logs to file and updates display."""

    seen_names: set[str] = set()

    def cb(current: int, total: int, name: str) -> None:
        if total == 0:
            # Accumulate API cost if reported
            if name.startswith("~$"):
                try:
                    cost = float(name.split("$")[1].split(" ")[0])
                    display.api_cost += cost
                except (ValueError, IndexError):
                    pass
            # Status text only (no progress bar)
            display.update(stage, name)
            return
        seen_names.add(name)
        if len(seen_names) <= 5:
            # Few distinct names → sub-stage progress bars (e.g. assemble phases)
            display.update(stage, f"{name}:{current}/{total}")
        else:
            # Many distinct names → single main progress bar + label
            display.update(stage, f"{current}/{total}")
            d = display._stage_data.get(stage)
            if d:
                d["label"] = name
        # Log at ~25% intervals to file (skip final — stage done log covers it)
        interval = max(total // 4, 1)
        if current % interval == 0 and current < total:
            elapsed = time.monotonic() - t0
            eta = (elapsed / current * (total - current) / 60) if current else 0
            pct = current / total * 100
            logger.info(
                "%s: %d/%d (%.0f%%) ETA %.1fmin — %s",
                stage,
                current,
                total,
                pct,
                eta,
                name,
            )

    return cb


def _build_headline_from_args(stages: list[str], plan=None) -> str:
    """Build a short headline from plan config for display."""
    parts = []
    if plan:
        if plan.target_duration:
            parts.append(f"{plan.target_duration}s")
        if plan.style:
            parts.append(plan.style)
        if plan.trip_type:
            parts.append(f"{plan.trip_type} vlog")
    if not parts:
        parts.append(", ".join(stages))
    return " ".join(parts)
