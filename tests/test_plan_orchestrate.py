"""Tests for pipeline.plan._orchestrate — plan() and _plan_visual() with mocked Gemini."""

from __future__ import annotations

import json
from unittest.mock import patch

from pipeline.config import Config
from pipeline.plan import PlanConfig

FAKE_EDL_JSON = json.dumps(
    {
        "title": "Test Trip",
        "target_duration": 60,
        "segments": [
            {
                "name": "Morning Stroll",
                "music_mood": "gentle acoustic",
                "items": [
                    {
                        "source_file": "PLACEHOLDER",
                        "media_type": "photo",
                        "display_duration": 4.0,
                    },
                    {
                        "source_file": "PLACEHOLDER2",
                        "media_type": "photo",
                        "display_duration": 3.0,
                    },
                ],
            }
        ],
    }
)


def _setup_workspace(tmp_path, n_photos=2):
    """Create a minimal workspace with preprocessed data and photos."""
    cfg = Config(workspace=tmp_path)
    cfg.ensure_dirs()
    cfg.media_dir.mkdir(parents=True, exist_ok=True)

    # Create photos and thumbnails
    photo_paths = []
    for i in range(1, n_photos + 1):
        photo = cfg.media_dir / f"photo_{i}.jpg"
        photo.write_bytes(b"\xff\xd8" + b"\x00" * 100)
        thumb = cfg.thumbnails_dir / f"photo_{i}_thumb.jpg"
        thumb.write_bytes(b"\xff\xd8" + b"\x00" * 50)
        photo_paths.append(str(photo))

    # Create manifest.json + per-item caches (replaces old analysis.json)
    manifest = [
        {
            "id": i,
            "filename": f"photo_{i}.jpg",
            "local_path": photo_paths[i - 1],
            "metadata": {"persons": ["Alice"]},
            "family_count": 1,
            "taken_iso": f"2025-06-13T{10 + i}:30:00",
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

    # Create preprocessed.json
    preprocessed = {
        "family_names": ["Alice"],
    }
    cfg.preprocessed_path.write_text(json.dumps(preprocessed))

    return cfg, photo_paths


def _patch_gemini(return_value):
    """Patch _gemini_call in _orchestrate."""
    return patch("pipeline.plan._orchestrate._gemini_call", return_value=return_value)


class TestPlanOrchestration:
    def test_plan_creates_edl_file(self, tmp_path, monkeypatch):
        """plan() should create edl_v1.json in workspace."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        cfg, photo_paths = _setup_workspace(tmp_path)

        # Build fake EDL response with actual photo paths
        fake_edl = {
            "title": "Test Trip",
            "target_duration": 60,
            "segments": [
                {
                    "name": "Morning",
                    "music_mood": "gentle",
                    "items": [
                        {
                            "source_file": photo_paths[0],
                            "media_type": "photo",
                            "display_duration": 30.0,
                        },
                        {
                            "source_file": photo_paths[1],
                            "media_type": "photo",
                            "display_duration": 40.0,
                        },
                    ],
                }
            ],
        }

        with _patch_gemini(json.dumps(fake_edl)):
            from pipeline.plan import plan

            edl, version = plan(cfg, PlanConfig(target_duration=60))

        assert version == 1
        assert cfg.edl_path(1).exists()
        assert edl.title == "Test Trip"
        assert len(edl.all_items()) == 2

    def test_plan_forces_video_effect_none(self, tmp_path, monkeypatch):
        """plan() should set effect='none' on all video items."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        cfg, photo_paths = _setup_workspace(tmp_path)

        # Create a .mp4 file so validate_and_fix_edl doesn't auto-correct to photo
        video_file = cfg.media_dir / "clip.mp4"
        video_file.write_bytes(b"\x00" * 200)

        fake_edl = {
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

        with _patch_gemini(json.dumps(fake_edl)):
            from pipeline.plan import plan

            edl, _ = plan(cfg, PlanConfig(target_duration=60))

        assert edl.all_items()[0].effect == "none"

    def test_plan_sets_metadata(self, tmp_path, monkeypatch):
        """plan() should set trip_type, style, language on the EDL."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        cfg, photo_paths = _setup_workspace(tmp_path)

        fake_edl = {
            "title": "T",
            "target_duration": 60,
            "segments": [
                {
                    "name": "S",
                    "music_mood": "m",
                    "items": [
                        {
                            "source_file": photo_paths[0],
                            "media_type": "photo",
                            "display_duration": 30.0,
                        },
                        {
                            "source_file": photo_paths[1],
                            "media_type": "photo",
                            "display_duration": 40.0,
                        },
                    ],
                }
            ],
        }

        with _patch_gemini(json.dumps(fake_edl)):
            from pipeline.plan import plan

            edl, _ = plan(
                cfg,
                PlanConfig(
                    target_duration=60,
                    style="cinematic",
                    trip_type="solo",
                    language="cn",
                ),
            )

        assert edl.style == "cinematic"
        assert edl.trip_type == "solo"
        assert edl.language == "cn"

    def test_plan_increments_version(self, tmp_path, monkeypatch):
        """Second plan() call should create v2."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        cfg, photo_paths = _setup_workspace(tmp_path)

        fake_edl = {
            "title": "T",
            "target_duration": 60,
            "segments": [
                {
                    "name": "S",
                    "music_mood": "m",
                    "items": [
                        {
                            "source_file": photo_paths[0],
                            "media_type": "photo",
                            "display_duration": 30.0,
                        },
                        {
                            "source_file": photo_paths[1],
                            "media_type": "photo",
                            "display_duration": 40.0,
                        },
                    ],
                }
            ],
        }

        with _patch_gemini(json.dumps(fake_edl)):
            from pipeline.plan import plan

            _, v1 = plan(cfg, PlanConfig(target_duration=60))
            _, v2 = plan(cfg, PlanConfig(target_duration=60))

        assert v1 == 1
        assert v2 == 2

    def test_plan_progress_callback(self, tmp_path, monkeypatch):
        """plan() should call progress_callback at milestones."""
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        cfg, photo_paths = _setup_workspace(tmp_path)

        fake_edl = {
            "title": "T",
            "target_duration": 60,
            "segments": [
                {
                    "name": "S",
                    "music_mood": "m",
                    "items": [
                        {
                            "source_file": photo_paths[0],
                            "media_type": "photo",
                            "display_duration": 30.0,
                        },
                        {
                            "source_file": photo_paths[1],
                            "media_type": "photo",
                            "display_duration": 40.0,
                        },
                    ],
                }
            ],
        }

        calls = []

        def cb(done, total, detail):
            calls.append((done, total, detail))

        with _patch_gemini(json.dumps(fake_edl)):
            from pipeline.plan import plan

            plan(cfg, PlanConfig(target_duration=60), progress_callback=cb)

        assert len(calls) >= 2  # at least content-ready + done
        # Verify progress is monotonically increasing
        dones = [c[0] for c in calls]
        assert dones == sorted(dones)
        # Last call should have done == total
        assert calls[-1][0] == calls[-1][1]
