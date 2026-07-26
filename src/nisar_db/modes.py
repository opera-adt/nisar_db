"""Shared NISAR acquisition-mode constants and voting helpers.

A NISAR ``mode`` is a 4-character code (e.g. ``"4005"``); its *family* is the
first two characters (``"40"``, ``"20"``, ...). The consistent-GSLC selection
and the file-list catalog both need to pick the dominant mode/family per
(track, frame), preferring the standard science modes over engineering or test
modes.

The standard modes are ordered, not just enumerated: a frame observed in both
science modes with the same coverage settles on ``"4005"``. Without an explicit
order the tie-break fell out of set iteration, so the winner changed with the
interpreter hash seed and the same catalog could produce two different
consistent databases.
"""

from __future__ import annotations

from collections.abc import Sequence

import pandas as pd

# Standard science-mode codes (full 4-char mode), most preferred first.
MODE_PRIORITY: tuple[str, ...] = ("4005", "2005")

# Standard mode families (first 2 chars), most preferred first.
FAMILY_PRIORITY: tuple[str, ...] = ("40", "20")

STANDARD_MODES = frozenset(MODE_PRIORITY)
STANDARD_FAMILIES = frozenset(FAMILY_PRIORITY)


def value_rank(value: str, priority: Sequence[str]) -> int:
    """Return the preference index of ``value``; unlisted values rank last.

    Parameters
    ----------
    value : str
        A mode (``"4005"``) or family (``"40"``) code.
    priority : sequence of str
        Preferred values, most preferred first (:data:`MODE_PRIORITY` or
        :data:`FAMILY_PRIORITY`).

    Examples
    --------
    >>> value_rank("4005", MODE_PRIORITY)
    0
    >>> value_rank("7700", MODE_PRIORITY)
    2

    """
    try:
        return priority.index(value)
    except ValueError:
        return len(priority)


def dominant_value(series: pd.Series, priority: Sequence[str]) -> str:
    """Return the most frequent value, restricted to ``priority`` when present.

    Values listed in ``priority`` vote first; only if the group has *none* of
    them does the overall most-frequent value win. Equal counts break toward the
    earlier entry in ``priority``.

    Parameters
    ----------
    series : pd.Series
        Values to vote over (e.g. ``mode`` or ``mode_family``).
    priority : sequence of str
        Preferred values, most preferred first (:data:`MODE_PRIORITY` or
        :data:`FAMILY_PRIORITY`).

    Examples
    --------
    >>> dominant_value(pd.Series(["2005", "4005"]), MODE_PRIORITY)
    '4005'

    """
    preferred_votes = series[series.isin(priority)]
    votes = preferred_votes if not preferred_votes.empty else series
    counts = votes.value_counts()
    ranked = sorted(counts.index, key=lambda v: (-counts[v], value_rank(v, priority)))
    return str(ranked[0])
