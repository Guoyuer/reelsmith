"""Pipeline configuration — workspace paths, shared directories, environment loading."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class Config:
    workspace: Path = Path("./workspace")
    api_base: str = "http://localhost:8000"

    @property
    def _base(self) -> Path:
        """Shared root: workspace/../.. if in runs/xxx, else workspace itself."""
        return self.workspace.parent.parent if self.workspace.parent.name == "runs" else self.workspace

    # Shared directories (across all runs)
    @property
    def media_dir(self) -> Path:
        return self._base / "media"

    @property
    def cache_dir(self) -> Path:
        return self._base / "analysis_cache"

    @property
    def thumbnails_dir(self) -> Path:
        return self._base / "thumbnails"

    @property
    def preview_clips_dir(self) -> Path:
        return self._base / "preview_clips"

    @property
    def heic_converted_dir(self) -> Path:
        return self._base / "heic_converted"

    @property
    def music_dir(self) -> Path:
        return self._base / "music"

    # Per-run directories and files
    @property
    def clips_dir(self) -> Path:
        return self.workspace / "clips"

    @property
    def output_dir(self) -> Path:
        return self.workspace / "output"

    @property
    def manifest_path(self) -> Path:
        return self.workspace / "manifest.json"

    @property
    def analysis_path(self) -> Path:
        return self.workspace / "analysis.json"

    @property
    def preprocessed_path(self) -> Path:
        return self.workspace / "preprocessed.json"

    def edl_path(self, version: int) -> Path:
        return self.workspace / f"edl_v{version}.json"

    @staticmethod
    def run_workspace(base_dir: str = "./workspace", run_name: str = "default") -> str:
        """Canonical path for a run's workspace directory."""
        return str(Path(base_dir) / "runs" / run_name)

    @classmethod
    def load(cls, workspace: str | None = None) -> Config:
        load_dotenv()
        ws = Path(workspace or os.getenv("WORKSPACE", "./workspace"))
        return cls(
            workspace=ws,
            api_base=os.getenv("SYNOLOGY_API_BASE", cls.api_base),
        )

    def ensure_dirs(self) -> None:
        for d in [
            self.clips_dir,
            self.output_dir,
            self.media_dir,
            self.cache_dir,
            self.thumbnails_dir,
            self.preview_clips_dir,
            self.heic_converted_dir,
            self.music_dir,
        ]:
            d.mkdir(parents=True, exist_ok=True)
