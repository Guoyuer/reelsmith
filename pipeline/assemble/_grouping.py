"""Clip grouping logic — shared between concat and timeline modules."""

from __future__ import annotations

MAX_GROUP = 10


def partition_into_groups(n: int, get_transition) -> list[list[int]]:
    """Partition clip indices into groups of <= MAX_GROUP.

    Single source of truth — used by both concat and timeline to ensure
    identical group boundaries (drift causes speech desync).

    Args:
        n: total number of clips
        get_transition: callable(i) -> transition name for clip i
    """
    if n == 0:
        return []
    groups: list[list[int]] = [[0]]
    for i in range(1, n):
        # Split at MAX_GROUP, or split early at fade_black boundaries
        # (fade_black = chapter change, clean split point for xfade chains)
        should_split = (
            len(groups[-1]) >= MAX_GROUP
            or (len(groups[-1]) >= MAX_GROUP - 3
                and get_transition(i) == "fade_black")
        )
        if should_split:
            groups.append([])
        groups[-1].append(i)
    return groups
