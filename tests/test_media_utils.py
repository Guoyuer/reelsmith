"""Tests for pipeline.media_utils — shared media utility functions."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pipeline.media_utils import convert_heic, strip_markdown_fences


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

        with patch("shutil.which", side_effect=lambda x: "/usr/bin/sips" if x == "sips" else None), \
             patch("pipeline.media_utils.run_subprocess", side_effect=mock_run), \
             patch.dict("sys.modules", {"pillow_heif": None}):
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

        with patch("pipeline.media_utils.run_subprocess") as mock_run:
            result = convert_heic(heic_file)

        mock_run.assert_not_called()
        assert result == jpeg_path
