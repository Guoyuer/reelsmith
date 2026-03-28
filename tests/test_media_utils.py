"""Tests for pipeline.utils.media and pipeline.utils.image."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import pipeline.utils.image as _img_utils
from pipeline.utils.image import convert_heic
from pipeline.utils.media import probe_duration, run_subprocess, strip_markdown_fences


@pytest.fixture(autouse=True)
def _reset_heic_dir():
    """Ensure tests use source.parent, not a stale global dir."""
    _img_utils._heic_dest_dir = None


# ---------------------------------------------------------------------------
# strip_markdown_fences
# ---------------------------------------------------------------------------


class TestStripMarkdownFences:
    @pytest.mark.parametrize(
        "input_text, expected_substring, no_fences",
        [
            ('```json\n{"key": "value"}\n```', '{"key": "value"}', True),
            ('{"key": "value"}', '{"key": "value"}', True),
            ('  {"key": "value"}  ', '{"key": "value"}', True),
        ],
    )
    def test_strips_fences_and_whitespace(
        self, input_text, expected_substring, no_fences
    ):
        result = strip_markdown_fences(input_text)
        assert expected_substring in result
        if no_fences:
            assert not result.startswith("```")


# ---------------------------------------------------------------------------
# convert_heic
# ---------------------------------------------------------------------------


class TestConvertHeic:
    def test_calls_sips_when_pillow_heif_unavailable(self, tmp_path: Path):
        """convert_heic should fall back to sips when pillow-heif fails."""
        heic_file = tmp_path / "photo.heic"
        heic_file.write_bytes(b"\x00" * 100)
        cache_dir = tmp_path / "cache"

        calls = []

        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            result = MagicMock()
            result.returncode = 0
            if cmd[0] == "sips":
                out_path = Path(cmd[-1])
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(b"\xff\xd8" + b"\x00" * 50)
            return result

        import sys

        saved = sys.modules.pop("pillow_heif", None)
        try:
            with (
                patch(
                    "shutil.which",
                    side_effect=lambda x: "/usr/bin/sips" if x == "sips" else None,
                ),
                patch("pipeline.utils.image.run_subprocess", side_effect=mock_run),
                patch.dict("sys.modules", {"pillow_heif": None}),
            ):
                jpeg = convert_heic(heic_file, cache_dir=cache_dir)
        finally:
            if saved is not None:
                sys.modules["pillow_heif"] = saved

        sips_calls = [c for c in calls if c[0] == "sips"]
        assert len(sips_calls) == 1
        assert sips_calls[0][3] == "jpeg"
        assert jpeg.suffix == ".jpg"
        assert jpeg.exists()

    def test_skips_existing_jpeg(self, tmp_path: Path):
        """If the JPEG already exists, sips should not be called."""
        heic_file = tmp_path / "photo.heic"
        heic_file.write_bytes(b"\x00" * 100)

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        jpeg_path = cache_dir / f"_converted_{heic_file.stem}.jpg"
        jpeg_path.write_bytes(b"\xff\xd8" + b"\x00" * 50)

        with patch("pipeline.utils.image.run_subprocess") as mock_run:
            result = convert_heic(heic_file, cache_dir=cache_dir)

        mock_run.assert_not_called()
        assert result == jpeg_path


# ---------------------------------------------------------------------------
# run_subprocess
# ---------------------------------------------------------------------------


class TestRunSubprocess:
    def test_basic_command(self):
        result = run_subprocess(
            ["python", "-c", "print('hello')"], capture_output=True, text=True
        )
        assert result.returncode == 0
        assert "hello" in result.stdout

    def test_timeout_returns_code_1(self):
        result = run_subprocess(
            ["python", "-c", "import time; time.sleep(10)"],
            timeout=1,
            capture_output=True,
        )
        assert result.returncode == 1

    def test_nonexistent_command_raises(self):
        with pytest.raises(FileNotFoundError):
            run_subprocess(["nonexistent_binary_xyz"], capture_output=True)


# ---------------------------------------------------------------------------
# probe_duration
# ---------------------------------------------------------------------------


class TestProbeDuration:
    @pytest.mark.parametrize(
        "stdout, expected",
        [
            ("123.45\n", 123.45),
            ("\n", 0.0),
            ("not a number\n", 0.0),
        ],
    )
    def test_probe_duration(self, stdout, expected):
        fake = MagicMock()
        fake.stdout = stdout
        with patch("pipeline.utils.media.run_subprocess", return_value=fake):
            assert probe_duration(Path("/fake.mp4")) == expected
