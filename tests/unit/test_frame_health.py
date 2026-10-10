"""Coverage gaps, network health, next passes, DISP readiness, QA around an event."""

from __future__ import annotations

import pytest

from nisar_db import frame_health as fh


def _grans(*pairs, mode="4005", cov="F"):
    return [{"date": d, "cycle": c, "mode": mode, "cov": cov} for d, c in pairs]


def test_coverage_names_missed_cycles_and_gaps():
    c = fh.coverage(
        _grans(("2026-06-02", 22), ("2026-06-14", 23), ("2026-07-08", 25)),
        today="2026-07-20",
    )
    assert (
        c["n_acquisitions"] == 3
        and c["first"] == "2026-06-02"
        and c["last"] == "2026-07-08"
    )
    assert c["missed_cycles"] == [{"cycle": 24, "expected_date": "2026-06-26"}]
    assert c["gaps"] == [{"from": "2026-06-14", "to": "2026-07-08", "days": 24}]
    assert c["days_since_last"] == 12 and c["longest_gap_days"] == 24


def test_coverage_of_nothing():
    assert fh.coverage([], today="2026-07-20")["last"] is None


def test_network_connected_and_unpaired():
    pairs = [
        {"ref": "2026-01-01", "sec": "2026-01-13"},
        {"ref": "2026-01-13", "sec": "2026-01-25"},
        {"ref": "2026-01-01", "sec": "2026-01-25"},
    ]
    n = fh.network(pairs, [{"date": "2026-02-06"}])
    assert n["status"] == "connected" and n["components"] == 1 and n["breaks"] == []
    assert n["unpaired"] == ["2026-02-06"] and n["n_pairs"] == 3


def test_network_break_and_off_main_dates():
    pairs = [
        {"ref": "2026-01-01", "sec": "2026-01-13"},
        {"ref": "2026-01-13", "sec": "2026-01-25"},
        {"ref": "2026-02-06", "sec": "2026-02-18"},
    ]
    n = fh.network(pairs)
    assert n["status"] == "disconnected" and n["components"] == 2
    assert n["breaks"] == [{"from": "2026-01-25", "to": "2026-02-06"}]
    assert n["off_main"] == ["2026-02-06", "2026-02-18"]


def test_network_without_pairs():
    assert fh.network([])["status"] == "no GUNW"


def test_next_passes_step_the_repeat_from_the_last_acquisition():
    assert fh.next_passes([{"date": "2026-07-08"}], n=3, today="2026-07-21") == [
        "2026-08-01",
        "2026-08-13",
        "2026-08-25",
    ]
    assert fh.next_passes([], today="2026-07-21") == []


def _frame(n_dates: int, *, blackout=None, other_mode=0):
    dates = [
        f"2026-{1 + (12 * i) // 30:02d}-{1 + (12 * i) % 28:02d}" for i in range(n_dates)
    ]
    grans = [{"date": d, "mode": "4005", "cov": "F"} for d in dates]
    grans += [
        {"date": f"2025-0{1 + i}-15", "mode": "2005", "cov": "F"}
        for i in range(other_mode)
    ]
    return {
        "cons_mode": "4005",
        "cons_cov": "F",
        "granules": grans,
        "blackout_ranges": blackout or [],
    }


def test_readiness_counts_consistent_acquisitions_outside_blackouts():
    r = fh.disp_readiness(
        _frame(6, blackout=["2026-01-01 -> 2026-01-31"], other_mode=2),
        today="2026-04-01",
    )
    assert (
        r["status"] == "accumulating" and r["n_blacked_out"] == 3 and r["n_usable"] == 3
    )
    assert r["n_other_modes"] == 2 and r["to_next_batch"] == 12 and r["batches"] == 0


def test_readiness_ready_after_a_full_batch():
    r = fh.disp_readiness(_frame(15), batch_size=15, today="2026-12-01")
    assert r["status"] == "ready" and r["batches"] == 1 and r["to_next_batch"] == 15


def test_next_batch_date_skips_blackout_windows():
    props = {
        "cons_mode": "4005",
        "cons_cov": "F",
        "granules": [{"date": "2026-01-01", "mode": "4005", "cov": "F"}],
        "blackout_ranges": ["2026-01-02 -> 2026-01-20"],
    }
    # Batch of 2: one more pass needed; 2026-01-13 is blacked out, 2026-01-25 is not.
    assert (
        fh.disp_readiness(props, batch_size=2, today="2026-01-01")["next_batch_date"]
        == "2026-01-25"
    )


def test_readiness_without_consistent_mode():
    assert fh.disp_readiness({"cons_mode": "none"})["status"] == "no consistent mode"


def test_event_qa_history_splits_before_across_after():
    pairs = [
        {"ref": r, "sec": s, "dt": 12, "qa": {"cm": v}}
        for r, s, v in [
            ("2026-05-01", "2026-05-13", 0.6),
            ("2026-05-13", "2026-05-25", 0.62),
            ("2026-05-25", "2026-06-06", 0.2),
            ("2026-06-06", "2026-06-18", 0.5),
        ]
    ]
    h = fh.event_qa_history(pairs, "2026-06-01", "cm", window_days=60)
    assert h["n"] == {"before": 2, "spanning": 1, "after": 1}
    assert h["median"] == {"before": 0.61, "spanning": 0.2, "after": 0.5}
    assert h["change_pct"] == {"spanning": -67.2, "after": -18.0}
    assert [p["side"] for p in h["pairs"]] == ["before", "before", "spanning", "after"]


def test_event_qa_history_rejects_an_unknown_metric():
    with pytest.raises(ValueError, match="unknown metric 'zz'"):
        fh.event_qa_history([], "2026-06-01", "zz")
