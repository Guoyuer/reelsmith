"""Tests for new modules: parallel, encoder (RenderContext), RenderReport,
prompt loading, EDL quality field, edl persistence helpers."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pipeline.edl import EDL, EditItem, Segment, MusicTrack


# ---------------------------------------------------------------------------
# parallel.run_parallel
# ---------------------------------------------------------------------------

class TestRunParallel:
    def test_empty_tasks(self):
        from pipeline.parallel import run_parallel
        assert run_parallel([], max_workers=2) == []

    def test_returns_all_results(self):
        from pipeline.parallel import run_parallel
        tasks = [(i, lambda i=i: i * 2) for i in range(5)]
        results = run_parallel(tasks, max_workers=2)
        assert len(results) == 5
        result_dict = dict(results)
        for i in range(5):
            assert result_dict[i] == i * 2

    def test_captures_exceptions(self):
        from pipeline.parallel import run_parallel

        def fail():
            raise ValueError("boom")

        tasks = [(0, lambda: 42), (1, fail)]
        results = run_parallel(tasks, max_workers=2)
        assert len(results) == 2
        result_dict = dict(results)
        assert result_dict[0] == 42
        assert isinstance(result_dict[1], ValueError)

    def test_progress_callback(self):
        from pipeline.parallel import run_parallel
        progress_calls = []
        tasks = [(i, lambda: None) for i in range(4)]
        run_parallel(tasks, max_workers=2,
                     progress_fn=lambda done, total: progress_calls.append((done, total)))
        assert len(progress_calls) == 4
        assert progress_calls[-1] == (4, 4)

    def test_batching(self):
        from pipeline.parallel import run_parallel
        tasks = [(i, lambda: None) for i in range(10)]
        results = run_parallel(tasks, max_workers=2, batch_size=3)
        assert len(results) == 10


# ---------------------------------------------------------------------------
# encoder.RenderContext
# ---------------------------------------------------------------------------

class TestRenderContext:
    def test_init_context_resets_caches(self):
        from pipeline.encoder import init_context, get_context
        ctx1 = init_context(quality=0.5)
        ctx1._dim_cache["test"] = (100, 100)
        ctx2 = init_context(quality=1.0)
        assert ctx2.quality == 1.0
        assert "test" not in ctx2._dim_cache
        assert ctx2 is get_context()

    def test_probe_dimensions_caches(self):
        from pipeline.encoder import RenderContext
        ctx = RenderContext()
        fake = MagicMock()
        fake.stdout = "1920x1080\n"
        with patch("pipeline.encoder.run_subprocess", return_value=fake):
            dims = ctx.probe_dimensions(Path("/fake/video.mp4"))
        assert dims == (1920, 1080)
        # Second call should hit cache (no subprocess)
        with patch("pipeline.encoder.run_subprocess", side_effect=RuntimeError("should not be called")):
            dims2 = ctx.probe_dimensions(Path("/fake/video.mp4"))
        assert dims2 == (1920, 1080)

    def test_probe_duration_caches(self):
        from pipeline.encoder import RenderContext
        ctx = RenderContext()
        fake = MagicMock()
        fake.stdout = "123.45\n"
        with patch("pipeline.encoder.run_subprocess", return_value=fake):
            dur = ctx.probe_duration(Path("/fake/video.mp4"))
        assert dur == 123.45
        # Cache hit
        with patch("pipeline.encoder.run_subprocess", side_effect=RuntimeError("should not be called")):
            dur2 = ctx.probe_duration(Path("/fake/video.mp4"))
        assert dur2 == 123.45

    def test_probe_dimensions_handles_bad_output(self):
        from pipeline.encoder import RenderContext
        ctx = RenderContext()
        fake = MagicMock()
        fake.stdout = "garbage\n"
        with patch("pipeline.encoder.run_subprocess", return_value=fake):
            dims = ctx.probe_dimensions(Path("/bad"))
        assert dims == (0, 0)

    def test_probe_duration_handles_bad_output(self):
        from pipeline.encoder import RenderContext
        ctx = RenderContext()
        fake = MagicMock()
        fake.stdout = "\n"
        with patch("pipeline.encoder.run_subprocess", return_value=fake):
            dur = ctx.probe_duration(Path("/bad"))
        assert dur == 0.0


# ---------------------------------------------------------------------------
# encoder.target_bitrate
# ---------------------------------------------------------------------------

class TestTargetBitrate:
    def test_4k_30fps(self):
        from pipeline.encoder import target_bitrate
        br = target_bitrate(3840, 2160, 30)
        assert br == "45M"

    def test_4k_60fps(self):
        from pipeline.encoder import target_bitrate
        br = target_bitrate(3840, 2160, 60)
        # 45 * 1.5 = 67
        assert br == "67M"

    def test_1080p_30fps(self):
        from pipeline.encoder import target_bitrate
        br = target_bitrate(1920, 1080, 30)
        assert br == "8M"

    def test_quality_multiplier(self):
        from pipeline.encoder import target_bitrate
        br = target_bitrate(1920, 1080, 30, quality=2.0)
        assert br == "16M"

    def test_quality_half(self):
        from pipeline.encoder import target_bitrate
        br = target_bitrate(1920, 1080, 30, quality=0.5)
        assert br == "4M"

    def test_small_resolution(self):
        from pipeline.encoder import target_bitrate
        br = target_bitrate(320, 180, 24)
        assert br == "3M"


# ---------------------------------------------------------------------------
# assemble.RenderReport
# ---------------------------------------------------------------------------

class TestRenderReport:
    def test_empty_report(self):
        from pipeline.assemble import RenderReport
        r = RenderReport()
        assert r.ok_count == 0
        assert r.skipped_count == 0
        assert r.failed_count == 0
        assert "0/0 OK" in r.summary()

    def test_all_ok(self):
        from pipeline.assemble import RenderReport, ClipStatus
        r = RenderReport(clips=[
            ClipStatus("c1", "a.jpg", "ok"),
            ClipStatus("c2", "b.jpg", "ok"),
        ])
        assert r.ok_count == 2
        assert r.skipped_count == 0
        assert "2/2 OK" in r.summary()

    def test_mixed_status(self):
        from pipeline.assemble import RenderReport, ClipStatus
        r = RenderReport(clips=[
            ClipStatus("c1", "a.jpg", "ok"),
            ClipStatus("c2", "b.jpg", "skipped", "source not found"),
            ClipStatus("c3", "c.jpg", "failed", "timeout"),
        ])
        assert r.ok_count == 1
        assert r.skipped_count == 1
        assert r.failed_count == 1
        summary = r.summary()
        assert "1/3 OK" in summary
        assert "skipped" in summary
        assert "timeout" in summary

    def test_to_metadata(self):
        from pipeline.assemble import RenderReport, ClipStatus
        r = RenderReport(clips=[ClipStatus("c1", "a.jpg", "ok")])
        meta = r.to_metadata()
        assert meta["clips_ok"] == 1
        assert meta["clips_skipped"] == 0


# ---------------------------------------------------------------------------
# Prompt loading
# ---------------------------------------------------------------------------

class TestPromptLoading:
    def test_load_system_template(self):
        from pipeline.plan import _load_system_template
        template = _load_system_template()
        assert "{guidance}" in template
        assert "{lang_instruction}" in template
        assert len(template) > 1000

    def test_load_narrative_guidance(self):
        from pipeline.plan import _load_narrative_guidance
        data = _load_narrative_guidance()
        assert "family" in data
        assert "general" in data
        assert "_default_focus" in data
        assert "Family is the heart" in data["family"]

    def test_load_lang_instructions(self):
        from pipeline.plan import _load_lang_instructions
        data = _load_lang_instructions()
        assert "en" in data
        assert "cn" in data
        assert "both" in data

    def test_visual_system_prompt_substitution(self):
        from pipeline.plan import _visual_system_prompt
        prompt = _visual_system_prompt("family", "cn")
        # Guidance substituted
        assert "Family is the heart" in prompt
        # Language substituted
        assert "简体中文" in prompt
        # No leftover placeholders
        assert "{guidance}" not in prompt
        assert "{lang_instruction}" not in prompt

    def test_unknown_trip_type_falls_back(self):
        from pipeline.plan import _visual_system_prompt
        prompt = _visual_system_prompt("nonexistent", "en")
        assert "Balanced storytelling" in prompt  # general fallback

    def test_missing_prompt_file_raises(self, tmp_path):
        from pipeline.plan import _load_json
        with pytest.raises(FileNotFoundError):
            # Temporarily override _PROMPTS_DIR to a nonexistent path
            import pipeline.plan as plan_mod
            orig = plan_mod._PROMPTS_DIR
            try:
                plan_mod._PROMPTS_DIR = tmp_path / "nonexistent"
                _load_json("anything.json")
            finally:
                plan_mod._PROMPTS_DIR = orig






# ---------------------------------------------------------------------------
# EDL persistence helpers
# ---------------------------------------------------------------------------

class TestEDLPersistence:
    def test_save_and_load(self, tmp_path):
        from pipeline.edl import save_edl, load_latest_edl
        from pipeline.config import Config

        cfg = Config(workspace=tmp_path)
        cfg.ensure_dirs()
        edl = EDL(title="Test", target_duration=30, segments=[
            Segment(name="S1", items=[
                EditItem(source_file="a.jpg", media_type="photo"),
            ]),
        ])
        save_edl(cfg, edl, version=3)
        assert (tmp_path / "edl_v3.json").exists()

        loaded, version = load_latest_edl(cfg)
        assert version == 3
        assert loaded.title == "Test"
        assert len(loaded.all_items()) == 1

    def test_find_latest_version(self, tmp_path):
        from pipeline.edl import find_latest_version
        from pipeline.config import Config

        cfg = Config(workspace=tmp_path)
        (tmp_path / "edl_v1.json").write_text("{}")
        (tmp_path / "edl_v5.json").write_text("{}")
        (tmp_path / "edl_v3.json").write_text("{}")
        assert find_latest_version(cfg) == 5

    def test_no_edl_raises(self, tmp_path):
        from pipeline.edl import load_latest_edl
        from pipeline.config import Config

        cfg = Config(workspace=tmp_path)
        cfg.ensure_dirs()
        with pytest.raises(FileNotFoundError):
            load_latest_edl(cfg)
