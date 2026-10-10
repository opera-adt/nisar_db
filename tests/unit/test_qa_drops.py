"""Significant QA drops in a frame's stack, and the dates behind them."""

from __future__ import annotations

import pytest

from nisar_db.qa_drops import METRICS, find_drops, rfi_by_date, robust_stats


def _pairs(values, dt=12, metric="cm"):
    """A chain of pairs, one per 12 days, carrying ``values`` of ``metric``."""
    out = []
    for i, v in enumerate(values):
        ref, sec = f"2026-01-{1 + i:02d}", f"2026-01-{2 + i:02d}"
        out.append(
            {"ref": ref, "sec": sec, "dt": dt, "gid": f"g{i}", "qa": {metric: v}}
        )
    return out


def test_robust_stats_ignore_one_outlier():
    med, sigma = robust_stats([0.6, 0.62, 0.58, 0.61, 0.59, 0.05])
    assert med == pytest.approx(0.595) and sigma == pytest.approx(1.4826 * 0.015)


def test_robust_stats_need_values():
    with pytest.raises(ValueError, match="no values"):
        robust_stats([])


def test_coherence_drop_is_flagged_and_its_date_named():
    # Pairs 4 and 5 share 2026-01-05 (sec of one, ref of the next).
    pairs = _pairs([0.6, 0.62, 0.58, 0.61, 0.1, 0.12, 0.6, 0.59, 0.61])
    report = find_drops(pairs, "cm")
    assert report["status"] == "ok" and report["median"] == 0.6
    assert [f["gid"] for f in report["flagged"]] == ["g4", "g5"]
    assert report["flagged"][0]["change_pct"] == pytest.approx(-83.3, abs=0.1)
    assert [d["date"] for d in report["dates"]] == ["2026-01-06"]
    assert report["dates"][0] == {
        "date": "2026-01-06",
        "n_pairs": 2,
        "n_flagged": 2,
        "share": 1.0,
        "worst_score": report["dates"][0]["worst_score"],
        "gids": ["g4", "g5"],
    }


def test_a_rise_in_coherence_is_not_a_drop():
    report = find_drops(_pairs([0.5, 0.52, 0.48, 0.51, 0.95, 0.5]), "cm")
    assert report["flagged"] == []


@pytest.mark.parametrize(
    ("metric", "values"), [("n", [1, 1, 1, 1, 9, 1]), ("is", [2, 2.2, 1.9, 2.1, 40, 2])]
)
def test_high_is_bad_metrics_flag_rises(metric, values):
    report = find_drops(_pairs(values, metric=metric), metric, min_pairs=1)
    assert [f["gid"] for f in report["flagged"]] == ["g4"]


def test_ionosphere_mean_is_bad_either_way():
    report = find_drops(
        _pairs([0.1, -0.2, 0.0, 0.2, -30.0, 0.1, 25.0], metric="im"), "im", min_pairs=1
    )
    assert sorted(f["gid"] for f in report["flagged"]) == ["g4", "g6"]


def test_a_flat_stack_ignores_changes_below_the_metric_floor():
    """Regression: with MAD 0 (one component in every pair) any 2 scored ~100."""
    report = find_drops(_pairs([1, 1, 1, 1, 2, 1, 1], metric="n"), "n", min_pairs=1)
    assert report["flagged"] == []


def test_pairs_are_compared_with_their_own_temporal_baseline():
    # Mostly 12-day pairs, a few 48-day ones with lower (normal) coherence.
    short = _pairs([0.7, 0.71, 0.69, 0.7, 0.72, 0.7, 0.71, 0.69, 0.7, 0.72, 0.7, 0.71])
    long_ = [
        {**p, "dt": 48, "gid": f"L{i}", "qa": {"cm": v}}
        for i, (p, v) in enumerate(zip(_pairs([0] * 5), [0.3, 0.31, 0.29, 0.3, 0.32]))
    ]
    assert find_drops(short + long_, "cm")["flagged"] == []
    # Lumped together, the long pairs look like drops.
    assert find_drops(short + long_, "cm", by_baseline=False)["flagged"]


def test_a_date_needs_most_of_its_pairs_flagged():
    pairs = _pairs([0.6, 0.62, 0.58, 0.61, 0.1, 0.6, 0.6, 0.59])
    # The one bad pair touches two dates, each with another good pair.
    assert find_drops(pairs, "cm")["dates"] == []
    loose = find_drops(pairs, "cm", min_pairs=1, min_share=0.5)
    assert [d["date"] for d in loose["dates"]] == ["2026-01-05", "2026-01-06"]


def test_gslc_rfi_flags_acquisition_dates_on_a_log_scale():
    granules = [
        {"date": f"2026-02-{d:02d}", "gid": f"s{d}", "qa": {"rl": 1e3}}
        for d in range(1, 8)
    ]
    granules[2]["qa"]["rl"] = 1e9
    report = find_drops(granules, "rl", product="gslc")
    assert [f["date"] for f in report["flagged"]] == ["2026-02-03"]
    assert report["flagged"][0]["value"] == 1e9 and report["median"] == 1e3
    assert report["dates"][0]["date"] == "2026-02-03"


def test_a_pairs_rfi_is_the_worse_of_its_acquisitions():
    granules = [
        {"date": "2026-01-01", "qa": {"rl": 10.0}},
        {"date": "2026-01-01", "qa": {"rl": 50.0}},
        {"date": "2026-01-02", "qa": {"rl": 5.0}},
    ]
    assert rfi_by_date(granules) == {"2026-01-01": 50.0, "2026-01-02": 5.0}


def test_too_few_values_are_not_judged():
    report = find_drops(_pairs([0.5, 0.1]), "cm")
    assert report["status"] == "too few values (2 < 5)" and report["flagged"] == []


@pytest.mark.parametrize(
    ("metric", "product", "message"),
    [
        ("zz", "gunw", "unknown metric 'zz'"),
        ("cm", "gslc", "'cm' is not a GSLC metric"),
    ],
)
def test_bad_metric_or_product(metric, product, message):
    with pytest.raises(ValueError, match=message):
        find_drops([], metric, product=product)


def test_every_metric_has_a_floor():
    assert all(m.min_sigma > 0 for m in METRICS.values())
