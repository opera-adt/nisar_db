"""Integration test for the consistent-GSLC selection pipeline."""

from __future__ import annotations

import pandas as pd

from nisar_db.consistent_gslc import (
    _common_mode_coverage,
    select_consistent_acquisitions,
)


def test_common_mode_coverage_prefers_standard_mode_and_full() -> None:
    grp = pd.DataFrame(
        {
            "mode": ["4005", "4005", "4005", "2005", "7700"],
            "coverage": ["F", "F", "P", "F", "F"],
        }
    )
    assert _common_mode_coverage(grp) == ("4005", "F")


def test_common_mode_coverage_falls_back_to_nonstandard() -> None:
    grp = pd.DataFrame({"mode": ["7700", "7700", "7700"], "coverage": ["P", "P", "F"]})
    assert _common_mode_coverage(grp) == ("7700", "P")


def test_select_consistent_acquisitions(consistent_catalog_df: pd.DataFrame) -> None:
    out = select_consistent_acquisitions(consistent_catalog_df)

    # Frame (128,129) -> 4005/F on two dates (same-date dup collapsed);
    # frame (200,300) -> 7700/P on two dates. Four rows total.
    assert len(out) == 4

    f1 = out[(out["track"] == 128) & (out["frame"] == 129)]
    assert set(f1["common_mode"]) == {"4005"}
    assert set(f1["common_coverage"]) == {"F"}
    assert list(f1["sensing_date"]) == ["2024-06-01", "2024-06-13"]

    f2 = out[(out["track"] == 200) & (out["frame"] == 300)]
    assert set(f2["common_mode"]) == {"7700"}
    assert set(f2["common_coverage"]) == {"P"}
    assert len(f2) == 2


def test_duplicate_date_keeps_earliest_time(
    consistent_catalog_df: pd.DataFrame,
) -> None:
    out = select_consistent_acquisitions(consistent_catalog_df)
    first = out[
        (out["track"] == 128)
        & (out["frame"] == 129)
        & (out["sensing_date"] == "2024-06-01")
    ]
    assert len(first) == 1
    assert first.iloc[0]["sensing_time"] == pd.Timestamp("2024-06-01T00:00:00")
