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


class TestStripFencesJson:
    def test_strip_fences_json(self):
        """Fences with ```json prefix should be stripped, returning inner content."""
        inner = '{"key": "value"}'
        fenced = f"```json\n{inner}\n```"
        result = strip_markdown_fences(fenced)
        # The fence prefix line is removed; content between first \n and last ``` is kept
        assert inner in result
        assert not result.startswith("```")


class TestStripFencesPlain:
    def test_strip_fences_plain(self):
        """Plain text without fences should be returned as-is (after strip)."""
        text = '{"key": "value"}'
        assert strip_markdown_fences(text) == text
        assert strip_markdown_fences(f"  {text}  ") == text


# ---------------------------------------------------------------------------
# convert_heic
# ---------------------------------------------------------------------------


class TestConvertHeicCallsSips:
    def test_convert_heic_calls_sips(self, tmp_path: Path):
        """convert_heic should fall back to sips when pillow-heif is missing."""
        heic_file = tmp_path / "photo.heic"
        heic_file.write_bytes(b"\x00" * 100)

        calls = []

        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            result = MagicMock()
            result.returncode = 0
            if cmd[0] == "sips":
                out_path = Path(cmd[-1])
                out_path.write_bytes(b"\xff\xd8" + b"\x00" * 50)
            return result

        with (
            patch(
                "shutil.which",
                side_effect=lambda x: "/usr/bin/sips" if x == "sips" else None,
            ),
            patch("pipeline.utils.image.run_subprocess", side_effect=mock_run),
            patch.dict("sys.modules", {"pillow_heif": None}),
        ):
            jpeg = convert_heic(heic_file)

        sips_calls = [c for c in calls if c[0] == "sips"]
        assert len(sips_calls) == 1
        assert sips_calls[0][3] == "jpeg"
        assert jpeg.suffix == ".jpg"
        assert jpeg.exists()


class TestConvertHeicSkipsExisting:
    def test_convert_heic_skips_existing(self, tmp_path: Path):
        """If the JPEG already exists, sips should not be called."""
        heic_file = tmp_path / "photo.heic"
        heic_file.write_bytes(b"\x00" * 100)

        # Pre-create the expected output jpeg
        jpeg_path = tmp_path / f"_converted_{heic_file.stem}.jpg"
        jpeg_path.write_bytes(b"\xff\xd8" + b"\x00" * 50)

        with patch("pipeline.utils.image.run_subprocess") as mock_run:
            result = convert_heic(heic_file)

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
        assert result.returncode == 1  # killed by timeout

    def test_nonexistent_command_raises(self):
        with pytest.raises(FileNotFoundError):
            run_subprocess(["nonexistent_binary_xyz"], capture_output=True)


# ---------------------------------------------------------------------------
# probe_duration
# ---------------------------------------------------------------------------


class TestProbeDuration:
    def test_returns_duration(self):
        fake = MagicMock()
        fake.stdout = "123.45\n"
        with patch("pipeline.utils.media.run_subprocess", return_value=fake):
            assert probe_duration(Path("/fake.mp4")) == 123.45

    def test_handles_empty_output(self):
        fake = MagicMock()
        fake.stdout = "\n"
        with patch("pipeline.utils.media.run_subprocess", return_value=fake):
            assert probe_duration(Path("/fake.mp4")) == 0.0

    def test_handles_bad_output(self):
        fake = MagicMock()
        fake.stdout = "not a number\n"
        with patch("pipeline.utils.media.run_subprocess", return_value=fake):
            assert probe_duration(Path("/fake.mp4")) == 0.0
