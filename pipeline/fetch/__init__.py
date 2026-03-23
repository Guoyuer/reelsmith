"""Fetch stage: download from NAS or scan local folder."""

from ._nas import fetch
from ._local import fetch_local

__all__ = ["fetch", "fetch_local"]
