"""Fetch stage: scan a local folder for photos and videos."""

from ._local import FetchConfig, fetch_local

__all__ = ["fetch_local", "FetchConfig"]
