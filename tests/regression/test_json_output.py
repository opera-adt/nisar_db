"""Regression tests: pin the on-disk structure of the blackout/reference JSONs."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from nisar_db.blackout import (
    create_blackout_dates_json,
    create_reference_dates_json,
)


def _read(path: Path) -> dict:
    return json.loads(path.read_text())


def test_blackout_json_structure(tmp_path: Path) -> None:
    out = tmp_path / "nisar-blackout.json"
    periods = {
        5827: [("2025-12-01", "2026-01-15")],
        "5830": [("2026-03-01", "2026-03-31")],
    }
    returned = create_blackout_dates_json(periods, output=out, description="unit-test")
    assert returned == out

    payload = _read(out)
    # Keys normalised to strings, tuples to lists.
    assert payload["blackout_dates"] == {
        "5827": [["2025-12-01", "2026-01-15"]],
        "5830": [["2026-03-01", "2026-03-31"]],
    }
    assert payload["metadata"]["description"] == "unit-test"
    assert "generation_time" in payload["metadata"]

    # A .json.zip sidecar is written with identical content.
    zip_path = out.with_suffix(".json.zip")
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path) as zf:
        assert json.loads(zf.read(out.name)) == payload


def test_blackout_json_default_description(tmp_path: Path) -> None:
    out = tmp_path / "b.json"
    create_blackout_dates_json({"1": [("2025-01-01", "2025-02-01")]}, output=out)
    assert "blackout" in _read(out)["metadata"]["description"].lower()


def test_reference_dates_json_structure(tmp_path: Path) -> None:
    out = tmp_path / "nisar-reference.json"
    refs = {5827: ["2026-01-15"], "5830": ["2025-12-01", "2026-06-01"]}
    create_reference_dates_json(refs, output=out)

    payload = _read(out)
    assert payload["data"] == {
        "5827": ["2026-01-15"],
        "5830": ["2025-12-01", "2026-06-01"],
    }
    assert "generation_time" in payload["metadata"]
