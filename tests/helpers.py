"""General-purpose test helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from PIL import Image

from pipeline.config import Config


def run_config(tmp_path: Path, *, name: str = "test", root: str = "runs") -> Config:
    """Create a Config for a temporary run workspace."""
    cfg = Config(workspace=tmp_path / root / name)
    cfg.ensure_dirs()
    return cfg


def make_jpeg(
    path: Path,
    *,
    size: tuple[int, int] = (100, 100),
    color: str | tuple[int, int, int] = "red",
) -> Path:
    """Create a small JPEG image and return its path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, "JPEG")
    return path


def make_png(
    path: Path,
    *,
    size: tuple[int, int] = (100, 100),
    color: str | tuple[int, int, int] = "red",
) -> Path:
    """Create a small PNG image and return its path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, "PNG")
    return path


def make_fake_jpeg(path: Path) -> Path:
    """Write enough JPEG-like bytes for scan/date tests that do not decode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
    return path


def make_fake_video(path: Path) -> Path:
    """Write enough MP4-like bytes for scan tests that do not decode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00\x00\x00\x1c\x66\x74\x79\x70" + b"\x00" * 100)
    return path


def subprocess_result(
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> MagicMock:
    """Create the subprocess-like object returned by mocked run_subprocess."""
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


def write_json(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path
