"""Experiment runner — execute A/B tests of FFmpeg feature changes.

Each experiment:
1. Records the baseline (current code) benchmark
2. Applies a code patch (via git branch or callable)
3. Runs the same benchmark on the patched code
4. Compares and logs results to experiments.jsonl

Usage:
    from benchmark.experiment import Experiment, run_experiment

    exp = Experiment(
        name="vulkan-hevc-encoder",
        description="Replace NVENC with Vulkan HEVC encoding",
        edl_path=Path("workspace/runs/test/edl_v1.json"),
        resolution="1080p30",
        branch="experiment/vulkan-hevc",  # or patch_fn for in-process changes
    )
    report = run_experiment(exp)
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .harness import BenchmarkResult, compare_results, run_benchmark

logger = logging.getLogger("reelsmith.benchmark.experiment")

EXPERIMENTS_LOG = Path("benchmark/experiments.jsonl")


@dataclass
class Experiment:
    """Definition of a single A/B experiment."""

    name: str
    description: str
    edl_path: Path
    resolution: str = "1080p30"

    # Option A: git branch with the change
    branch: str | None = None

    # Option B: in-process monkey-patch (for quick iteration)
    # Callable that applies the change; returns a cleanup callable.
    patch_fn: object = None  # Callable[[], Callable[[], None]] | None

    # How many times to run each side (for variance)
    iterations: int = 1

    # Reference video for VMAF comparison (required)
    reference_output: Path = field(default_factory=lambda: Path("benchmark/reference.mp4"))


@dataclass
class ExperimentReport:
    """Result of running an A/B experiment."""

    name: str
    description: str
    timestamp: str = ""
    baseline: BenchmarkResult | None = None
    experiment: BenchmarkResult | None = None
    comparison: dict = field(default_factory=dict)
    verdict: str = ""  # "accept", "reject", "inconclusive"
    notes: str = ""

    def to_jsonl_entry(self) -> str:
        """Single-line JSON for appending to experiments.jsonl."""
        return json.dumps(
            {
                "name": self.name,
                "description": self.description,
                "timestamp": self.timestamp,
                "verdict": self.verdict,
                "notes": self.notes,
                "comparison": self.comparison,
                "baseline_sha": self.baseline.git_sha if self.baseline else "",
                "experiment_sha": self.experiment.git_sha if self.experiment else "",
            }
        )


def _checkout_and_run(
    branch: str, edl_path: Path, resolution: str, label: str, ref: Path
) -> BenchmarkResult:
    """Checkout a branch in a worktree, run benchmark, return result."""
    worktree = Path(f"/tmp/reelsmith-bench-{label}-{int(time.time())}")
    try:
        subprocess.run(
            ["git", "worktree", "add", str(worktree), branch],
            check=True,
            capture_output=True,
        )
        # Run benchmark in subprocess (isolated Python env)
        result = subprocess.run(
            [
                "python",
                "-m",
                "benchmark.harness",
                "--edl",
                str(edl_path),
                "-r",
                resolution,
                "--reference",
                str(ref),
                "--experiment",
                label,
                "-o",
                str(worktree / "bench_result.json"),
            ],
            cwd=str(worktree),
            capture_output=True,
            text=True,
            timeout=1800,  # 30min max
        )
        if result.returncode != 0:
            logger.error("Benchmark failed in worktree %s: %s", branch, result.stderr)
            r = BenchmarkResult(experiment=label)
            r.errors.append(f"Subprocess failed: {result.stderr[:500]}")
            return r
        return BenchmarkResult.from_json(worktree / "bench_result.json")
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            capture_output=True,
        )


def run_experiment(exp: Experiment) -> ExperimentReport:
    """Run a full A/B experiment and return the report."""
    report = ExperimentReport(
        name=exp.name,
        description=exp.description,
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
    )

    logger.info("=== Experiment: %s ===", exp.name)
    logger.info("Description: %s", exp.description)

    # --- Baseline ---
    logger.info("Running baseline...")
    if exp.branch:
        # Git branch mode: use worktrees for isolation
        report.baseline = _checkout_and_run(
            "main", exp.edl_path, exp.resolution, "baseline", exp.reference_output
        )
        logger.info("Running experiment branch: %s", exp.branch)
        report.experiment = _checkout_and_run(
            exp.branch,
            exp.edl_path,
            exp.resolution,
            exp.name,
            exp.reference_output,
        )
    elif exp.patch_fn:
        # In-process mode: monkey-patch for quick iteration
        report.baseline = run_benchmark(
            exp.edl_path,
            exp.resolution,
            exp.reference_output,
            experiment="baseline",
        )
        logger.info("Applying patch and running experiment...")
        cleanup = exp.patch_fn()
        try:
            report.experiment = run_benchmark(
                exp.edl_path,
                exp.resolution,
                exp.reference_output,
                experiment=exp.name,
            )
        finally:
            cleanup()
    else:
        report.notes = "No branch or patch_fn specified"
        report.verdict = "inconclusive"
        return report

    # --- Compare ---
    if report.baseline and report.experiment:
        report.comparison = compare_results(report.baseline, report.experiment)

        # Auto-verdict heuristics (VMAF is always available)
        comp = report.comparison
        has_new_errors = bool(comp.get("new_errors"))
        speed_info = comp.get("speed", {})
        is_faster = speed_info.get("faster", False)
        quality_info = comp.get("quality", {})
        vmaf_baseline = quality_info["vmaf_baseline"]
        vmaf_experiment = quality_info["vmaf_experiment"]
        vmaf_drop = vmaf_baseline - vmaf_experiment

        if has_new_errors:
            report.verdict = "reject"
            report.notes = f"New errors: {comp['new_errors']}"
        elif vmaf_drop > 1.0:
            report.verdict = "reject"
            report.notes = (
                f"Quality regression: VMAF {vmaf_baseline:.2f} → "
                f"{vmaf_experiment:.2f} (Δ{-vmaf_drop:+.2f})"
            )
        elif is_faster and vmaf_drop <= 1.0:
            report.verdict = "accept"
            report.notes = (
                f"Faster by {speed_info['change']}, "
                f"VMAF {vmaf_experiment:.2f} (Δ{-vmaf_drop:+.2f})"
            )
        else:
            report.verdict = "inconclusive"
            report.notes = (
                f"Speed: {speed_info['change']}, "
                f"VMAF {vmaf_experiment:.2f} (Δ{-vmaf_drop:+.2f})"
            )

    # --- Log ---
    EXPERIMENTS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(EXPERIMENTS_LOG, "a") as f:
        f.write(report.to_jsonl_entry() + "\n")

    logger.info("Verdict: %s — %s", report.verdict, report.notes)
    return report
