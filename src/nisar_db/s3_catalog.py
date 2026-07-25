"""Scan-once / query-many catalog for NISAR products in an S3 bucket.

S3 ``LIST`` can only filter by key prefix, so an ad-hoc track/frame search has
to enumerate the whole bucket every time (minutes). The efficient pattern is to
enumerate **once** into a persisted index, then answer every subsequent query as
a fast local filter (milliseconds):

    >>> from nisar_db.s3_catalog import build_s3_catalog, query_catalog
    >>> build_s3_catalog("nisar-ops-rs-fwd", "products/L2_L_GSLC/",
    ...                   "gslc_catalog.parquet", profile="saml-pub")
    >>> df = query_catalog("gslc_catalog.parquet", track=34, frame=20)

The catalog is a flat table (one row per granule). Output format is chosen from
the file suffix: ``.parquet`` (needs pyarrow), ``.duckdb``/``.db`` (needs
duckdb), or ``.csv``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Union

import pandas as pd

from .search_nisar import NISARProduct, ProductType, search_s3_products

logger = logging.getLogger(__name__)

# Columns written to the catalog, in order.
_CATALOG_COLUMNS = [
    "granule_id",
    "product_type",
    "track",
    "frame",
    "direction",
    "cycle",
    "polarization",
    "mode",
    "coverage",
    "crid",
    "full_frame",
    "joint_observation",
    "start_datetime",
    "end_datetime",
    "production_datetime",
    "url",
    "s3_key",
    "size_gb",
]


def products_to_catalog_df(products: List[NISARProduct]) -> pd.DataFrame:
    """Flatten NISARProducts (from :func:`search_s3_products`) into a catalog table."""
    rows = []
    for p in products:
        meta = p.metadata or {}
        size_bytes = meta.get("s3_size_bytes")
        rows.append(
            {
                "granule_id": p.granule_id,
                "product_type": p.product_type.value,
                "track": p.track,
                "frame": p.frame,
                "direction": p.direction,
                "cycle": p.cycle,
                "polarization": p.polarization,
                "mode": meta.get("mode"),
                "coverage": meta.get("coverage"),
                "crid": p.crid or meta.get("crid"),
                "full_frame": p.full_frame,
                "joint_observation": p.joint_observation,
                "start_datetime": p.start_datetime,
                "end_datetime": p.end_datetime,
                # Granule names carry no production time; the S3 object's
                # LastModified is when the product landed in the bucket.
                "production_datetime": meta.get("s3_last_modified"),
                "url": p.url,
                "s3_key": meta.get("s3_key"),
                "size_gb": (size_bytes / 1e9) if size_bytes is not None else None,
            }
        )
    return pd.DataFrame(rows, columns=_CATALOG_COLUMNS)


def _write_catalog(df: pd.DataFrame, output: Path) -> None:
    """Write the catalog table to parquet / duckdb / csv based on the suffix."""
    suffix = output.suffix.lower()
    output.parent.mkdir(parents=True, exist_ok=True)
    if suffix == ".parquet":
        df.to_parquet(output, index=False)  # needs pyarrow or fastparquet
    elif suffix in (".duckdb", ".db"):
        import duckdb

        con = duckdb.connect(str(output))
        con.execute("CREATE OR REPLACE TABLE products AS SELECT * FROM df")
        con.close()
    elif suffix == ".csv":
        df.to_csv(output, index=False)
    else:
        raise ValueError(
            f"Unsupported catalog format '{suffix}' (use .parquet/.duckdb/.csv)"
        )


def _load_catalog(path: Union[str, Path]) -> pd.DataFrame:
    """Load a catalog written by :func:`build_s3_catalog`."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        df = pd.read_parquet(path)
    elif suffix in (".duckdb", ".db"):
        import duckdb

        df = duckdb.connect(str(path)).execute("SELECT * FROM products").df()
    elif suffix == ".csv":
        df = pd.read_csv(
            path,
            parse_dates=["start_datetime", "end_datetime", "production_datetime"],
        )
    else:
        raise ValueError(
            f"Unsupported catalog format '{suffix}' (use .parquet/.duckdb/.csv)"
        )
    return df


def build_s3_catalog(
    bucket: str,
    prefix: str,
    output: Union[str, Path],
    *,
    profile: Optional[str] = None,
    region: str = "us-west-2",
    product_type: Union[str, ProductType] = ProductType.GSLC,
    max_workers: int = 8,
) -> Path:
    """Enumerate an S3 bucket once and persist a queryable NISAR product catalog.

    Parameters
    ----------
    bucket, prefix, profile, region, product_type, max_workers :
        Passed to :func:`nisar_db.search_nisar.search_s3_products` (unfiltered:
        every granule under ``prefix`` is catalogued).
    output : str or Path
        Catalog file to write; format is inferred from the suffix
        (``.parquet`` / ``.duckdb`` / ``.csv``).

    Returns
    -------
    Path
        The written catalog path.

    """
    output = Path(output)
    logger.info(
        f"Building {product_type} catalog from s3://{bucket}/{prefix} -> {output}"
    )

    products = search_s3_products(
        bucket,
        prefix,
        profile=profile,
        region=region,
        product_type=product_type,
        max_workers=max_workers,
        max_results=10**9,  # no cap: catalogue everything
    )

    df = products_to_catalog_df(products)
    _write_catalog(df, output)
    logger.info(f"Wrote {len(df):,} granules to {output}")
    return output


def query_catalog(
    path: Union[str, Path],
    *,
    track: Optional[int] = None,
    frame: Optional[int] = None,
    direction: Optional[str] = None,
    cycle: Optional[int] = None,
    polarization: Optional[str] = None,
    mode: Optional[str] = None,
    crid: Optional[str] = None,
    crid_min: Optional[str] = None,
    product_type: Optional[str] = None,
    full_frame: Optional[bool] = None,
    joint_observation: Optional[bool] = None,
    start_datetime: Optional[datetime] = None,
    end_datetime: Optional[datetime] = None,
) -> pd.DataFrame:
    """Load a built catalog and return the rows matching the given filters.

    All filters are exact-match except ``crid_min`` (keeps ``crid >= crid_min``,
    the string comparison used to select the latest processing version) and the
    ``start_datetime``/``end_datetime`` range (on sensing start).
    """
    df = _load_catalog(path)
    mask = pd.Series(True, index=df.index)

    for col, val in [
        ("track", track),
        ("frame", frame),
        ("cycle", cycle),
        ("mode", mode),
        ("crid", crid),
        ("full_frame", full_frame),
        ("joint_observation", joint_observation),
    ]:
        if val is not None:
            mask &= df[col] == val

    if direction is not None:
        mask &= df["direction"].astype(str).str.upper().str[0] == direction.upper()[0]
    if polarization is not None:
        mask &= df["polarization"].astype(str).str.upper() == polarization.upper()
    if product_type is not None:
        mask &= df["product_type"].astype(str).str.upper() == product_type.upper()
    if crid_min is not None:
        mask &= df["crid"].astype(str) >= crid_min
    if start_datetime is not None:
        mask &= df["start_datetime"] >= pd.Timestamp(start_datetime)
    if end_datetime is not None:
        mask &= df["start_datetime"] <= pd.Timestamp(end_datetime)

    return df[mask].reset_index(drop=True)


def catalog_to_gdf(
    catalog: Union[str, Path, pd.DataFrame],
    nisar_frames,
    *,
    add_na_flag: bool = True,
):
    """Join a catalog to NISAR frame geometries, returning a GeoDataFrame.

    Attaches each granule's frame polygon (and ``passDirection``) by matching on
    (track, frame) — the geometry step from the OPS notebook, minus the manual
    ``_convert_to_gdf`` helper.

    Parameters
    ----------
    catalog : str | Path | pd.DataFrame
        A catalog DataFrame (from :func:`query_catalog` / :func:`build_s3_catalog`)
        or a path to a built catalog file.
    nisar_frames : str | Path | geopandas.GeoDataFrame
        NISAR TrackFrame frames (columns ``track``, ``frame``, ``geometry`` and,
        if present, ``passDirection``), or a path to read them from.
    add_na_flag : bool
        Add an ``is_north_america`` boolean column (frame intersects/touches the
        OPERA North America polygon). Requires network to fetch the polygon.

    Returns
    -------
    geopandas.GeoDataFrame
        The catalog with ``geometry`` (and ``passDirection`` / ``is_north_america``).

    """
    import geopandas as gpd

    from .geodb import get_opera_na_shape

    df = _load_catalog(catalog) if isinstance(catalog, (str, Path)) else catalog.copy()

    frames = (
        nisar_frames
        if isinstance(nisar_frames, gpd.GeoDataFrame)
        else gpd.read_file(nisar_frames)
    ).copy()

    df["track"] = df["track"].astype(int)
    df["frame"] = df["frame"].astype(int)
    frames["track"] = frames["track"].astype(int)
    frames["frame"] = frames["frame"].astype(int)

    cols = ["track", "frame", "geometry"]
    if "passDirection" in frames.columns:
        cols.insert(2, "passDirection")

    merged = df.merge(frames[cols], on=["track", "frame"], how="left")
    gdf = gpd.GeoDataFrame(merged, geometry="geometry", crs=frames.crs)

    if add_na_flag:
        na_shape = get_opera_na_shape()
        gdf["is_north_america"] = gdf.geometry.intersects(
            na_shape
        ) | gdf.geometry.touches(na_shape)

    return gdf
