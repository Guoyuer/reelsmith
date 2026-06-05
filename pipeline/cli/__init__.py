"""CLI package for the reelsmith pipeline."""

# Register the workspace command (side-effect import)
from . import _workspace  # noqa: F401
from ._commands import cli

__all__ = ["cli"]
