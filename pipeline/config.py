from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class Config:
    api_base: str = "http://localhost:8000"
    workspace: Path = Path("./workspace")
    # Shared directories (across all runs)
    media_dir: Path = Path("./workspace/media")
    cache_dir: Path = Path("./workspace/analysis_cache")
    thumbnails_dir: Path = Path("./workspace/thumbnails")
    contact_sheets_dir: Path = Path("./workspace/contact_sheets")
    preview_clips_dir: Path = Path("./workspace/preview_clips")
    music_dir: Path = Path("./workspace/music")

    # Per-run directories (derived from workspace)
    @property
    def clips_dir(self) -> Path:
        return self.workspace / "clips"

    @property
    def output_dir(self) -> Path:
        return self.workspace / "output"

    @staticmethod
    def run_workspace(base_dir: str = "./workspace", run_name: str = "default") -> str:
        """Canonical path for a run's workspace directory."""
        return str(Path(base_dir) / "runs" / run_name)

    @classmethod
    def load(cls, workspace: str | None = None) -> Config:
        load_dotenv()
        ws = Path(workspace or os.getenv("WORKSPACE", "./workspace"))
        # Shared dirs live at the workspace root (grandparent of run dirs)
        # If workspace is workspace/runs/myrun, shared = workspace/
        # If workspace is ./workspace, shared = ./workspace/
        base = ws.parent.parent if ws.parent.name == "runs" else ws
        cfg = cls(
            api_base=os.getenv("SYNOLOGY_API_BASE", cls.api_base),
            workspace=ws,
            media_dir=base / "media",
            cache_dir=base / "analysis_cache",
            thumbnails_dir=base / "thumbnails",
            contact_sheets_dir=base / "contact_sheets",
            preview_clips_dir=base / "preview_clips",
            music_dir=base / "music",
        )
        return cfg

    def ensure_dirs(self) -> None:
        for d in [
            self.clips_dir,
            self.output_dir,
            self.media_dir,
            self.cache_dir,
            self.thumbnails_dir,
            self.contact_sheets_dir,
            self.preview_clips_dir,
            self.music_dir,
        ]:
            d.mkdir(parents=True, exist_ok=True)
