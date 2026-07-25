"""Integration tests: build an S3 catalog table and query it back.

Exercises products_to_catalog_df -> _write_catalog -> _load_catalog ->
query_catalog across CSV and DuckDB backends, without touching S3.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nisar_db.s3_catalog import _write_catalog as write_catalog
from nisar_db.s3_catalog import (
    products_to_catalog_df,
    query_catalog,
)


def test_products_to_catalog_df_shape(s3_products) -> None:
    df = products_to_catalog_df(s3_products)
    assert len(df) == 2
    assert set(df["track"]) == {128}
    assert set(df["frame"]) == {129}
    assert set(df["crid"]) == {"P01234", "P09999"}
    # 2 GB / 3 GB objects -> size_gb column.
    assert sorted(df["size_gb"]) == pytest.approx([2.0, 3.0])


@pytest.mark.parametrize("suffix", [".csv", ".duckdb"])
def test_catalog_roundtrip_and_filters(
    s3_products, tmp_path: Path, suffix: str
) -> None:
    df = products_to_catalog_df(s3_products)
    catalog = tmp_path / f"catalog{suffix}"
    write_catalog(df, catalog)
    assert catalog.exists()

    # Exact-match filters.
    assert len(query_catalog(catalog, track=128)) == 2
    assert len(query_catalog(catalog, track=999)) == 0
    assert len(query_catalog(catalog, direction="A")) == 2
    assert len(query_catalog(catalog, polarization="hh")) == 2  # case-insensitive

    # crid_min keeps only the latest processing version.
    latest = query_catalog(catalog, crid_min="P09999")
    assert list(latest["crid"]) == ["P09999"]


def test_unsupported_format_raises(s3_products, tmp_path: Path) -> None:
    df = products_to_catalog_df(s3_products)
    with pytest.raises(ValueError, match="Unsupported catalog format"):
        write_catalog(df, tmp_path / "catalog.xlsx")
