"""Assemble stage: render clips, concatenate, mix audio, validate."""

from ._assemble import ClipStatus, RenderReport, assemble

__all__ = ["assemble", "RenderReport", "ClipStatus"]
