"""Unit tests for the mode/family voting helpers."""

from __future__ import annotations

import pandas as pd

from nisar_db.modes import (
    FAMILY_PRIORITY,
    MODE_PRIORITY,
    STANDARD_FAMILIES,
    STANDARD_MODES,
    dominant_value,
    value_rank,
)


def test_standard_sets() -> None:
    assert {"4005", "2005"} == STANDARD_MODES
    assert {"40", "20"} == STANDARD_FAMILIES


def test_priority_order() -> None:
    assert MODE_PRIORITY == ("4005", "2005")
    assert FAMILY_PRIORITY == ("40", "20")


def test_value_rank_puts_unlisted_values_last() -> None:
    assert value_rank("4005", MODE_PRIORITY) == 0
    assert value_rank("2005", MODE_PRIORITY) == 1
    assert value_rank("7700", MODE_PRIORITY) == len(MODE_PRIORITY)


def test_standard_value_wins_over_more_frequent_nonstandard() -> None:
    # "7700" appears more often, but a standard mode is present and must win.
    series = pd.Series(["7700", "7700", "7700", "4005"])
    assert dominant_value(series, MODE_PRIORITY) == "4005"


def test_most_frequent_standard_wins() -> None:
    series = pd.Series(["4005", "4005", "2005"])
    assert dominant_value(series, MODE_PRIORITY) == "4005"


def test_equal_counts_break_toward_priority_order() -> None:
    # Equal counts, "2005" seen first: the priority order still picks "4005".
    series = pd.Series(["2005", "4005"])
    assert dominant_value(series, MODE_PRIORITY) == "4005"


def test_falls_back_to_overall_most_frequent_when_no_standard() -> None:
    series = pd.Series(["7700", "7700", "0000"])
    assert dominant_value(series, MODE_PRIORITY) == "7700"


def test_family_voting() -> None:
    series = pd.Series(["40", "40", "77", "20"])
    assert dominant_value(series, FAMILY_PRIORITY) == "40"
