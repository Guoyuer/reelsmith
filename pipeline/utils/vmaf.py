"""VMAF quality comparison between two rendered videos.

Used as a quality harness when testing rendering optimisations:
render the same EDL with different settings, then compare outputs.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .media import run_subprocess

logger = logging.getLogger("reelsmith.utils.vmaf")


@dataclass
class VmafResult:
    """Aggregate VMAF comparison result."""

    score: float  # mean VMAF (0-100)
    min_score: float
    max_score: float
    harmonic_mean: float
    n_frames: int
    per_frame: list[float]  # per-frame scores (empty if not requested)

    @property
    def passed(self) -> bool:
        """True if quality is transparent (VMAF >= 93)."""
        return self.score >= 93.0

    def summary(self) -> str:
        lines = [
            f"VMAF: {self.score:.2f}  (min={self.min_score:.2f}, max={self.max_score:.2f})",
            f"Harmonic mean: {self.harmonic_mean:.2f}",
            f"Frames: {self.n_frames}",
        ]
        if self.score >= 93:
            lines.append("Quality: TRANSPARENT (>= 93)")
        elif self.score >= 85:
            lines.append("Quality: GOOD (85-93)")
        elif self.score >= 75:
            lines.append("Quality: ACCEPTABLE (75-85)")
        else:
            lines.append("Quality: POOR (< 75)")
        return "\n".join(lines)


def compare_vmaf(
    reference: Path,
    distorted: Path,
    *,
    threads: int = 8,
    subsample: int = 1,
) -> VmafResult:
    """Run VMAF comparison between reference and distorted videos.

    *reference* is the baseline (higher quality or known-good).
    *distorted* is the test render to evaluate.
    *subsample*: 1 = every frame (accurate), 5 = every 5th (fast estimate).

    Returns VmafResult with aggregate and optionally per-frame scores.
    """
    if not reference.exists():
        raise FileNotFoundError(f"Reference not found: {reference}")
    if not distorted.exists():
        raise FileNotFoundError(f"Distorted not found: {distorted}")

    log_path = distorted.with_suffix(".vmaf.json")

    # Remux both inputs to fix non-monotonic DTS from concat demuxer,
    # which causes libvmaf frame alignment failures.
    ref_clean = reference.with_stem(reference.stem + "_vmaf_ref")
    dist_clean = distorted.with_stem(distorted.stem + "_vmaf_dist")
    for src, dst in [(reference, ref_clean), (distorted, dist_clean)]:
        remux = run_subprocess(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(src),
                "-c",
                "copy",
                "-fflags",
                "+genpts",
                str(dst),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if remux.returncode != 0:
            raise RuntimeError(f"Remux failed for {src}: {remux.stderr[-300:]}")

    # libvmaf expects: first input = distorted, second input = reference.
    # -an disables audio to prevent stream mapping interference.
    cmd = [
        "ffmpeg",
        "-an",
        "-i",
        str(dist_clean),
        "-an",
        "-i",
        str(ref_clean),
        "-filter_complex",
        f"[0:v][1:v]libvmaf=n_threads={threads}:n_subsample={subsample}"
        f":log_path={str(log_path).replace(chr(92), '/')}:log_fmt=json",
        "-f",
        "null",
        "-",
    ]

    logger.info("Running VMAF comparison (subsample=%d)...", subsample)
    result = run_subprocess(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"VMAF failed: {result.stderr[-500:]}")

    # Parse JSON log
    data = json.loads(log_path.read_text(encoding="utf-8"))
    frames = data.get("frames", [])
    scores = [f["metrics"]["vmaf"] for f in frames]

    pool = data.get("pooled_metrics", {}).get("vmaf", {})
    mean = pool.get("mean", 0.0)
    hmean = pool.get("harmonic_mean", 0.0)
    min_s = pool.get("min", 0.0)
    max_s = pool.get("max", 0.0)

    vmaf = VmafResult(
        score=mean,
        min_score=min_s,
        max_score=max_s,
        harmonic_mean=hmean,
        n_frames=len(scores),
        per_frame=scores,
    )
    logger.info(
        "VMAF: %.2f (min=%.2f, max=%.2f, %d frames)",
        vmaf.score,
        vmaf.min_score,
        vmaf.max_score,
        vmaf.n_frames,
    )

    # Clean up remuxed temp files
    ref_clean.unlink(missing_ok=True)
    dist_clean.unlink(missing_ok=True)

    return vmaf
