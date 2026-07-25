"""Unit tests for blackout-period filtering."""

from __future__ import annotations

from datetime import date

import pandas as pd

from nisar_db.blackout import apply_blackout, is_excluded

_PERIODS = {
    "129": [["2024-06-10", "2024-06-20"]],
    "300": [["2024-01-01", "2024-01-31"], ["2024-12-01", "2024-12-31"]],
}


class TestIsExcluded:
    def test_inside_range_inclusive(self) -> None:
        assert is_excluded("129", date(2024, 6, 10), _PERIODS) is True
        assert is_excluded("129", date(2024, 6, 20), _PERIODS) is True
        assert is_excluded("129", date(2024, 6, 15), _PERIODS) is True

    def test_outside_range(self) -> None:
        assert is_excluded("129", date(2024, 6, 21), _PERIODS) is False

    def test_frame_key_coerced_to_str(self) -> None:
        assert is_excluded(129, date(2024, 6, 15), _PERIODS) is True

    def test_unknown_frame_never_excluded(self) -> None:
        assert is_excluded("999", date(2024, 6, 15), _PERIODS) is False

    def test_second_period_of_a_frame(self) -> None:
        assert is_excluded("300", date(2024, 12, 15), _PERIODS) is True
        assert is_excluded("300", date(2024, 6, 15), _PERIODS) is False


class TestApplyBlackout:
    def _df(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "frame_idx": ["129", "129", "300"],
                "sensing_date": ["2024-06-15", "2024-07-01", "2024-01-15"],
            }
        )

    def test_removes_blacked_out_rows(self) -> None:
        out = apply_blackout(self._df(), "frame_idx", _PERIODS)
        assert list(out["sensing_date"]) == ["2024-07-01"]

    def test_empty_periods_is_noop(self) -> None:
        df = self._df()
        out = apply_blackout(df, "frame_idx", {})
        pd.testing.assert_frame_equal(out, df)

    def test_index_reset_after_filter(self) -> None:
        out = apply_blackout(self._df(), "frame_idx", _PERIODS)
        assert list(out.index) == list(range(len(out)))
