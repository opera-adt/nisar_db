"""Duplicate granules: the same acquisition delivered more than once."""

from __future__ import annotations

import pytest

from nisar_db.duplicates import duplicate_groups, parse_name, summarize


def gslc(
    date: str,
    start: str,
    crid: str = "P05023",
    counter: str = "001",
    mode: str = "2005",
    cov: str = "F",
    pol: str = "DHDH",
) -> dict:
    d = date.replace("-", "")
    gid = (
        f"NISAR_L2_PR_GSLC_005_113_D_065_{mode}_{pol}_A_{d}T{start}_{d}T{start}"
        f"_{crid}_N_{cov}_J_{counter}"
    )
    return {"gid": gid, "date": date, "mode": mode, "cov": cov, "pol": pol}


def test_parse_name_reads_start_crid_and_counter():
    name = parse_name(gslc("2026-01-02", "010538", "P05012", "003")["gid"], "gslc")
    assert name == {"start": "20260102T010538", "crid": "P05012", "counter": "003"}


def test_a_short_name_parses_to_blanks():
    assert parse_name("bad_name", "gunw") == {"start": "", "crid": "", "counter": ""}


@pytest.mark.parametrize(
    ("granules", "reason", "keep_crid"),
    [
        (
            [
                gslc("2026-01-02", "010538", "P05012"),
                gslc("2026-01-02", "010538", "P05023"),
            ],
            "reprocessed",
            "P05023",
        ),
        (
            [gslc("2026-01-02", "010538"), gslc("2026-01-02", "010605")],
            "split",
            "P05023",
        ),
        (
            [
                gslc("2026-01-02", "010538", counter="001"),
                gslc("2026-01-02", "010538", counter="002"),
            ],
            "repeat",
            "P05023",
        ),
    ],
)
def test_reason_and_granule_to_keep(granules, reason, keep_crid):
    (group,) = duplicate_groups(granules)
    assert group["reason"] == reason and group["n"] == 2
    assert parse_name(group["keep"], "gslc")["crid"] == keep_crid


def test_a_repeat_keeps_the_highest_counter():
    (group,) = duplicate_groups(
        [
            gslc("2026-01-02", "010538", counter="001"),
            gslc("2026-01-02", "010538", counter="002"),
        ]
    )
    assert group["keep"].endswith("_002")


def test_other_modes_or_coverage_are_not_duplicates():
    granules = [
        gslc("2026-01-02", "010538"),
        gslc("2026-01-02", "010538", mode="4005"),
        gslc("2026-01-02", "010538", cov="P"),
        gslc("2026-01-14", "010538"),
    ]
    assert duplicate_groups(granules) == []


def test_gunw_pairs_repeat_on_both_dates():
    base = {"ref": "2026-01-02", "sec": "2026-01-14", "mode": "4000", "cov": "F"}
    pairs = [
        {**base, "gid": "NISAR_L2_PR_GUNW_a_b_c_d_e_f_g_h_i_j_k_P05012_N_F_J_001"},
        {**base, "gid": "NISAR_L2_PR_GUNW_a_b_c_d_e_f_g_h_i_j_k_P05023_N_F_J_001"},
        {**base, "sec": "2026-01-26", "gid": "NISAR_L2_PR_GUNW_x"},
    ]
    (group,) = duplicate_groups(pairs, "gunw")
    assert (group["ref"], group["sec"], group["reason"]) == (
        "2026-01-02",
        "2026-01-14",
        "reprocessed",
    )


def test_summarize_counts_extra_granules_by_reason():
    groups = duplicate_groups(
        [
            gslc("2026-01-02", "010538", "P05012"),
            gslc("2026-01-02", "010538", "P05023"),
            gslc("2026-01-14", "010538"),
            gslc("2026-01-14", "010605"),
            gslc("2026-01-14", "010630"),
        ]
    )
    assert summarize(groups) == {
        "groups": 2,
        "extra_granules": 3,
        "by_reason": {"reprocessed": 1, "split": 1},
    }
