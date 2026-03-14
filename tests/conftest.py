"""Shared fixtures for vlog pipeline tests."""

from pathlib import Path

import pytest
from PIL import Image


@pytest.fixture
def tiny_landscape_image(tmp_path: Path) -> Path:
    """Create a small 160x90 solid-blue landscape JPEG."""
    img = Image.new("RGB", (160, 90), color=(0, 0, 255))
    path = tmp_path / "landscape.jpg"
    img.save(path, "JPEG")
    return path


@pytest.fixture
def tiny_portrait_image(tmp_path: Path) -> Path:
    """Create a small 90x160 solid-red portrait JPEG."""
    img = Image.new("RGB", (90, 160), color=(255, 0, 0))
    path = tmp_path / "portrait.jpg"
    img.save(path, "JPEG")
    return path
