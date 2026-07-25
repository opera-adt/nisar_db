"""Unit tests for historical/forward processing-mode labelling."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from click.testing import CliRunner

from nisar_db.processing_mode import (
    add_processing_modes,
    assign_processing_modes,
    find_frames_with_changed_mode,
    get_processing_mode_summary,
    identify_time_groups,
)
from nisar_db.processing_mode import main as label_processing_mode

_REPEAT_DAYS = 12


def _times(n: int, start: datetime = datetime(2025, 1, 5, 1, 23, 45)) -> list[str]:
    """Return ``n`` sensing times at the NISAR 12-day repeat."""
    return [
        (start + timedelta(days=_REPEAT_DAYS * i)).strftime("%Y-%m-%dT%H:%M:%S")
        for i in range(n)
    ]


class TestIdentifyTimeGroups:
    def test_contiguous_stack_is_one_group(self) -> None:
        assert len(identify_time_groups(_times(30), 2.0)) == 1

    def test_long_gap_splits(self) -> None:
        times = _times(20) + _times(5, start=datetime(2031, 6, 1))
        groups = identify_time_groups(times, 2.0)
        assert [len(g) for g in groups] == [20, 5]

    def test_empty(self) -> None:
        assert identify_time_groups([], 2.0) == []


class TestAssignProcessingModes:
    def test_full_batches_are_historical(self) -> None:
        labels = assign_processing_modes(_times(30), batch_size=15)
        assert set(labels.values()) == {"historical_01"}

    def test_trailing_partial_batch_is_forward(self) -> None:
        labels = assign_processing_modes(_times(40), batch_size=15)
        counts = {v: list(labels.values()).count(v) for v in set(labels.values())}
        assert counts == {"historical_01": 30, "forward_01": 10}

    def test_short_stack_is_no_run(self) -> None:
        labels = assign_processing_modes(_times(8), batch_size=15)
        assert set(labels.values()) == {"no_run"}

    def test_group_number_increments_after_gap(self) -> None:
        times = _times(15) + _times(20, start=datetime(2031, 6, 1))
        labels = assign_processing_modes(times, batch_size=15)
        assert set(labels.values()) == {"historical_01", "historical_02", "forward_02"}

    def test_output_is_sorted_by_time(self) -> None:
        times = _times(20)
        labels = assign_processing_modes(list(reversed(times)), batch_size=15)
        assert list(labels) == times

    def test_empty(self) -> None:
        assert assign_processing_modes([]) == {}


class TestAddProcessingModes:
    def test_preserves_other_frame_fields(self) -> None:
        db = {
            "metadata": {"description": "x"},
            "data": {
                "5827": {
                    "common_mode": "4005",
                    "common_coverage": "F",
                    "sensing_time_list": _times(20),
                }
            },
        }
        out = add_processing_modes(db, batch_size=15)
        frame = out["data"]["5827"]
        assert frame["common_mode"] == "4005"
        assert frame["common_coverage"] == "F"
        assert isinstance(frame["sensing_time_list"], dict)
        assert out["metadata"]["processing_mode_params"]["batch_size"] == 15

    def test_accepts_unwrapped_document(self) -> None:
        db = {"5827": {"sensing_time_list": _times(3)}}
        out = add_processing_modes(db)
        assert set(out["data"]["5827"]["sensing_time_list"].values()) == {"no_run"}


class TestFindFramesWithChangedMode:
    def test_detects_coverage_flip(self) -> None:
        old = {"1": {"common_mode": "4005", "common_coverage": "F"}}
        new = {"1": {"common_mode": "4005", "common_coverage": "P"}}
        assert find_frames_with_changed_mode(old, new) == ["1"]

    def test_unchanged_frames_omitted(self) -> None:
        same = {"1": {"common_mode": "4005", "common_coverage": "F"}}
        assert find_frames_with_changed_mode(same, dict(same)) == []

    def test_frames_missing_from_new_are_skipped(self) -> None:
        old = {"1": {"common_mode": "4005", "common_coverage": "F"}}
        assert find_frames_with_changed_mode(old, {}) == []


class TestSummary:
    def test_counts_across_groups(self) -> None:
        db = {
            "data": {
                "1": {"sensing_time_list": _times(40)},
                "2": {"sensing_time_list": _times(5)},
            }
        }
        summary = get_processing_mode_summary(add_processing_modes(db, batch_size=15))
        assert summary["historical_count"] == 30
        assert summary["forward_count"] == 10
        assert summary["no_run_count"] == 5
        assert summary["frames_with_forward"] == 1
        assert summary["total_frames"] == 2

    def test_group_numbers_above_nine_still_counted(self) -> None:
        labels = {f"t{i}": f"historical_{i:02d}" for i in range(9, 12)}
        summary = get_processing_mode_summary(
            {"data": {"1": {"sensing_time_list": labels}}}
        )
        assert summary["historical_count"] == 3


class TestCli:
    def test_writes_json_and_zip(self, tmp_path: Path) -> None:
        consistent = tmp_path / "consistent.json"
        consistent.write_text(
            json.dumps(
                {
                    "metadata": {},
                    "data": {
                        "1": {
                            "common_mode": "4005",
                            "common_coverage": "F",
                            "sensing_time_list": _times(20),
                        }
                    },
                }
            )
        )
        previous = tmp_path / "previous.json"
        previous.write_text(
            json.dumps({"data": {"1": {"common_mode": "4005", "common_coverage": "P"}}})
        )
        output = tmp_path / "labelled.json"

        result = CliRunner().invoke(
            label_processing_mode,
            [
                "--consistent-json",
                str(consistent),
                "--previous-json",
                str(previous),
                "--output",
                str(output),
            ],
        )

        assert result.exit_code == 0, result.output
        assert output.exists()
        assert (tmp_path / "labelled.json.zip").exists()
        written = json.loads(output.read_text())
        assert written["metadata"]["frames_with_changed_mode"] == ["1"]
