"""Unit tests for reference (reset) date derivation."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from nisar_db.reference_dates import (
    build_desired_month_map_from_blackout,
    calculate_reference_dates,
    find_blacked_out_references,
    load_consistent_json,
    pick_month_based_on_snow,
)
from nisar_db.reference_dates import main as create_reference_dates

_REPEAT_DAYS = 12
_START = datetime(2025, 1, 5, 1, 23, 45)


def _times(n: int) -> list[str]:
    return [
        (_START + timedelta(days=_REPEAT_DAYS * i)).strftime("%Y-%m-%dT%H:%M:%S")
        for i in range(n)
    ]


@pytest.fixture
def consistent_json(tmp_path: Path) -> Path:
    path = tmp_path / "consistent.json"
    path.write_text(
        json.dumps(
            {
                "metadata": {},
                "data": {
                    "5827": {"sensing_time_list": _times(60)},
                    "5830": {"sensing_time_list": _times(10)},
                },
            }
        )
    )
    return path


@pytest.fixture
def blackout_json(tmp_path: Path) -> Path:
    path = tmp_path / "blackout.json"
    path.write_text(
        json.dumps(
            {
                "blackout_dates": {
                    "5827": [["2025-11-01", "2026-05-31"]] * 7,
                    "5829": [["2025-12-01", "2026-03-01"]] * 3,
                    "5830": [],
                }
            }
        )
    )
    return path


class TestPickMonth:
    @pytest.mark.parametrize(
        ("num_blackouts", "expected"), [(0, 11), (1, 9), (5, 9), (6, 7), (20, 7)]
    )
    def test_month_moves_towards_summer_with_snow(
        self, num_blackouts: int, expected: int
    ) -> None:
        assert pick_month_based_on_snow(num_blackouts) == expected


class TestBuildDesiredMonthMap:
    def test_maps_every_frame(self, blackout_json: Path) -> None:
        months = build_desired_month_map_from_blackout(blackout_json)
        assert months == {"5827": 7, "5829": 9, "5830": 11}


class TestIntervalBased:
    def test_first_acquisition_is_always_a_reference(
        self, consistent_json: Path
    ) -> None:
        refs = calculate_reference_dates(consistent_json_file=consistent_json)
        assert refs["5827"][0] == _times(1)[0]

    def test_new_epoch_opens_after_a_year(self, consistent_json: Path) -> None:
        refs = calculate_reference_dates(consistent_json_file=consistent_json)
        assert len(refs["5827"]) == 2
        assert (
            datetime.strptime(refs["5827"][1], "%Y-%m-%dT%H:%M:%S") - _START
        ).days >= 365

    def test_sparse_frame_keeps_a_single_reference(self, consistent_json: Path) -> None:
        refs = calculate_reference_dates(consistent_json_file=consistent_json)
        assert len(refs["5830"]) == 1

    def test_min_acquisitions_blocks_new_epoch(self, consistent_json: Path) -> None:
        refs = calculate_reference_dates(
            consistent_json_file=consistent_json, min_acquisitions_per_batch=1000
        )
        assert len(refs["5827"]) == 1


class TestFindBlackedOutReferences:
    """The stack is filtered upstream; this only catches when it was not."""

    def test_clean_when_no_reference_is_blacked_out(self) -> None:
        refs = {"5827": ["2025-07-05T00:00:00"]}
        blackout = {"5827": [["2025-11-01T00:00:00", "2026-05-31T23:59:59"]]}
        assert find_blacked_out_references(refs, blackout) == {}

    def test_reports_a_reference_inside_a_window(self) -> None:
        refs = {"5827": ["2025-07-05T00:00:00", "2026-01-12T00:00:00"]}
        blackout = {"5827": [["2025-11-01T00:00:00", "2026-05-31T23:59:59"]]}
        assert find_blacked_out_references(refs, blackout) == {
            "5827": ["2026-01-12T00:00:00"]
        }

    def test_ignores_frames_with_no_blackout(self) -> None:
        refs = {"5830": ["2026-01-12T00:00:00"]}
        blackout = {"5827": [["2025-11-01T00:00:00", "2026-05-31T23:59:59"]]}
        assert find_blacked_out_references(refs, blackout) == {}


class TestMonthBased:
    def test_every_date_is_a_real_acquisition(self, consistent_json: Path) -> None:
        refs = calculate_reference_dates(
            consistent_json_file=consistent_json, desired_month_by_frame={"5827": 7}
        )
        assert set(refs["5827"]) <= set(_times(60))

    def test_one_date_per_year_starting_in_the_chosen_month(
        self, consistent_json: Path
    ) -> None:
        refs = calculate_reference_dates(
            consistent_json_file=consistent_json, desired_month_by_frame={"5827": 7}
        )
        # 60 acquisitions 12 days apart span 2025-01-05 to 2026-12-21.
        assert [d[:7] for d in refs["5827"]] == ["2025-07", "2026-07"]
        assert all(
            datetime.strptime(d, "%Y-%m-%dT%H:%M:%S") >= datetime(int(d[:4]), 7, 1)
            for d in refs["5827"]
        )

    def test_no_dates_past_the_end_of_the_archive(self, consistent_json: Path) -> None:
        refs = calculate_reference_dates(
            consistent_json_file=consistent_json, desired_month_by_frame={"5827": 7}
        )
        last_acquisition = datetime.strptime(_times(60)[-1], "%Y-%m-%dT%H:%M:%S")
        assert all(
            datetime.strptime(d, "%Y-%m-%dT%H:%M:%S") <= last_acquisition
            for d in refs["5827"]
        )

    def test_frame_ending_before_its_anchor_month_is_dropped(
        self, consistent_json: Path
    ) -> None:
        # 5830's 10 acquisitions end in April 2025, before any July anchor.
        refs = calculate_reference_dates(
            consistent_json_file=consistent_json,
            desired_month_by_frame={"5827": 7, "5830": 7},
        )
        assert list(refs) == ["5827"]

    def test_frame_without_acquisitions_is_dropped(self, consistent_json: Path) -> None:
        refs = calculate_reference_dates(
            consistent_json_file=consistent_json,
            desired_month_by_frame={"5827": 7, "999": 9},
        )
        assert list(refs) == ["5827"]

    def test_anchors_before_the_first_acquisition_collapse(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "late.json"
        path.write_text(
            json.dumps({"data": {"1": {"sensing_time_list": ["2027-08-02T00:00:00"]}}})
        )
        refs = calculate_reference_dates(
            consistent_json_file=path, desired_month_by_frame={"1": 7}
        )
        assert refs["1"] == ["2027-08-02T00:00:00"]


class TestErrors:
    def test_requires_consistent_json(self) -> None:
        with pytest.raises(ValueError, match="--consistent-json"):
            calculate_reference_dates()

    def test_month_map_alone_is_not_enough(self) -> None:
        with pytest.raises(ValueError, match="--consistent-json"):
            calculate_reference_dates(desired_month_by_frame={"5827": 7})


class TestLoadConsistentJson:
    def test_accepts_unwrapped_document(self, tmp_path: Path) -> None:
        path = tmp_path / "flat.json"
        path.write_text(json.dumps({"1": {"sensing_time_list": []}}))
        assert list(load_consistent_json(path)) == ["1"]


class TestCli:
    def test_writes_json_and_zip(
        self, consistent_json: Path, blackout_json: Path, tmp_path: Path
    ) -> None:
        output = tmp_path / "refs.json"
        result = CliRunner().invoke(
            create_reference_dates,
            [
                "--consistent-json",
                str(consistent_json),
                "--blackout-file",
                str(blackout_json),
                "--output",
                str(output),
            ],
        )

        assert result.exit_code == 0, result.output
        assert (tmp_path / "refs.json.zip").exists()
        written = json.loads(output.read_text())
        assert written["metadata"]["strategy"] == "month-based"
        assert set(written["data"]["5827"]) <= set(_times(60))

    def test_unfiltered_stack_is_rejected(
        self, blackout_json: Path, tmp_path: Path
    ) -> None:
        # A stack built without --blackout-file still holds winter acquisitions,
        # so 5827's September anchor lands inside its blackout window.
        stack = tmp_path / "unfiltered.json"
        stack.write_text(
            json.dumps(
                {"data": {"5829": {"sensing_time_list": ["2025-12-15T00:00:00"]}}}
            )
        )
        result = CliRunner().invoke(
            create_reference_dates,
            [
                "--consistent-json",
                str(stack),
                "--blackout-file",
                str(blackout_json),
                "--output",
                str(tmp_path / "refs.json"),
            ],
        )

        assert result.exit_code != 0
        assert "inside a blackout window" in result.output
        assert "make-consistent-gslc --blackout-file" in result.output

    def test_no_input_is_a_usage_error(self) -> None:
        result = CliRunner().invoke(create_reference_dates, [])
        assert result.exit_code != 0
        assert "--consistent-json is required" in result.output

    def test_blackout_alone_is_a_usage_error(self, blackout_json: Path) -> None:
        result = CliRunner().invoke(
            create_reference_dates, ["--blackout-file", str(blackout_json)]
        )
        assert result.exit_code != 0
        assert "--consistent-json is required" in result.output
