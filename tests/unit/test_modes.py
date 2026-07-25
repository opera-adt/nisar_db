"""Unit tests for the mode/family voting helpers."""

from __future__ import annotations

import pandas as pd

from nisar_db.modes import STANDARD_FAMILIES, STANDARD_MODES, dominant_value


def test_standard_sets() -> None:
    assert {"4005", "2005"} == STANDARD_MODES
    assert {"40", "20"} == STANDARD_FAMILIES


def test_standard_value_wins_over_more_frequent_nonstandard() -> None:
    # "7700" appears more often, but a standard mode is present and must win.
    series = pd.Series(["7700", "7700", "7700", "4005"])
    assert dominant_value(series, STANDARD_MODES) == "4005"


def test_most_frequent_standard_wins() -> None:
    series = pd.Series(["4005", "4005", "2005"])
    assert dominant_value(series, STANDARD_MODES) == "4005"


def test_falls_back_to_overall_most_frequent_when_no_standard() -> None:
    series = pd.Series(["7700", "7700", "0000"])
    assert dominant_value(series, STANDARD_MODES) == "7700"


def test_family_voting() -> None:
    series = pd.Series(["40", "40", "77", "20"])
    assert dominant_value(series, STANDARD_FAMILIES) == "40"
