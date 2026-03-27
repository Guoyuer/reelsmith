"""Tests for pipeline.plan._orchestrate — plan() and _plan_visual() with mocked Gemini."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from pipeline.config import Config
from pipeline.plan import PlanConfig


def _setup_workspace(tmp_path, n_photos=2):
    """Create a minimal workspace with preprocessed data and photos."""
    cfg = Config(workspace=tmp_path / "runs" / "test")
    cfg.ensure_dirs()
    cfg.media_dir.mkdir(parents=True, exist_ok=True)

    photo_paths = []
    for i in range(1, n_photos + 1):
        photo = cfg.media_dir / f"photo_{i}.jpg"
        photo.write_bytes(b"\xff\xd8" + b"\x00" * 100)
        thumb = cfg.thumbnails_dir / f"photo_{i}_thumb.jpg"
        thumb.write_bytes(b"\xff\xd8" + b"\x00" * 50)
        photo_paths.append(str(photo))

    manifest = [
        {
            "id": i,
            "local_path": photo_paths[i - 1],
            "taken_at": f"2025-06-13T{10 + i}:30:00",
        }
        for i in range(1, n_photos + 1)
    ]
    cfg.manifest_path.write_text(json.dumps(manifest))
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    for i in range(1, n_photos + 1):
        (cfg.cache_dir / f"{i}.json").write_text(
            json.dumps(
                {
                    "thumbnail_path": str(cfg.thumbnails_dir / f"photo_{i}_thumb.jpg"),
                }
            )
        )

    return cfg, photo_paths


def _fake_edl_json(photo_paths, title="Test Trip", segment_name="Morning"):
    """Build a fake EDL response JSON using the given photo paths."""
    return json.dumps(
        {
            "title": title,
            "target_duration": 60,
            "segments": [
                {
                    "name": segment_name,
                    "music_mood": "gentle",
                    "items": [
                        {
                            "source_file": path,
                            "media_type": "photo",
                            "display_duration": 30.0 + i * 10,
                        }
                        for i, path in enumerate(photo_paths)
                    ],
                }
            ],
        }
    )


def _patch_gemini(return_value):
    return patch("pipeline.plan._orchestrate._gemini_call", return_value=return_value)


class TestPlanOrchestration:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        self.cfg, self.photo_paths = _setup_workspace(tmp_path)

    def _plan(self, **kwargs):
        from pipeline.plan import plan

        return plan(self.cfg, PlanConfig(target_duration=60, **kwargs))

    def test_creates_edl_file(self):
        with _patch_gemini(_fake_edl_json(self.photo_paths)):
            edl, version = self._plan()

        assert version == 1
        assert self.cfg.edl_path(1).exists()
        assert edl.title == "Test Trip"
        assert len(edl.all_items()) == 2

    def test_forces_video_effect_none(self):
        video_file = self.cfg.media_dir / "clip.mp4"
        video_file.write_bytes(b"\x00" * 200)

        fake = json.dumps(
            {
                "title": "T",
                "target_duration": 60,
                "segments": [
                    {
                        "name": "S",
                        "music_mood": "m",
                        "items": [
                            {
                                "source_file": str(video_file),
                                "media_type": "video",
                                "display_duration": 60.0,
                                "effect": "ken_burns_in",
                            },
                        ],
                    }
                ],
            }
        )

        with _patch_gemini(fake):
            edl, _ = self._plan()

        assert edl.all_items()[0].effect == "none"

    def test_sets_metadata(self):
        with _patch_gemini(_fake_edl_json(self.photo_paths)):
            edl, _ = self._plan(style="cinematic", trip_type="solo", language="cn")

        assert edl.style == "cinematic"
        assert edl.trip_type == "solo"
        assert edl.language == "cn"

    def test_increments_version(self):
        with _patch_gemini(_fake_edl_json(self.photo_paths)):
            _, v1 = self._plan()
            _, v2 = self._plan()

        assert v1 == 1
        assert v2 == 2

    def test_progress_callback(self):
        calls = []

        with _patch_gemini(_fake_edl_json(self.photo_paths)):
            self._plan()
            from pipeline.plan import plan

            plan(
                self.cfg,
                PlanConfig(target_duration=60),
                progress_callback=lambda done, total, detail: calls.append(
                    (done, total, detail)
                ),
            )

        assert len(calls) >= 2
        dones = [c[0] for c in calls]
        assert dones == sorted(dones)
        assert calls[-1][0] == calls[-1][1]
