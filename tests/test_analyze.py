"""Tests for pipeline.analyze — vision model analysis with tiering and caching."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from pipeline.analyze import (
    VISION_PROMPT_FAMILY,
    VISION_PROMPT_SCENE,
    analyze,
)
from pipeline.media_utils import extract_frames


def _make_tiny_image(path: Path, size=(160, 90)) -> Path:
    """Create a tiny JPEG at the given path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, color=(100, 150, 200))
    img.save(path, "JPEG")
    return path


def _write_preprocessed(cfg: Config, items: list[dict]) -> None:
    """Write a preprocessed.json file to the workspace."""
    data = {
        "family_names": ["Alice", "Bob"],
        "total_items": len(items),
        "selected_items": len(items),
        "tier_counts": {},
        "items": items,
        "timeline": [],
    }
    (cfg.workspace / "preprocessed.json").write_text(json.dumps(data))


def _make_item(item_id: int, tier: str, filename: str, local_path: str,
               family_count: int = 0, **extra) -> dict:
    """Build a preprocessed item dict."""
    item = {
        "id": item_id,
        "tier": tier,
        "filename": filename,
        "local_path": local_path,
        "family_count": family_count,
        "item_type": 0,
    }
    item.update(extra)
    return item


FAKE_VISION = {"description": "test scene", "visual_quality": 8, "vlog_worthy": True}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAnalyzeSkipsTierD:
    def test_analyze_skips_tier_d(self, tmp_path: Path, mock_config):
        """Tier D items should not be analyzed."""
        cfg = mock_config
        img = _make_tiny_image(cfg.media_dir / "100_photo.jpg")

        items = [
            _make_item(100, "D", "photo.jpg", str(img)),
        ]
        _write_preprocessed(cfg, items)

        with patch("pipeline.analyze._analyze_image", return_value=FAKE_VISION):
            results = analyze(cfg)

        # Tier D items should not appear in results
        assert len(results) == 0


class TestAnalyzeResumesFromExisting:
    def test_analyze_resumes_from_existing(self, tmp_path: Path, mock_config):
        """Items with vision data in analysis.json should be skipped on resume."""
        cfg = mock_config
        img = _make_tiny_image(cfg.media_dir / "101_photo.jpg")

        items = [
            _make_item(101, "A", "photo.jpg", str(img), family_count=2),
        ]
        _write_preprocessed(cfg, items)

        # Pre-write analysis.json with vision data
        existing = [{
            "id": 101,
            "filename": "photo.jpg",
            "local_path": str(img),
            "vision": {"description": "already analyzed"},
        }]
        (cfg.workspace / "analysis.json").write_text(json.dumps(existing))

        with patch("pipeline.analyze._analyze_image") as mock_analyze:
            results = analyze(cfg)

        # _analyze_image should not have been called
        mock_analyze.assert_not_called()
        assert len(results) == 1
        assert results[0]["vision"]["description"] == "already analyzed"


class TestAnalyzeUsesSharedCache:
    def test_analyze_uses_shared_cache(self, tmp_path: Path, mock_config):
        """If cache_dir/{id}.json exists, vision call should be skipped."""
        cfg = mock_config
        img = _make_tiny_image(cfg.media_dir / "102_photo.jpg")

        items = [
            _make_item(102, "B", "photo.jpg", str(img), family_count=1),
        ]
        _write_preprocessed(cfg, items)

        # Write shared cache entry
        cache_entry = {"vision": {"description": "cached result", "visual_quality": 9}}
        (cfg.cache_dir / "102.json").write_text(json.dumps(cache_entry))

        with patch("pipeline.analyze._analyze_image") as mock_analyze:
            results = analyze(cfg)

        mock_analyze.assert_not_called()
        assert len(results) == 1
        assert results[0]["vision"]["description"] == "cached result"


class TestAnalyzeSavesToSharedCache:
    def test_analyze_saves_to_shared_cache(self, tmp_path: Path, mock_config):
        """After analysis, cache_dir/{id}.json should be written."""
        cfg = mock_config
        img = _make_tiny_image(cfg.media_dir / "103_photo.jpg")

        items = [
            _make_item(103, "A", "photo.jpg", str(img), family_count=2),
        ]
        _write_preprocessed(cfg, items)

        with patch("pipeline.analyze._analyze_image", return_value=FAKE_VISION):
            analyze(cfg)

        cache_file = cfg.cache_dir / "103.json"
        assert cache_file.exists()
        cached = json.loads(cache_file.read_text())
        assert "vision" in cached


class TestAnalyzeWritesIncrementally:
    def test_analyze_writes_incrementally(self, tmp_path: Path, mock_config):
        """analysis.json should be written after each item."""
        cfg = mock_config

        img1 = _make_tiny_image(cfg.media_dir / "104_a.jpg")
        img2 = _make_tiny_image(cfg.media_dir / "105_b.jpg")

        items = [
            _make_item(104, "A", "a.jpg", str(img1), family_count=2),
            _make_item(105, "B", "b.jpg", str(img2), family_count=1),
        ]
        _write_preprocessed(cfg, items)

        analysis_path = cfg.workspace / "analysis.json"
        write_counts = []

        original_write_text = Path.write_text

        def tracking_write(self_path, content, *args, **kwargs):
            original_write_text(self_path, content, *args, **kwargs)
            if self_path == analysis_path:
                data = json.loads(content)
                write_counts.append(len(data))

        with patch("pipeline.analyze._analyze_image", return_value=FAKE_VISION), \
             patch.object(Path, "write_text", tracking_write):
            analyze(cfg)

        # analysis.json should be written at least twice (once per item)
        assert len(write_counts) >= 2
        # First write has 1 item, second has 2
        assert write_counts[0] == 1
        assert write_counts[1] == 2


class TestAnalyzeFamilyPromptForTierA:
    def test_analyze_family_prompt_for_tier_a(self, tmp_path: Path, mock_config):
        """Tier A should get VISION_PROMPT_FAMILY."""
        cfg = mock_config
        img = _make_tiny_image(cfg.media_dir / "106_photo.jpg")

        items = [
            _make_item(106, "A", "photo.jpg", str(img), family_count=2),
        ]
        _write_preprocessed(cfg, items)

        with patch("pipeline.analyze._analyze_image", return_value=FAKE_VISION) as mock_analyze:
            analyze(cfg)

        # Check the prompt argument
        assert mock_analyze.call_count == 1
        _, kwargs = mock_analyze.call_args
        # _analyze_image is called with positional args: (image_path, cfg, prompt)
        args = mock_analyze.call_args[0]
        prompt_used = args[2]
        assert prompt_used == VISION_PROMPT_FAMILY


class TestAnalyzeScenePromptForTierC:
    def test_analyze_scene_prompt_for_tier_c(self, tmp_path: Path, mock_config):
        """Tier C should get VISION_PROMPT_SCENE."""
        cfg = mock_config
        img = _make_tiny_image(cfg.media_dir / "107_photo.jpg")

        items = [
            _make_item(107, "C", "photo.jpg", str(img), family_count=0),
        ]
        _write_preprocessed(cfg, items)

        with patch("pipeline.analyze._analyze_image", return_value=FAKE_VISION) as mock_analyze:
            analyze(cfg)

        assert mock_analyze.call_count == 1
        args = mock_analyze.call_args[0]
        prompt_used = args[2]
        assert prompt_used == VISION_PROMPT_SCENE


class TestAnalyzeHeicConvertsViaSips:
    def test_analyze_heic_converts_via_sips(self, tmp_path: Path, mock_config):
        """A .heic file should trigger a sips subprocess call inside _analyze_image."""
        cfg = mock_config

        # Create a fake .heic file (we will mock _analyze_image but check sips
        # is called by NOT patching _analyze_image — instead patch the lower
        # level subprocess.run and httpx.post)
        heic_path = cfg.media_dir / "108_photo.heic"
        # Write a minimal JPEG as the file content (sips will be mocked anyway)
        img = Image.new("RGB", (160, 90), color=(50, 50, 50))
        img.save(heic_path, "JPEG")  # save as JPEG bytes but with .heic extension

        items = [
            _make_item(108, "A", "photo.heic", str(heic_path), family_count=2),
        ]
        _write_preprocessed(cfg, items)

        sips_calls = []

        def mock_subprocess_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            if cmd[0] == "sips":
                sips_calls.append(cmd)
                # Create the output jpeg
                out_path = Path(cmd[-1])
                jpeg_img = Image.new("RGB", (160, 90), color=(50, 50, 50))
                jpeg_img.save(out_path, "JPEG")
            return result

        with patch("pipeline.analyze.run_subprocess", side_effect=mock_subprocess_run), \
             patch("pipeline.media_utils.run_subprocess", side_effect=mock_subprocess_run), \
             patch("pipeline.analyze.ollama_json", return_value=FAKE_VISION):
            analyze(cfg)

        assert len(sips_calls) >= 1
        assert sips_calls[0][0] == "sips"
        assert sips_calls[0][3] == "jpeg"


class TestAnalyzeProgressCallback:
    def test_analyze_progress_callback(self, tmp_path: Path, mock_config):
        """Callback should be called with (current, total, filename)."""
        cfg = mock_config
        img1 = _make_tiny_image(cfg.media_dir / "109_a.jpg")
        img2 = _make_tiny_image(cfg.media_dir / "110_b.jpg")

        items = [
            _make_item(109, "A", "a.jpg", str(img1), family_count=2),
            _make_item(110, "B", "b.jpg", str(img2), family_count=1),
        ]
        _write_preprocessed(cfg, items)

        callback_args = []

        def on_progress(current, total, filename):
            callback_args.append((current, total, filename))

        with patch("pipeline.analyze._analyze_image", return_value=FAKE_VISION):
            analyze(cfg, progress_callback=on_progress)

        assert len(callback_args) == 2
        # Each call should have (current_int, total_int, filename_str)
        for current, total, filename in callback_args:
            assert isinstance(current, int)
            assert isinstance(total, int)
            assert isinstance(filename, str)
        # Total should be 2 for both calls
        assert callback_args[0][1] == 2
        assert callback_args[1][1] == 2


class TestExtractKeyframes:
    def test_extract_keyframes(self, tmp_path: Path, mock_config):
        """Should call ffprobe then ffmpeg."""
        video_path = tmp_path / "video.mp4"
        video_path.write_bytes(b"\x00" * 100)
        kf_dir = tmp_path / "keyframes"

        calls = []

        def mock_run(cmd, **kwargs):
            calls.append(cmd[0])
            result = MagicMock()
            result.returncode = 0
            result.stdout = "10.0\n"
            result.stderr = ""
            return result

        with patch("pipeline.media_utils.run_subprocess", side_effect=mock_run):
            extract_frames(video_path, kf_dir, prefix="999", count=5)

        assert "ffprobe" in calls
        assert "ffmpeg" in calls
        assert calls.index("ffprobe") < calls.index("ffmpeg")
