"""Unit tests for blackout-period filtering."""

from __future__ import annotations

import json
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from nisar_db.blackout import (
    append_blackout_dates_json,
    append_blackout_period,
    apply_blackout,
    is_excluded,
    load_blackout_json,
    normalize_period,
)

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


class TestNormalizePeriod:
    def test_bare_dates_get_inclusive_end_of_day(self) -> None:
        assert normalize_period("2025-11-01", "2026-05-31") == [
            "2025-11-01T00:00:00",
            "2026-05-31T23:59:59",
        ]

    def test_explicit_times_are_kept(self) -> None:
        assert normalize_period("2025-11-01T06:00:00", "2025-11-02T12:30:00") == [
            "2025-11-01T06:00:00",
            "2025-11-02T12:30:00",
        ]

    def test_reversed_range_raises(self) -> None:
        with pytest.raises(ValueError, match="precedes start"):
            normalize_period("2026-05-31", "2025-11-01")


class TestAppendBlackoutPeriod:
    def test_creates_missing_frame(self) -> None:
        periods: dict[str, list[list[str]]] = {}
        assert append_blackout_period(periods, 5827, "2025-12-01", "2026-01-15") is True
        assert periods == {"5827": [["2025-12-01T00:00:00", "2026-01-15T23:59:59"]]}

    def test_duplicate_not_appended(self) -> None:
        periods: dict[str, list[list[str]]] = {}
        append_blackout_period(periods, "5827", "2025-12-01", "2026-01-15")
        assert (
            append_blackout_period(periods, "5827", "2025-12-01", "2026-01-15") is False
        )
        assert len(periods["5827"]) == 1

    def test_windows_kept_sorted(self) -> None:
        periods: dict[str, list[list[str]]] = {}
        append_blackout_period(periods, "1", "2026-03-01", "2026-03-31")
        append_blackout_period(periods, "1", "2025-12-01", "2025-12-31")
        assert [p[0][:10] for p in periods["1"]] == ["2025-12-01", "2026-03-01"]

    def test_appended_window_is_excluded_by_filter(self) -> None:
        periods: dict[str, list[list[str]]] = {}
        append_blackout_period(periods, "5827", "2025-12-01", "2026-01-15")
        assert is_excluded("5827", date(2026, 1, 15), periods) is True
        assert is_excluded("5827", date(2026, 1, 16), periods) is False


def _write_doc(path: Path) -> Path:
    doc = {
        "metadata": {"generation_time": "2026-01-01T00:00:00"},
        "blackout_dates": {"1001": [["2025-11-01T00:00:00", "2026-05-31T23:59:59"]]},
    }
    path.write_text(json.dumps(doc))
    return path


class TestAppendBlackoutDatesJson:
    def test_appends_in_place_and_refreshes_zip(self, tmp_path: Path) -> None:
        json_file = _write_doc(tmp_path / "bo.json")
        append_blackout_dates_json(json_file, 5827, [("2025-12-01", "2026-01-15")])

        doc = json.loads(json_file.read_text())
        assert doc["blackout_dates"]["5827"] == [
            ["2025-12-01T00:00:00", "2026-01-15T23:59:59"]
        ]
        assert doc["blackout_dates"]["1001"]  # untouched
        assert doc["metadata"]["manual_edits"][0]["frame_idx"] == "5827"

        zip_path = tmp_path / "bo.json.zip"
        with zipfile.ZipFile(zip_path) as zf:
            assert json.loads(zf.read("bo.json")) == doc

    def test_output_leaves_input_untouched(self, tmp_path: Path) -> None:
        json_file = _write_doc(tmp_path / "bo.json")
        original = json_file.read_text()
        out = append_blackout_dates_json(
            json_file,
            5827,
            [("2025-12-01", "2026-01-15")],
            output=tmp_path / "new.json",
            write_zip=False,
        )
        assert json_file.read_text() == original
        assert "5827" in json.loads(out.read_text())["blackout_dates"]

    def test_create_flag_starts_empty_document(self, tmp_path: Path) -> None:
        out = append_blackout_dates_json(
            tmp_path / "new.json",
            5827,
            [("2025-12-01", "2026-01-15")],
            create=True,
            write_zip=False,
        )
        assert list(json.loads(out.read_text())["blackout_dates"]) == ["5827"]

    def test_missing_file_without_create_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            append_blackout_dates_json(
                tmp_path / "missing.json", 1, [("2025-12-01", "2026-01-15")]
            )

    def test_zip_input_writes_plain_json_sibling(self, tmp_path: Path) -> None:
        json_file = _write_doc(tmp_path / "bo.json")
        with zipfile.ZipFile(tmp_path / "bo.json.zip", "w") as zf:
            zf.writestr("bo.json", json_file.read_text())
        json_file.unlink()

        out = append_blackout_dates_json(
            tmp_path / "bo.json.zip", 5827, [("2025-12-01", "2026-01-15")]
        )
        assert out == tmp_path / "bo.json"
        assert "5827" in json.loads(out.read_text())["blackout_dates"]

    def test_rejects_non_blackout_json(self, tmp_path: Path) -> None:
        path = tmp_path / "other.json"
        path.write_text(json.dumps({"data": {}}))
        with pytest.raises(ValueError, match="blackout_dates"):
            load_blackout_json(path)
