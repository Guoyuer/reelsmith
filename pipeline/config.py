from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class Config:
    api_base: str = "http://localhost:8000"
    anthropic_api_key: str = ""
    ollama_base: str = "http://localhost:11434"
    vision_model: str = "llava:7b"
    whisper_model: str = "small"
    workspace: Path = Path("./workspace")

    @classmethod
    def load(cls, workspace: str | None = None) -> Config:
        load_dotenv()
        cfg = cls(
            api_base=os.getenv("SYNOLOGY_API_BASE", cls.api_base),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            ollama_base=os.getenv("OLLAMA_BASE", cls.ollama_base),
            vision_model=os.getenv("VISION_MODEL", cls.vision_model),
            whisper_model=os.getenv("WHISPER_MODEL", cls.whisper_model),
            workspace=Path(workspace or os.getenv("WORKSPACE", "./workspace")),
        )
        return cfg

    def ensure_dirs(self) -> None:
        for sub in ["raw", "keyframes", "clips", "output"]:
            (self.workspace / sub).mkdir(parents=True, exist_ok=True)
