"""Tests for pipeline.parallel — batched ThreadPoolExecutor runner."""

from __future__ import annotations

from pipeline.parallel import run_parallel


class TestRunParallel:
    def test_empty_tasks(self):
        assert run_parallel([], max_workers=2) == []

    def test_returns_all_results(self):
        tasks = [(i, lambda i=i: i * 2) for i in range(5)]
        results = run_parallel(tasks, max_workers=2)
        assert len(results) == 5
        result_dict = dict(results)
        for i in range(5):
            assert result_dict[i] == i * 2

    def test_captures_exceptions(self):
        def fail():
            raise ValueError("boom")

        tasks = [(0, lambda: 42), (1, fail)]
        results = run_parallel(tasks, max_workers=2)
        result_dict = dict(results)
        assert result_dict[0] == 42
        assert isinstance(result_dict[1], ValueError)

    def test_progress_callback(self):
        progress_calls = []
        tasks = [(i, lambda: None) for i in range(4)]
        run_parallel(
            tasks,
            max_workers=2,
            progress_fn=lambda done, total: progress_calls.append((done, total)),
        )
        assert len(progress_calls) == 4
        assert progress_calls[-1] == (4, 4)

    def test_batching(self):
        tasks = [(i, lambda: None) for i in range(10)]
        results = run_parallel(tasks, max_workers=2, batch_size=3)
        assert len(results) == 10
