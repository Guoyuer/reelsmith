"""Shared parallel task runner for FFmpeg and other subprocess work."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable


def run_parallel(
    tasks: list[tuple[Any, Callable[[], Any]]],
    max_workers: int,
    *,
    batch_size: int | None = None,
    progress_fn: Callable[[int, int], None] | None = None,
) -> list[tuple[Any, Any | Exception]]:
    """Run tasks in parallel with batching and interrupt handling.

    Args:
        tasks: List of (task_id, callable) pairs.
        max_workers: Thread pool size.
        batch_size: If set, submit in batches (limits orphan processes on kill).
                    Defaults to max_workers * 3.
        progress_fn: Called with (completed_count, total_count) after each task.

    Returns:
        List of (task_id, result_or_exception) in completion order.
    """
    if not tasks:
        return []

    total = len(tasks)
    batch_size = batch_size or max_workers * 3
    results: list[tuple[Any, Any | Exception]] = []

    try:
        for batch_start in range(0, total, batch_size):
            batch = tasks[batch_start:batch_start + batch_size]
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(fn): tid for tid, fn in batch}
                for future in as_completed(futures):
                    tid = futures[future]
                    try:
                        results.append((tid, future.result()))
                    except Exception as e:
                        results.append((tid, e))
                    if progress_fn:
                        progress_fn(len(results), total)
    except (KeyboardInterrupt, SystemExit):
        if progress_fn:
            progress_fn(len(results), total)
        raise

    return results
