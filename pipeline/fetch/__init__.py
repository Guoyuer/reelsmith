"""Fetch stage: download from NAS or scan local folder."""

from ._nas import fetch, FetchConfig
from ._local import fetch_local

__all__ = ["fetch", "fetch_local", "FetchConfig"]
