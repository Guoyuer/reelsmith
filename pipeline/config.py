"""Pipeline configuration — workspace paths, shared directories, environment loading.

Configuration resolution priority (highest wins):

1. CLI explicit arguments (e.g. ``--duration 300``)
2. ``--use-cfg-file`` values (run_config_*.yaml)
3. CLI defaults (Click ``default=`` values)
4. Hardcoded defaults (``./workspace``)

See also ``_commands.py:_resolve_params()`` for CLI-level resolution.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Shared callback type: (current, total, label) → None
ProgressCallback = Callable[[int, int, str], None] | None


@dataclass
class Config:
    workspace: Path = Path("./workspace")

    @property
    def _base(self) -> Path:
        """Shared root: workspace/runs/{name} → workspace."""
        return self.workspace.parent.parent

    # Shared directories (across all runs)
    @property
    def media_dir(self) -> Path:
        return self._base / "media"

    @property
    def thumbnails_dir(self) -> Path:
        return self._base / "thumbnails"

    @property
    def preview_clips_dir(self) -> Path:
        return self._base / "preview_clips"

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

    def edl_path(self, version: int) -> Path:
        return self.workspace / f"edl_v{version}.json"

    @staticmethod
    def run_workspace(base_dir: str = "./workspace", run_name: str = "default") -> str:
        """Canonical path for a run's workspace directory."""
        return str(Path(base_dir) / "runs" / run_name)

    @classmethod
    def load(cls, workspace: str | None = None) -> Config:
        """Load config. Priority: workspace arg > default (./workspace).

        .env is loaded for GEMINI_API_KEY (used by plan/music stages).
        """
        load_dotenv()
        ws = Path(workspace or "./workspace")
        return cls(workspace=ws)

    def ensure_dirs(self) -> None:
        for d in [
            self.clips_dir,
            self.output_dir,
            self.thumbnails_dir,
            self.preview_clips_dir,
            self.music_dir,
        ]:
            d.mkdir(parents=True, exist_ok=True)
