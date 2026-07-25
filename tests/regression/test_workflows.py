"""End-to-end regression tests for the core user workflows.

Each test drives a full workflow with deterministic, network-free inputs and
pins the observable output:

1. query               -> query_catalog over a built catalog
2. download            -> download_from_url with a mocked HTTP session
3. make consistent     -> make_consistent_gslc_json (catalog CSV + frames GPKG)
4. make blackout dates -> create_blackout_dates_json
5. make reference date -> create_reference_dates_json
"""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Point

from nisar_db import download
from nisar_db.blackout import create_blackout_dates_json, create_reference_dates_json
from nisar_db.consistent_gslc import make_consistent_gslc_json
from nisar_db.s3_catalog import _write_catalog, products_to_catalog_df, query_catalog


# ---------------------------------------------------------------------------
# 1. query
# ---------------------------------------------------------------------------
def test_workflow_query(s3_products, tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.csv"
    _write_catalog(products_to_catalog_df(s3_products), catalog)

    # Latest-version selection: crid_min keeps only P09999.
    latest = query_catalog(catalog, track=128, crid_min="P09999")
    assert list(latest["crid"]) == ["P09999"]
    assert latest.iloc[0]["track"] == 128
    assert latest.iloc[0]["frame"] == 129

    # A non-matching filter yields an empty frame (not an error).
    assert query_catalog(catalog, frame=999).empty


# ---------------------------------------------------------------------------
# 2. download
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.headers = {"content-length": str(sum(len(c) for c in chunks))}

    def raise_for_status(self) -> None:
        pass

    def iter_content(self, chunk_size: int):  # noqa: ARG002
        return iter(self._chunks)


class _FakeSession:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def get(self, url: str, stream: bool = False, timeout: int = 60):  # noqa: ARG002
        return _FakeResponse([self._payload])


def test_workflow_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = b"NISAR-granule-bytes"
    monkeypatch.setattr(download.requests, "Session", lambda: _FakeSession(payload))

    url = "https://example.com/data/GRANULE.h5?token=abc"
    result = download.download_from_url(url, output_dir=tmp_path, show_progress=False)

    assert result == str(tmp_path / "GRANULE.h5")
    assert (tmp_path / "GRANULE.h5").read_bytes() == payload

    # Re-running with skip_existing short-circuits (no second download attempt).
    again = download.download_from_url(url, output_dir=tmp_path, skip_existing=True)
    assert again == result


# ---------------------------------------------------------------------------
# 3. make consistent catalog
# ---------------------------------------------------------------------------
@pytest.fixture
def frames_gpkg(tmp_path: Path) -> Path:
    """A minimal NISAR frames GeoPackage with a frame_idx column."""
    gdf = gpd.GeoDataFrame(
        {
            "track": [128, 200],
            "frame": [129, 300],
            "frame_idx": [1001, 2002],
            "geometry": [Point(0, 0), Point(1, 1)],
        },
        crs="EPSG:4326",
    )
    path = tmp_path / "frames.gpkg"
    gdf.to_file(path, driver="GPKG")
    return path


def test_workflow_make_consistent(
    consistent_catalog_df: pd.DataFrame, frames_gpkg: Path, tmp_path: Path
) -> None:
    catalog_csv = tmp_path / "gslc_catalog.csv"
    consistent_catalog_df.to_csv(catalog_csv, index=False)
    out = tmp_path / "consistent.json"

    returned = make_consistent_gslc_json(catalog_csv, frames_gpkg, output=out)
    assert returned == out

    payload = json.loads(out.read_text())
    data = payload["data"]

    # Frames keyed by frame_idx; frame (128,129)->1001 keeps two 4005/F dates.
    assert set(data) == {"1001", "2002"}
    assert data["1001"]["common_mode"] == "4005"
    assert data["1001"]["common_coverage"] == "F"
    assert data["1001"]["sensing_time_list"] == [
        "2024-06-01T00:00:00",
        "2024-06-13T00:00:00",
    ]
    assert data["2002"]["common_mode"] == "7700"
    assert data["2002"]["common_coverage"] == "P"


def test_workflow_make_consistent_with_blackout(
    consistent_catalog_df: pd.DataFrame, frames_gpkg: Path, tmp_path: Path
) -> None:
    catalog_csv = tmp_path / "gslc_catalog.csv"
    consistent_catalog_df.to_csv(catalog_csv, index=False)

    # Black out the 2024-06-13 acquisition of frame_idx 1001.
    blackout = tmp_path / "blackout.json"
    create_blackout_dates_json(
        {"1001": [("2024-06-10", "2024-06-20")]}, output=blackout
    )

    out = tmp_path / "consistent.json"
    make_consistent_gslc_json(
        catalog_csv, frames_gpkg, output=out, blackout_file=blackout
    )
    data = json.loads(out.read_text())["data"]
    assert data["1001"]["sensing_time_list"] == ["2024-06-01T00:00:00"]


# ---------------------------------------------------------------------------
# 4. make blackout dates
# ---------------------------------------------------------------------------
def test_workflow_make_blackout(tmp_path: Path) -> None:
    out = tmp_path / "nisar-blackout.json"
    returned = create_blackout_dates_json(
        {5827: [("2025-12-01", "2026-01-15")]}, output=out, description="snow"
    )
    assert returned == out
    payload = json.loads(out.read_text())
    assert payload["blackout_dates"] == {"5827": [["2025-12-01", "2026-01-15"]]}
    assert payload["metadata"]["description"] == "snow"
    assert out.with_suffix(".json.zip").exists()


# ---------------------------------------------------------------------------
# 5. make reference date
# ---------------------------------------------------------------------------
def test_workflow_make_reference(tmp_path: Path) -> None:
    out = tmp_path / "nisar-reference.json"
    returned = create_reference_dates_json(
        {5827: ["2026-01-15"], "5830": ["2025-12-01", "2026-06-01"]}, output=out
    )
    assert returned == out
    payload = json.loads(out.read_text())
    assert payload["data"] == {
        "5827": ["2026-01-15"],
        "5830": ["2025-12-01", "2026-06-01"],
    }
    assert out.with_suffix(".json.zip").exists()
