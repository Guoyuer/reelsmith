"""Shared fixtures for vlog pipeline tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from dotenv import load_dotenv
from PIL import Image

load_dotenv()

from pipeline.config import Config
from pipeline.edl import EDL, EditItem, MusicTrack, Segment


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


@pytest.fixture
def sample_manifest() -> list[dict]:
    """List of 6 items with mixed types, varied takentimes, person metadata."""
    base_time = 1700000000  # 2023-11-14 ~14:13 UTC
    return [
        {
            "id": 1,
            "filename": "IMG_001.jpg",
            "item_type": 0,
            "takentime": base_time,
            "filesize": 5000000,
            "district": "Marina Bay",
            "country": "Singapore",
            "metadata": {"persons": ["Alice", "Bob"]},
        },
        {
            "id": 2,
            "filename": "IMG_002.jpg",
            "item_type": 0,
            "takentime": base_time + 5,
            "filesize": 4000000,
            "district": "Marina Bay",
            "country": "Singapore",
            "metadata": {"persons": ["Alice"]},
        },
        {
            "id": 3,
            "filename": "VID_003.mp4",
            "item_type": 1,
            "takentime": base_time + 20,
            "filesize": 30000000,
            "district": "Chinatown",
            "country": "Singapore",
            "metadata": {"persons": []},
        },
        {
            "id": 4,
            "filename": "IMG_004.jpg",
            "item_type": 0,
            "takentime": base_time + 30,
            "filesize": 6000000,
            "metadata": {"persons": []},
        },
        {
            "id": 5,
            "filename": "Screenshot_20231114.png",
            "item_type": 3,
            "takentime": base_time + 40,
            "filesize": 1000000,
            "metadata": {"persons": []},
        },
        {
            "id": 6,
            "filename": "IMG_006.jpg",
            "item_type": 0,
            "takentime": base_time + 86400,  # next day
            "filesize": 7000000,
            "district": "Orchard",
            "country": "Singapore",
            "metadata": {"persons": ["Alice", "Bob", "Charlie"]},
        },
    ]


@pytest.fixture
def sample_preprocessed() -> dict:
    """Preprocessed data with family names, tiered items, 2-day timeline."""
    return {
        "family_names": ["Alice", "Bob"],
        "total_items": 6,
        "selected_items": 5,
        "tier_counts": {"A": 2, "B": 1, "C": 1, "D": 1},
        "items": [
            {"id": 1, "tier": "A", "family_count": 2, "filename": "IMG_001.jpg"},
            {"id": 2, "tier": "B", "family_count": 1, "filename": "IMG_002.jpg"},
            {"id": 3, "tier": "C", "family_count": 0, "filename": "VID_003.mp4"},
            {"id": 5, "tier": "D", "family_count": 0, "filename": "Screenshot_20231114.png"},
            {"id": 6, "tier": "A", "family_count": 3, "filename": "IMG_006.jpg"},
        ],
        "timeline": [
            {
                "date": "2023-11-14",
                "day_name": "Tuesday",
                "chapters": [
                    {
                        "time_block": "afternoon",
                        "location": "Marina Bay",
                        "item_ids": [1, 2],
                        "count": 2,
                        "family_together": 1,
                    },
                    {
                        "time_block": "afternoon",
                        "location": "Chinatown",
                        "item_ids": [3],
                        "count": 1,
                        "family_together": 0,
                    },
                ],
                "total_items": 3,
            },
            {
                "date": "2023-11-15",
                "day_name": "Wednesday",
                "chapters": [
                    {
                        "time_block": "afternoon",
                        "location": "Orchard",
                        "item_ids": [6],
                        "count": 1,
                        "family_together": 1,
                    },
                ],
                "total_items": 1,
            },
        ],
    }


@pytest.fixture
def sample_edl() -> EDL:
    """EDL with 2 segments, 3 items each."""
    return EDL(
        title="Singapore Trip",
        target_duration=120.0,
        segments=[
            Segment(
                name="Marina Bay",
                items=[
                    EditItem(source_file="IMG_001.jpg", media_type="photo", display_duration=4.0),
                    EditItem(source_file="IMG_002.jpg", media_type="photo", display_duration=3.0),
                    EditItem(source_file="VID_003.mp4", media_type="video", display_duration=5.0,
                             start_time=0.0, end_time=5.0),
                ],
                transition="crossfade",
                transition_duration=0.8,
            ),
            Segment(
                name="Chinatown",
                items=[
                    EditItem(source_file="IMG_004.jpg", media_type="photo", display_duration=4.0),
                    EditItem(source_file="IMG_005.jpg", media_type="photo", display_duration=3.5),
                    EditItem(source_file="VID_006.mp4", media_type="video", display_duration=6.0,
                             start_time=2.0, end_time=8.0),
                ],
                transition="cut",
                transition_duration=0.5,
            ),
        ],
        music=MusicTrack(file="bg_music.mp3", volume=0.15),
    )


@pytest.fixture
def mock_config(tmp_path: Path) -> Config:
    """Config with fake URLs pointed at tmp_path, directories created."""
    cfg = Config(
        api_base="http://fake:8000",
        workspace=tmp_path / "workspace",
        media_dir=tmp_path / "workspace" / "media",
        cache_dir=tmp_path / "workspace" / "analysis_cache",
        keyframes_dir=tmp_path / "workspace" / "keyframes",
    )
    cfg.ensure_dirs()
    return cfg
