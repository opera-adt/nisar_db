"""Unit tests for the product layout page generator (no network)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np
import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
GRANULE = (
    "NISAR_L2_PR_GSLC_033_117_A_165_4005_DHDH_A_20261020T084224_20261020T084229"
    "_P05023_N_P_J_001"
)


def _load() -> ModuleType:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(
        "document_product_layout", SCRIPTS / "document_product_layout.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("path", "pattern", "seen"),
    [
        (
            "science/LSAR/GSLC/grids/frequencyB/VV",
            "science/LSAR/GSLC/grids/frequency{f}/{pol}",
            {"f": "B", "pol": "VV"},
        ),
        (
            "a/pixelOffsets/HH/layer3/x",
            "a/pixelOffsets/{pol}/layer{n}/x",
            {"pol": "HH", "n": "3"},
        ),
        (
            "science/LSAR/identification/trackNumber",
            "science/LSAR/identification/trackNumber",
            {},
        ),
    ],
)
def test_fold_names_the_repeating_parts(path: str, pattern: str, seen: dict) -> None:
    assert _load().fold(path) == (pattern, seen)


def test_string_types_do_not_break_a_table() -> None:
    gen = _load()
    assert gen.dtype_name(np.dtype("S6")) == "string[6]"
    assert gen.dtype_name(np.dtype("float32")) == "float32"


def _rows() -> list[dict]:
    base = "science/LSAR/GSLC/grids/frequency{f}/{pol}"
    rows: list[dict] = [
        {"path": "science/LSAR/GSLC/grids", "attrs": {}},
        {
            "path": "science/LSAR/identification/trackNumber",
            "attrs": {"description": b"Track number"},
            "shape": (),
            "dtype": "uint8",
            "value": 117,
        },
        {
            "path": "science/LSAR/GSLC/metadata/orbit/position",
            "attrs": {"units": "meters", "description": "Position | ECEF"},
            "shape": (7, 3),
            "dtype": "float64",
        },
    ]
    for f, pol in (("A", "HH"), ("A", "HV"), ("B", "HH")):
        rows.append(
            {
                "path": base.format(f=f, pol=pol),
                "attrs": {"description": "  Complex\n  image  "},
                "shape": (10, 20),
                "dtype": "complex64",
            }
        )
    return rows


def test_render_folds_repeats_and_puts_identification_first() -> None:
    page = _load().render("GSLC", GRANULE, 3_850_000_000, _rows())

    sections = [line for line in page.splitlines() if line.startswith("## ")]
    assert sections == ["## identification", "## grids", "## metadata/orbit"]
    # Three per-frequency, per-polarization datasets become one row.
    assert page.count("frequency{A,B}/{HH,HV}") == 1
    assert "| 10 \u00d7 20 | `complex64` |  | Complex image |" in page
    assert "Example: `117`" in page
    # A pipe in a description must not split the table row.
    assert "Position \\| ECEF" in page
    assert "3.9 GB" in page


def test_read_layout_lists_groups_datasets_and_small_values(tmp_path: Path) -> None:
    h5py = pytest.importorskip("h5py")
    gen = _load()
    path = tmp_path / "product.h5"
    with h5py.File(path, "w") as h5:
        h5["science/LSAR/identification/trackNumber"] = np.uint8(117)
        h5["science/LSAR/GSLC/grids/frequencyA/HH"] = np.zeros((4, 5), "complex64")
        h5["science/LSAR/GSLC/grids/frequencyA/HH"].attrs["units"] = "1"

    with h5py.File(path) as h5:
        rows = {r["path"]: r for r in gen.read_layout(h5)}

    assert "dtype" not in rows["science/LSAR/GSLC/grids"]  # a group
    track = rows["science/LSAR/identification/trackNumber"]
    assert (track["shape"], track["dtype"], track["value"]) == ((), "uint8", 117)
    image = rows["science/LSAR/GSLC/grids/frequencyA/HH"]
    assert image["shape"] == (4, 5) and "value" not in image


class FakeResponse:
    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body

    def json(self) -> dict[str, Any]:
        return self.body


class FakeCMR:
    """Answers the two CMR requests ``data_url`` makes."""

    def __init__(self, entries: list[dict]) -> None:
        self.entries = entries

    def get(
        self, url: str, params: dict | None = None, timeout: float = 0
    ) -> FakeResponse:
        assert timeout > 0
        self.params = params
        if url.endswith("granules.json"):
            body: dict[str, Any] = {"feed": {"entry": self.entries}}
        else:
            body = {
                "RelatedUrls": [
                    {"URL": f"s3://bucket/{GRANULE}.h5"},
                    {"URL": f"https://host/{GRANULE}_QA_STATS.h5"},
                    {"URL": f"https://host/{GRANULE}.h5"},
                ],
                "DataGranule": {
                    "ArchiveAndDistributionInformation": [
                        {"Name": f"{GRANULE}.h5", "SizeInBytes": 42}
                    ]
                },
            }
        return FakeResponse(body)


def test_data_url_picks_the_https_product_file() -> None:
    product, url, size = _load().data_url(GRANULE, FakeCMR([{"id": "G1-ASF"}]))

    assert (product, url, size) == ("GSLC", f"https://host/{GRANULE}.h5", 42)


def test_data_url_rejects_an_unknown_granule() -> None:
    with pytest.raises(ValueError, match="is not in CMR"):
        _load().data_url(GRANULE, FakeCMR([]))
