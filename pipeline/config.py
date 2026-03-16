from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class Config:
    api_base: str = "http://localhost:8000"
    ollama_base: str = "http://localhost:11434"
    vision_model: str = "llava:13b"
    planning_model: str = "llama3:8b"
    whisper_model: str = "medium"
    workspace: Path = Path("./workspace")
    media_dir: Path = Path("./workspace/media")
    cache_dir: Path = Path("./workspace/analysis_cache")
    keyframes_dir: Path = Path("./workspace/keyframes")

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
            ollama_base=os.getenv("OLLAMA_BASE", cls.ollama_base),
            vision_model=os.getenv("VISION_MODEL", cls.vision_model),
            planning_model=os.getenv("PLANNING_MODEL", cls.planning_model),
            whisper_model=os.getenv("WHISPER_MODEL", cls.whisper_model),
            workspace=ws,
            media_dir=base / "media",
            cache_dir=base / "analysis_cache",
            keyframes_dir=base / "keyframes",
        )
        return cfg

    def ensure_dirs(self) -> None:
        for d in [
            self.workspace / "clips",
            self.workspace / "output",
            self.media_dir,
            self.cache_dir,
            self.keyframes_dir,
        ]:
            d.mkdir(parents=True, exist_ok=True)
