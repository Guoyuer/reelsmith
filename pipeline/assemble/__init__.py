"""Assemble stage: render clips, concatenate, mix audio, validate."""

from ._assemble import assemble, RenderReport, ClipStatus

__all__ = ["assemble", "RenderReport", "ClipStatus"]
