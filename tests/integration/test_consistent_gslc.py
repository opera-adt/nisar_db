"""Integration test for the consistent-GSLC selection pipeline."""

from __future__ import annotations

import pandas as pd

from nisar_db.consistent_gslc import (
    _common_mode_coverage,
    select_consistent_acquisitions,
)


def _grp(*combos: tuple[str, str, int]) -> pd.DataFrame:
    """Build a (track, frame) group from ``(mode, coverage, count)`` triples."""
    rows = [{"mode": m, "coverage": c} for m, c, n in combos for _ in range(n)]
    return pd.DataFrame(rows)


def test_common_mode_coverage_prefers_standard_mode_and_full() -> None:
    grp = pd.DataFrame(
        {
            "mode": ["4005", "4005", "4005", "2005", "7700"],
            "coverage": ["F", "F", "P", "F", "F"],
        }
    )
    assert _common_mode_coverage(grp) == ("4005", "F")


def test_common_mode_coverage_full_beats_partial_across_modes() -> None:
    # 4005 is the preferred mode, but it only ever comes in partial coverage
    # here, so the full-frame 2005 stack wins -- even with fewer acquisitions.
    # 50% partial keeps the frame under PARTIAL_DOMINANCE_THRESHOLD.
    grp = _grp(("4005", "P", 4), ("2005", "F", 4))
    assert _common_mode_coverage(grp) == ("2005", "F")


def test_common_mode_coverage_partial_dominant_frame_keeps_partial() -> None:
    # Real case: track 42 / frame 164. 80% partial, so preferring the one
    # full-frame acquisition would cut the stack from four epochs to one.
    grp = _grp(("4005", "P", 4), ("2005", "F", 1))
    assert _common_mode_coverage(grp) == ("4005", "P")


def test_common_mode_coverage_partial_threshold_boundary() -> None:
    # 3/5 partial (60%) stays under the threshold, so full coverage wins.
    assert _common_mode_coverage(_grp(("4005", "P", 3), ("2005", "F", 2))) == (
        "2005",
        "F",
    )
    # 2/3 partial (66.7%) is above 0.66, so the partial series wins.
    assert _common_mode_coverage(_grp(("4005", "P", 2), ("2005", "F", 1))) == (
        "4005",
        "P",
    )


def test_common_mode_coverage_prefers_4005_when_both_full() -> None:
    # Real case: track 163 / frame 11, an exact 4-vs-4 tie in full coverage.
    grp = _grp(("2005", "F", 4), ("4005", "F", 4))
    assert _common_mode_coverage(grp) == ("4005", "F")


def test_common_mode_coverage_count_outranks_mode_priority() -> None:
    # 4005 is preferred only on level counts; a longer 2005 series wins.
    assert _common_mode_coverage(_grp(("2005", "F", 9), ("4005", "F", 2))) == (
        "2005",
        "F",
    )
    # Real case: track 29 / frame 96 -- both partial, so the count decides.
    assert _common_mode_coverage(_grp(("2005", "P", 4), ("4005", "P", 1))) == (
        "2005",
        "P",
    )


def test_common_mode_coverage_partial_majority_within_single_mode() -> None:
    grp = _grp(("4005", "P", 7), ("4005", "F", 1))
    assert _common_mode_coverage(grp) == ("4005", "P")


def test_common_mode_coverage_is_order_independent() -> None:
    combos = [("2005", "F", 4), ("4005", "F", 4), ("7700", "P", 9)]
    assert _common_mode_coverage(_grp(*combos)) == _common_mode_coverage(
        _grp(*reversed(combos))
    )


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
