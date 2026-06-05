"""Tests for pipeline.utils.media and pipeline.utils.image."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline.utils.media import probe_duration, run_subprocess
from tests.helpers import subprocess_result

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
        fake = subprocess_result(stdout=stdout)
        with patch("pipeline.utils.media.run_subprocess", return_value=fake):
            assert probe_duration(Path("/fake.mp4")) == expected
