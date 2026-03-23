"""Fetch stage: download from NAS or scan local folder."""

from ._local import fetch_local
from ._nas import FetchConfig, fetch

__all__ = ["fetch", "fetch_local", "FetchConfig"]
