"""Tests for pipeline.cli._workspace — helper functions."""

from __future__ import annotations

import time

from pipeline.cli._workspace import _age_str, _dir_size, _fmt_size


class TestFmtSize:
    def test_kb(self):
        assert _fmt_size(500 * 1024) == "500 KB"

    def test_mb(self):
        assert _fmt_size(50 * 1024**2) == "50 MB"

    def test_gb(self):
        assert _fmt_size(2 * 1024**3) == "2.0 GB"

    def test_small(self):
        assert _fmt_size(512) == "0 KB"  # 512/1024 truncated to 0


class TestDirSize:
    def test_empty_dir(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        size, count = _dir_size(d)
        assert size == 0
        assert count == 0

    def test_nonexistent(self, tmp_path):
        size, count = _dir_size(tmp_path / "nope")
        assert size == 0
        assert count == 0

    def test_with_files(self, tmp_path):
        d = tmp_path / "data"
        d.mkdir()
        (d / "a.txt").write_bytes(b"x" * 100)
        (d / "b.txt").write_bytes(b"y" * 200)
        size, count = _dir_size(d)
        assert size == 300
        assert count == 2

    def test_recursive(self, tmp_path):
        d = tmp_path / "data"
        sub = d / "sub"
        sub.mkdir(parents=True)
        (d / "a.txt").write_bytes(b"x" * 50)
        (sub / "b.txt").write_bytes(b"y" * 50)
        size, count = _dir_size(d)
        assert size == 100
        assert count == 2


class TestAgeStr:
    def test_minutes(self):
        result = _age_str(time.time() - 120)  # 2 minutes ago
        assert result == "2m ago"

    def test_hours(self):
        result = _age_str(time.time() - 7200)  # 2 hours ago
        assert result == "2h ago"

    def test_days(self):
        result = _age_str(time.time() - 172800)  # 2 days ago
        assert result == "2d ago"

    def test_very_recent(self):
        result = _age_str(time.time() - 10)  # 10 seconds ago
        assert result == "1m ago"  # clamped to min 1m
