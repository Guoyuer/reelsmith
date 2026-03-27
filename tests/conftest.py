"""Shared fixtures for vlog pipeline tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from dotenv import load_dotenv

load_dotenv()

from pipeline.config import Config
from pipeline.edl import EDL, EditItem, MusicTrack, Segment


@pytest.fixture
def sample_manifest() -> list[dict]:
    """List of 6 items with mixed types, varied takentimes, person metadata."""
    from datetime import datetime, timezone

    base_time = 1700000000  # 2023-11-14 ~14:13 UTC

    def _item(item_id, filename, taken, **kw):
        """Build a manifest item with required fields."""
        iso = datetime.fromtimestamp(taken, tz=timezone.utc).isoformat()
        return {
            "id": item_id,
            "item_type": kw.pop("item_type", 0),
            "takentime": taken,
            "taken_iso": iso,
            "local_path": f"/fake/media/{item_id}_{filename}",
            "filesize": kw.pop("filesize", 5000000),
            **kw,
        }

    return [
        _item(
            1,
            "IMG_001.jpg",
            base_time,
            district="Marina Bay",
            country="Singapore",
        ),
        _item(
            2,
            "IMG_002.jpg",
            base_time + 5,
            district="Marina Bay",
            country="Singapore",
            filesize=4000000,
        ),
        _item(
            3,
            "VID_003.mp4",
            base_time + 20,
            item_type=1,
            district="Chinatown",
            country="Singapore",
            filesize=30000000,
        ),
        _item(4, "IMG_004.jpg", base_time + 30, filesize=6000000),
        _item(5, "Screenshot_20231114.png", base_time + 40, filesize=1000000),
        _item(
            6,
            "IMG_006.jpg",
            base_time + 86400,
            district="Orchard",
            country="Singapore",
            filesize=7000000,
        ),
    ]


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
                    EditItem(
                        source_file="IMG_001.jpg",
                        media_type="photo",
                        display_duration=4.0,
                    ),
                    EditItem(
                        source_file="IMG_002.jpg",
                        media_type="photo",
                        display_duration=3.0,
                    ),
                    EditItem(
                        source_file="VID_003.mp4",
                        media_type="video",
                        display_duration=5.0,
                        start_time=0.0,
                        end_time=5.0,
                    ),
                ],
                transition="crossfade",
                transition_duration=0.8,
            ),
            Segment(
                name="Chinatown",
                items=[
                    EditItem(
                        source_file="IMG_004.jpg",
                        media_type="photo",
                        display_duration=4.0,
                    ),
                    EditItem(
                        source_file="IMG_005.jpg",
                        media_type="photo",
                        display_duration=3.5,
                    ),
                    EditItem(
                        source_file="VID_006.mp4",
                        media_type="video",
                        display_duration=6.0,
                        start_time=2.0,
                        end_time=8.0,
                    ),
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
        workspace=tmp_path / "workspace",
    )
    cfg.ensure_dirs()
    cfg.media_dir.mkdir(parents=True, exist_ok=True)
    return cfg
