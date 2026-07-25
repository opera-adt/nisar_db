"""Unit tests for the CMR result cap (no network)."""

from __future__ import annotations

import logging

import pytest

from nisar_db.search.cmr import _apply_max_results


@pytest.mark.parametrize("cap", [None, 0, -1, 10])
def test_no_truncation_when_cap_is_unset_or_larger(cap: int | None) -> None:
    items = list(range(10))
    assert _apply_max_results(items, cap) == items


def test_truncates_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    items = list(range(10))
    with caplog.at_level(logging.WARNING):
        kept = _apply_max_results(items, 4)

    assert kept == [0, 1, 2, 3]
    # A silently partial archive is the failure mode worth shouting about: the
    # cap is applied after every CMR page is fetched, so it saves no time.
    assert "6 granules dropped" in caplog.text
