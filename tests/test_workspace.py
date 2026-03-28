"""Tests for pipeline.cli._workspace — helper functions and workspace command."""

from __future__ import annotations

import json
import time
from pathlib import Path

from click.testing import CliRunner

from pipeline.cli._workspace import (
    _age_str,
    _dir_size,
    _fmt_size,
    _latest_mtime,
    _run_detail,
)


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

    def test_just_under_one_hour(self):
        result = _age_str(time.time() - 3500)  # ~58 min
        assert result == "58m ago"


# ---------------------------------------------------------------------------
# _latest_mtime
# ---------------------------------------------------------------------------


class TestLatestMtime:
    def test_empty_dir(self, tmp_path: Path):
        d = tmp_path / "empty"
        d.mkdir()
        assert _latest_mtime(d) == 0.0

    def test_nonexistent(self, tmp_path: Path):
        assert _latest_mtime(tmp_path / "missing") == 0.0

    def test_returns_newest(self, tmp_path: Path):
        d = tmp_path / "multi"
        d.mkdir()
        (d / "old.txt").write_text("old")
        time.sleep(0.05)
        (d / "new.txt").write_text("new")
        result = _latest_mtime(d)
        assert result >= (d / "new.txt").stat().st_mtime


# ---------------------------------------------------------------------------
# _run_detail
# ---------------------------------------------------------------------------


class TestRunDetail:
    def test_empty_run_dir(self, tmp_path: Path):
        run_dir = tmp_path / "test_run"
        run_dir.mkdir()
        info = _run_detail(run_dir)
        assert info["name"] == "test_run"
        assert info["size"] == 0
        assert info["file_count"] == 0
        assert info["edl_versions"] == 0
        assert info["outputs"] == []
        assert info["old_output_bytes"] == 0

    def test_with_edl(self, tmp_path: Path):
        run_dir = tmp_path / "run_with_edl"
        run_dir.mkdir()
        edl_data = {
            "title": "My Trip",
            "target_duration": 180,
            "language": "cn",
            "segments": [
                {
                    "name": "Seg1",
                    "items": [
                        {"source_file": "a.jpg", "media_type": "photo"},
                        {
                            "source_file": "b.mp4",
                            "media_type": "video",
                            "keep_audio": True,
                        },
                    ],
                }
            ],
        }
        (run_dir / "edl_v1.json").write_text(json.dumps(edl_data))
        info = _run_detail(run_dir)
        assert info["edl_versions"] == 1
        assert info["edl_latest"] == 1
        assert info["title"] == "My Trip"
        assert info["segments"] == 1
        assert info["items"] == 2
        assert info["n_videos"] == 1
        assert info["n_keep_audio"] == 1
        assert info["language"] == "cn"

    def test_with_outputs(self, tmp_path: Path):
        run_dir = tmp_path / "run_output"
        run_dir.mkdir()
        out_dir = run_dir / "output"
        out_dir.mkdir()
        # stem.split("_v")[1] must parse as int — use simple vlog_v{N}.mp4
        (out_dir / "vlog_v1.mp4").write_bytes(b"\x00" * 5000)
        (out_dir / "vlog_v2.mp4").write_bytes(b"\x00" * 8000)
        info = _run_detail(run_dir)
        assert len(info["outputs"]) == 2
        assert info["old_output_bytes"] == 5000

    def test_single_output_no_old_bytes(self, tmp_path: Path):
        run_dir = tmp_path / "run_one"
        run_dir.mkdir()
        out_dir = run_dir / "output"
        out_dir.mkdir()
        (out_dir / "vlog_v1.mp4").write_bytes(b"\x00" * 5000)
        info = _run_detail(run_dir)
        assert info["old_output_bytes"] == 0

    def test_intermediates(self, tmp_path: Path):
        run_dir = tmp_path / "run_inter"
        run_dir.mkdir()
        out_dir = run_dir / "output"
        out_dir.mkdir()
        (out_dir / "x_nomix.mp4").write_bytes(b"\x00" * 3000)
        (out_dir / "x_speech.wav").write_bytes(b"\x00" * 2000)
        info = _run_detail(run_dir)
        assert info["intermediate_bytes"] == 5000
        assert len(info["intermediate_files"]) == 2

    def test_legacy_txt_clips(self, tmp_path: Path):
        run_dir = tmp_path / "run_legacy"
        run_dir.mkdir()
        render_dir = run_dir / "render"
        render_dir.mkdir()
        (render_dir / "seg00_item00_txt.mp4").write_bytes(b"\x00" * 1000)
        info = _run_detail(run_dir)
        assert info["legacy_txt_bytes"] == 1000

    def test_multiple_edl_versions(self, tmp_path: Path):
        run_dir = tmp_path / "run_multi_edl"
        run_dir.mkdir()
        for v in [1, 2, 3]:
            edl_data = {
                "title": f"V{v}",
                "segments": [{"name": "S", "items": []}],
            }
            (run_dir / f"edl_v{v}.json").write_text(json.dumps(edl_data))
        info = _run_detail(run_dir)
        assert info["edl_versions"] == 3
        assert info["edl_latest"] == 3
        assert info["title"] == "V3"


# ---------------------------------------------------------------------------
# workspace CLI command
# ---------------------------------------------------------------------------


class TestWorkspaceCommand:
    def test_no_workspace_dir(self, tmp_path: Path):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            from pipeline.cli._workspace import workspace

            result = runner.invoke(workspace, [], standalone_mode=False)
            assert "No workspace directory found" in result.output

    def test_empty_workspace(self, tmp_path: Path):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            Path("workspace").mkdir()
            from pipeline.cli._workspace import workspace

            result = runner.invoke(workspace, [], standalone_mode=False)
            assert "Workspace" in result.output

    def test_clean_safe_nothing(self, tmp_path: Path):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            ws = Path("workspace")
            ws.mkdir()
            (ws / "runs").mkdir()
            run_dir = ws / "runs" / "test"
            run_dir.mkdir()
            (run_dir / "edl_v1.json").write_text("{}")
            from pipeline.cli._workspace import workspace

            result = runner.invoke(
                workspace, ["--clean", "safe", "-y"], standalone_mode=False
            )
            assert "Nothing to clean" in result.output
