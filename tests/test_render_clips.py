"""Tests for _render_clips and _concat_and_mix with fully mocked FFmpeg."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pipeline.assemble._assemble import AssembleJob, _render_clips, _concat_and_mix, RenderReport
from pipeline.assemble._encoder import RenderContext
from pipeline.config import Config
from pipeline.edl import EDL, EditItem, MusicTrack, Segment


def _make_job(tmp_path, items=None, music=None, intro="none", outro="none"):
    cfg = Config(workspace=tmp_path)
    cfg.ensure_dirs()

    if items is None:
        # Create real photo files
        photos = []
        for i in range(3):
            p = cfg.media_dir / f"photo_{i}.jpg"
            p.write_bytes(b"\xff\xd8" + b"\x00" * 100)
            photos.append(EditItem(source_file=str(p), media_type="photo", display_duration=4.0))
        items = photos

    edl = EDL(
        title="Test",
        target_duration=60,
        intro_style=intro,
        outro_style=outro,
        music=music,
        segments=[Segment(name="S1", items=items, transition="crossfade", transition_duration=0.5)],
    )
    ctx = RenderContext(w=320, h=180, fps=15)
    return AssembleJob(cfg=cfg, edl=edl, version=1, ctx=ctx)


class TestRenderClips:
    def _mock_render(self, job):
        """Make render_photo/render_video/render_title_card create dummy clip files."""
        def fake_render_item(item, output_path, **kwargs):
            output_path.write_bytes(b"\x00" * 2000)

        def fake_render_title(title, subtitle, output_path, **kwargs):
            output_path.write_bytes(b"\x00" * 2000)

        return (
            patch("pipeline.assemble._assemble.render_photo", side_effect=fake_render_item),
            patch("pipeline.assemble._assemble.render_video", side_effect=fake_render_item),
            patch("pipeline.assemble._assemble.render_title_card", side_effect=fake_render_title),
        )

    def test_renders_all_clips(self, tmp_path):
        job = _make_job(tmp_path)
        p1, p2, p3 = self._mock_render(job)
        with p1, p2, p3:
            clips, report = _render_clips(job, skip_broken=True)

        assert len(clips) == 3
        assert report.ok_count == 3
        assert report.failed_count == 0

    def test_skips_missing_source(self, tmp_path):
        # Mix of valid and invalid — skip_broken=True should skip bad ones
        valid_photo = tmp_path / "media" / "good.jpg"
        valid_photo.parent.mkdir(parents=True, exist_ok=True)
        valid_photo.write_bytes(b"\xff\xd8" + b"\x00" * 100)
        items = [
            EditItem(source_file="/nonexistent/file.jpg", media_type="photo", display_duration=4.0),
            EditItem(source_file=str(valid_photo), media_type="photo", display_duration=4.0),
        ]
        job = _make_job(tmp_path, items=items)
        p1, p2, p3 = self._mock_render(job)
        with p1, p2, p3:
            clips, report = _render_clips(job, skip_broken=True)

        assert report.skipped_count == 1
        assert len(clips) >= 1  # at least the valid one

    def test_raises_on_broken_without_skip(self, tmp_path):
        items = [
            EditItem(source_file="/nonexistent/file.jpg", media_type="photo", display_duration=4.0),
        ]
        job = _make_job(tmp_path, items=items)
        with pytest.raises(RuntimeError, match="Clip rendering"):
            _render_clips(job, skip_broken=False)

    def test_intro_outro_added(self, tmp_path):
        job = _make_job(tmp_path, intro="title_card", outro="fade_title")
        p1, p2, p3 = self._mock_render(job)
        with p1, p2, p3:
            clips, report = _render_clips(job, skip_broken=True)

        # 3 photos + intro + outro = 5
        assert len(clips) == 5
        # First clip should be intro (cut transition)
        assert clips[0]["transition"] == "cut"
        # Second clip should have fade_black from intro
        assert clips[1]["transition"] == "fade_black"

    def test_progress_callback_called(self, tmp_path):
        job = _make_job(tmp_path)
        p1, p2, p3 = self._mock_render(job)
        calls = []
        def cb(done, total, detail):
            calls.append(done)

        with p1, p2, p3:
            _render_clips(job, progress_callback=cb, skip_broken=True)

        assert len(calls) >= 1

    def test_cached_clips_not_re_rendered(self, tmp_path):
        job = _make_job(tmp_path)
        # Pre-create clip files
        for seg_idx, seg in enumerate(job.edl.segments):
            for item_idx, _ in enumerate(seg.items):
                clip = job.clips_dir / f"seg{seg_idx:02d}_item{item_idx:02d}_{job.res_label}.mp4"
                clip.write_bytes(b"\x00" * 2000)

        with patch("pipeline.assemble._assemble.render_photo") as mock_render:
            clips, report = _render_clips(job, skip_broken=True)

        # render_photo should NOT be called since clips exist
        mock_render.assert_not_called()
        assert len(clips) == 3


class TestConcatAndMix:
    def test_basic_concat(self, tmp_path):
        job = _make_job(tmp_path)
        clips = []
        for i in range(3):
            p = tmp_path / "clips" / f"clip_{i}.mp4"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"\x00" * 2000)
            clips.append({
                "path": p, "duration": 4.0,
                "transition": "cut" if i == 0 else "crossfade",
                "transition_duration": 0 if i == 0 else 0.5,
                "keep_audio": False, "media_type": "photo",
            })

        with patch("pipeline.assemble._assemble.concatenate") as mock_concat, \
             patch("pipeline.assemble._assemble.mix_final_audio") as mock_mix, \
             patch("pipeline.assemble._assemble.write_chapters"), \
             patch("pipeline.assemble._assemble.Timeline") as mock_tl:
            mock_tl.build_actual.return_value = MagicMock()
            mock_tl.build_actual.return_value.speech_entries.return_value = []
            mock_tl.build_actual.return_value.speech_ranges.return_value = []
            mock_tl.build_actual.return_value.dump = MagicMock()

            _concat_and_mix(job, clips, t_start=0)

        mock_concat.assert_called_once()
        mock_mix.assert_called_once()
