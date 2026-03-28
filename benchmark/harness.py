"""Benchmark harness — deterministic, repeatable rendering measurements.

Usage:
    python -m benchmark.harness --edl workspace/runs/test/edl_v1.json --resolution 1080p30
    python -m benchmark.harness --edl workspace/runs/test/edl_v1.json --resolution 1080p30 --baseline results/baseline.json

Collects: wall time (total + per-phase), file size, duration accuracy,
codec metadata, and optionally VMAF/PSNR quality scores.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger("reelsmith.benchmark")


# ---------------------------------------------------------------------------
# Metrics data structures
# ---------------------------------------------------------------------------


@dataclass
class PhaseMetrics:
    """Timing for a single render phase."""

    name: str
    wall_seconds: float = 0.0
    detail: str = ""


@dataclass
class QualityMetrics:
    """Optional quality scores (requires FFmpeg with libvmaf)."""

    vmaf: float | None = None
    psnr_avg: float | None = None
    ssim: float | None = None


@dataclass
class BenchmarkResult:
    """Full result of a single benchmark run."""

    # Identity
    experiment: str = "baseline"
    git_branch: str = ""
    git_sha: str = ""
    ffmpeg_version: str = ""
    timestamp: str = ""

    # Input
    edl_path: str = ""
    resolution: str = ""
    n_segments: int = 0
    n_items: int = 0
    estimated_duration: float = 0.0

    # Output
    output_path: str = ""
    output_size_mb: float = 0.0
    actual_duration: float = 0.0
    duration_accuracy: float = 0.0  # actual / estimated

    # Timing
    total_wall_seconds: float = 0.0
    phases: list[PhaseMetrics] = field(default_factory=list)

    # Codec info
    video_codec: str = ""
    audio_codec: str = ""
    video_bitrate_kbps: int = 0
    hw_encoder: str = ""

    # Quality (optional)
    quality: QualityMetrics = field(default_factory=QualityMetrics)

    # Validation
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, path: Path) -> BenchmarkResult:
        data = json.loads(path.read_text())
        data["phases"] = [PhaseMetrics(**p) for p in data.get("phases", [])]
        data["quality"] = QualityMetrics(**data.get("quality", {}))
        return cls(**data)


# ---------------------------------------------------------------------------
# Metric collection helpers
# ---------------------------------------------------------------------------


def _get_git_info() -> tuple[str, str]:
    """Return (branch, sha) from current git state."""
    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], text=True
        ).strip()
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True
        ).strip()
        return branch, sha
    except (subprocess.SubprocessError, FileNotFoundError):
        return "unknown", "unknown"


def _get_ffmpeg_version() -> str:
    """Return FFmpeg version string."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, text=True, timeout=10
        )
        first_line = (result.stdout or "").split("\n", 1)[0]
        return first_line
    except (subprocess.SubprocessError, FileNotFoundError):
        return "unknown"


def _probe_output(path: Path) -> dict:
    """Probe output file for codec/bitrate/duration metadata."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return json.loads(result.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, FileNotFoundError):
        return {}


def _compute_vmaf(reference: Path, distorted: Path, resolution: str) -> QualityMetrics:
    """Compute VMAF/PSNR/SSIM between reference and distorted videos.

    Requires FFmpeg built with --enable-libvmaf.
    """
    metrics = QualityMetrics()
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-i",
                str(distorted),
                "-i",
                str(reference),
                "-lavfi",
                "libvmaf=log_fmt=json:log_path=/dev/stdout:feature=name=psnr:feature=name=float_ssim",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=600,
        )
        data = json.loads(result.stdout)
        pooled = data.get("pooled_metrics", {})
        metrics.vmaf = pooled.get("vmaf", {}).get("mean")
        metrics.psnr_avg = pooled.get("psnr_y", {}).get("mean")
        metrics.ssim = pooled.get("float_ssim", {}).get("mean")
    except Exception as exc:
        logger.debug("VMAF computation failed (optional): %s", exc)
    return metrics


# ---------------------------------------------------------------------------
# Core benchmark function
# ---------------------------------------------------------------------------


def run_benchmark(
    edl_path: Path,
    resolution: str,
    *,
    experiment: str = "baseline",
    reference_output: Path | None = None,
) -> BenchmarkResult:
    """Run a full assemble benchmark and collect metrics.

    Parameters
    ----------
    edl_path:
        Path to EDL JSON file.
    resolution:
        Resolution string (e.g. "1080p30", "4k60").
    experiment:
        Label for this experiment run.
    reference_output:
        If provided, compute VMAF/PSNR against this reference video.
    """
    from pipeline.cli._commands import _parse_resolution
    from pipeline.config import Config
    from pipeline.edl import EDL

    result = BenchmarkResult(experiment=experiment)
    result.edl_path = str(edl_path)
    result.resolution = resolution
    result.timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    result.git_branch, result.git_sha = _get_git_info()
    result.ffmpeg_version = _get_ffmpeg_version()

    # Load EDL
    edl = EDL.model_validate_json(edl_path.read_text())
    result.n_segments = len(edl.segments)
    result.n_items = sum(len(s.items) for s in edl.segments)
    result.estimated_duration = edl.estimated_duration()

    # Parse resolution
    w, h, fps = _parse_resolution(resolution)

    # Run assemble with timing
    ws_path = str(edl_path.parent)
    cfg = Config.load(ws_path)
    cfg.ensure_dirs()

    from pipeline.assemble import AssembleConfig, assemble

    acfg = AssembleConfig(w=w, h=h, fps=fps)

    t_start = time.monotonic()
    output_path, val_issues = assemble(cfg, acfg)
    result.total_wall_seconds = time.monotonic() - t_start

    # Collect validation issues
    for issue in val_issues:
        if issue["level"] == "error":
            result.errors.append(issue["message"])
        else:
            result.warnings.append(issue["message"])

    # Probe output
    result.output_path = str(output_path)
    if output_path.exists():
        result.output_size_mb = output_path.stat().st_size / (1024 * 1024)
        probe = _probe_output(output_path)

        fmt = probe.get("format", {})
        result.actual_duration = float(fmt.get("duration", 0))
        if result.estimated_duration > 0:
            result.duration_accuracy = result.actual_duration / result.estimated_duration

        for stream in probe.get("streams", []):
            if stream.get("codec_type") == "video":
                result.video_codec = stream.get("codec_name", "")
                result.video_bitrate_kbps = int(stream.get("bit_rate", 0)) // 1000
            elif stream.get("codec_type") == "audio":
                result.audio_codec = stream.get("codec_name", "")

        # Quality metrics (optional)
        if reference_output and reference_output.exists():
            result.quality = _compute_vmaf(reference_output, output_path, resolution)

    return result


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def compare_results(
    baseline: BenchmarkResult, experiment: BenchmarkResult
) -> dict:
    """Compare two benchmark results and return a structured diff."""

    def _pct(base: float, exp: float) -> str:
        if base == 0:
            return "N/A"
        delta = ((exp - base) / base) * 100
        sign = "+" if delta > 0 else ""
        return f"{sign}{delta:.1f}%"

    return {
        "experiment": experiment.experiment,
        "speed": {
            "baseline_s": baseline.total_wall_seconds,
            "experiment_s": experiment.total_wall_seconds,
            "change": _pct(baseline.total_wall_seconds, experiment.total_wall_seconds),
            "faster": experiment.total_wall_seconds < baseline.total_wall_seconds,
        },
        "size": {
            "baseline_mb": baseline.output_size_mb,
            "experiment_mb": experiment.output_size_mb,
            "change": _pct(baseline.output_size_mb, experiment.output_size_mb),
        },
        "duration_accuracy": {
            "baseline": baseline.duration_accuracy,
            "experiment": experiment.duration_accuracy,
        },
        "quality": {
            "vmaf_baseline": baseline.quality.vmaf,
            "vmaf_experiment": experiment.quality.vmaf,
            "vmaf_change": _pct(
                baseline.quality.vmaf or 0, experiment.quality.vmaf or 0
            )
            if baseline.quality.vmaf and experiment.quality.vmaf
            else "N/A",
        },
        "codec_change": baseline.video_codec != experiment.video_codec,
        "new_errors": [
            e for e in experiment.errors if e not in baseline.errors
        ],
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Reelsmith rendering benchmark")
    parser.add_argument("--edl", required=True, help="Path to EDL JSON")
    parser.add_argument("--resolution", "-r", default="1080p30")
    parser.add_argument("--experiment", default="baseline", help="Experiment label")
    parser.add_argument("--baseline", help="Path to baseline results JSON for comparison")
    parser.add_argument("--reference", help="Path to reference video for VMAF")
    parser.add_argument("--output", "-o", help="Save results JSON to this path")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    ref = Path(args.reference) if args.reference else None
    result = run_benchmark(
        Path(args.edl),
        args.resolution,
        experiment=args.experiment,
        reference_output=ref,
    )

    # Save results
    out_path = Path(args.output) if args.output else Path(f"benchmark_{args.experiment}.json")
    out_path.write_text(result.to_json())
    print(f"Results saved to {out_path}")

    # Compare if baseline provided
    if args.baseline:
        baseline = BenchmarkResult.from_json(Path(args.baseline))
        diff = compare_results(baseline, result)
        print("\n--- Comparison ---")
        print(json.dumps(diff, indent=2))
