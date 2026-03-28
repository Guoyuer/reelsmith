"""Benchmark harness — deterministic, repeatable rendering measurements.

Usage:
    python -m benchmark.harness --edl workspace/runs/test/edl_v1.json -r 1080p30 --reference ref.mp4
    python -m benchmark.harness --edl workspace/runs/test/edl_v1.json -r 1080p30 --reference ref.mp4 --baseline results/baseline.json

Collects: wall time (total + per-phase), file size, duration accuracy,
codec metadata, and VMAF/PSNR/SSIM quality scores.

VMAF is mandatory — every benchmark run must compare against a reference
video. FFmpeg must be built with --enable-libvmaf.
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
    """Quality scores via libvmaf (mandatory for all benchmark runs)."""

    vmaf: float = 0.0
    psnr_avg: float = 0.0
    ssim: float = 0.0


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

    # Quality (mandatory — VMAF/PSNR/SSIM vs reference)
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


def check_libvmaf() -> None:
    """Verify that FFmpeg was built with libvmaf support.

    Raises SystemExit if libvmaf is not available.
    """
    try:
        result = subprocess.run(
            ["ffmpeg", "-filters"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if "libvmaf" not in (result.stdout or ""):
            raise SystemExit(
                "FFmpeg was not built with --enable-libvmaf. "
                "VMAF is required for benchmarking. "
                "Install a build with libvmaf support "
                "(e.g. ffmpeg-full on Homebrew, or build from source)."
            )
    except FileNotFoundError:
        raise SystemExit("FFmpeg not found on PATH.")
    except subprocess.SubprocessError as exc:
        raise SystemExit(f"Could not check libvmaf support: {exc}")


def _compute_vmaf(reference: Path, distorted: Path) -> QualityMetrics:
    """Compute VMAF/PSNR/SSIM between reference and distorted videos.

    Raises RuntimeError if computation fails — VMAF is not optional.
    """
    # Use /dev/stderr for log output to keep stdout clean, then parse stderr
    log_path = distorted.parent / f".vmaf_{distorted.stem}.json"
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-i",
                str(distorted),
                "-i",
                str(reference),
                "-lavfi",
                f"libvmaf=log_fmt=json:log_path={log_path}"
                ":feature=name=psnr:feature=name=float_ssim",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"VMAF computation failed (exit {result.returncode}): "
                f"{result.stderr[-500:]}"
            )
        data = json.loads(log_path.read_text())
        pooled = data.get("pooled_metrics", {})
        return QualityMetrics(
            vmaf=pooled.get("vmaf", {}).get("mean", 0.0),
            psnr_avg=pooled.get("psnr_y", {}).get("mean", 0.0),
            ssim=pooled.get("float_ssim", {}).get("mean", 0.0),
        )
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Failed to parse VMAF JSON output: {exc}")
    except FileNotFoundError:
        raise RuntimeError(f"VMAF log file not created: {log_path}")
    finally:
        log_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Core benchmark function
# ---------------------------------------------------------------------------


def run_benchmark(
    edl_path: Path,
    resolution: str,
    reference_output: Path,
    *,
    experiment: str = "baseline",
) -> BenchmarkResult:
    """Run a full assemble benchmark and collect metrics.

    Parameters
    ----------
    edl_path:
        Path to EDL JSON file.
    resolution:
        Resolution string (e.g. "1080p30", "4k60").
    reference_output:
        Reference video for VMAF/PSNR/SSIM comparison (required).
    experiment:
        Label for this experiment run.
    """
    # Fail fast if libvmaf is not available
    check_libvmaf()

    if not reference_output.exists():
        raise FileNotFoundError(
            f"Reference video not found: {reference_output}. "
            "A reference video is required for VMAF quality measurement. "
            "Generate one first with the baseline code."
        )
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

        # Quality metrics (mandatory)
        result.quality = _compute_vmaf(reference_output, output_path)

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
            "vmaf_change": _pct(baseline.quality.vmaf, experiment.quality.vmaf),
            "psnr_baseline": baseline.quality.psnr_avg,
            "psnr_experiment": experiment.quality.psnr_avg,
            "ssim_baseline": baseline.quality.ssim,
            "ssim_experiment": experiment.quality.ssim,
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
    parser.add_argument(
        "--reference", required=True, help="Reference video for VMAF comparison (required)"
    )
    parser.add_argument("--baseline", help="Path to baseline results JSON for comparison")
    parser.add_argument("--output", "-o", help="Save results JSON to this path")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    result = run_benchmark(
        Path(args.edl),
        args.resolution,
        Path(args.reference),
        experiment=args.experiment,
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
