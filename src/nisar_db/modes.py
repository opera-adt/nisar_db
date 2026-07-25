"""Shared NISAR acquisition-mode constants and voting helpers.

A NISAR ``mode`` is a 4-character code (e.g. ``"4005"``); its *family* is the
first two characters (``"40"``, ``"20"``, ...). The consistent-GSLC selection
and the file-list catalog both need to pick the dominant mode/family per
(track, frame), preferring the standard science modes over engineering or test
modes.
"""

from __future__ import annotations

import pandas as pd

# Standard science-mode codes (full 4-char mode).
STANDARD_MODES = {"4005", "2005"}

# Standard mode families (first 2 chars).
STANDARD_FAMILIES = {"40", "20"}


def dominant_value(series: pd.Series, standard: set[str]) -> str:
    """Return the most frequent value, restricted to ``standard`` when present.

    Standard-family/mode values vote first; only if the group has *no* standard
    values does the overall most-frequent value win. Ties break on
    ``value_counts`` order (first-seen among the max).

    Parameters
    ----------
    series : pd.Series
        Values to vote over (e.g. ``mode`` or ``mode_family``).
    standard : set of str
        The subset considered "standard" (:data:`STANDARD_MODES` or
        :data:`STANDARD_FAMILIES`).

    """
    standard_votes = series[series.isin(standard)]
    votes = standard_votes if not standard_votes.empty else series
    return votes.value_counts().index[0]
