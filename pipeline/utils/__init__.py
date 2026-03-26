"""Cross-cutting utilities shared across pipeline stages."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.console import Console


def stderr_console() -> Console | None:
    """Return a Rich Console writing to stderr, or None if not a TTY.

    This centralises the ``isatty()`` + optional-import guard that was
    previously duplicated across many modules.
    """
    if not sys.stderr.isatty():
        return None
    try:
        from rich.console import Console

        return Console(stderr=True)
    except ImportError:
        return None
