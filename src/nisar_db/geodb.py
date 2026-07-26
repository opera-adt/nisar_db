from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import MultiPolygon

from .download import download_earthdata_granule
from .filenames import NISAR_DB_GRANULE_ID


def get_trackframe_db(
    output_dir: str | Path = ".",
    skip_existing: bool = True,
    granule_id: str = NISAR_DB_GRANULE_ID,
) -> Path:
    """Download the global NISAR TrackFrame database GeoPackage.

    The TrackFrame database is a public CMR granule, so no credentials beyond a
    standard Earthdata Login ``.netrc`` entry are needed. It is the
    ``--nisar-gpkg`` input to ``create-frame-to-bound``, ``create-gslc-csv`` and
    ``create-consistent``.

    Parameters
    ----------
    output_dir : str or Path
        Directory to download into.
    skip_existing : bool
        Reuse the file if it is already in ``output_dir`` (the default): the
        granule is versioned by date, so a present file is the same file.
    granule_id : str
        CMR concept id to fetch. Defaults to the current TrackFrame database;
        pass an older concept id to pin a previous version.

    Returns
    -------
    Path
        Path to the local ``NISAR_TrackFrame_L_<date>.gpkg``.

    Raises
    ------
    RuntimeError
        If the granule yielded no GeoPackage, e.g. a failed or unauthenticated
        download.

    Examples
    --------
    >>> gpkg = get_trackframe_db("data/")  # doctest: +SKIP
    >>> gpkg.name  # doctest: +SKIP
    'NISAR_TrackFrame_L_20250909.gpkg'

    """
    # The granule advertises a browse page under the same CMR relation as the
    # GeoPackage; without the suffix filter it lands as a stray HTML file.
    paths = download_earthdata_granule(
        granule_id,
        output_dir=output_dir,
        skip_existing=skip_existing,
        filename_suffix=".gpkg",
    )
    if not paths:
        raise RuntimeError(
            f"No GeoPackage downloaded for granule {granule_id}. Check network "
            "access and that ~/.netrc holds valid Earthdata Login credentials."
        )
    return Path(paths[0])


def load_trackframe_db(
    output_dir: str | Path = ".",
    skip_existing: bool = True,
    granule_id: str = NISAR_DB_GRANULE_ID,
) -> gpd.GeoDataFrame:
    """Download (if needed) and read the NISAR TrackFrame database.

    Thin wrapper over :func:`get_trackframe_db` for the common case of wanting
    the frames in memory rather than the path.

    Examples
    --------
    >>> frames = load_trackframe_db("data/")  # doctest: +SKIP
    >>> sorted(frames.columns)[:3]  # doctest: +SKIP
    ['crossesDateline', 'endAX', 'endCY']

    """
    return gpd.read_file(
        get_trackframe_db(
            output_dir=output_dir,
            skip_existing=skip_existing,
            granule_id=granule_id,
        )
    )


def convert_to_gdf(df: pd.DataFrame, nisar_db: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Merge NISAR TrackFrame geometry onto ``df`` by relative orbit and frame.

    Parameters
    ----------
    df : pd.DataFrame
        Table with ``relative_orbit`` and ``track_frame`` columns.
    nisar_db : gpd.GeoDataFrame
        NISAR TrackFrame frames providing ``track``, ``frame``,
        ``passDirection`` and ``geometry``.

    Returns
    -------
    gpd.GeoDataFrame
        ``df`` with the matching frame geometry attached.

    """
    df = df.copy()
    df["relative_orbit"] = df["relative_orbit"].astype(int)
    df["track_frame"] = df["track_frame"].astype(int)
    nisar_db = nisar_db.copy()
    nisar_db["track"] = nisar_db["track"].astype(int)
    nisar_db["frame"] = nisar_db["frame"].astype(int)

    df_merged = pd.merge(
        df,
        nisar_db[["track", "frame", "passDirection", "geometry"]],
        left_on=["relative_orbit", "track_frame"],
        right_on=["track", "frame"],
        how="left",
    )
    gdf = gpd.GeoDataFrame(df_merged, geometry="geometry", crs=nisar_db.crs)
    gdf = gdf.drop(columns=["track", "frame"])
    return gdf


def get_opera_na_shape() -> MultiPolygon:
    """Read the OPERA North America geometry as a single shapely multipolygon."""
    url = (
        "https://raw.githubusercontent.com/"
        "nasa/opera-sds-pcm/develop/geo/data/north_america_opera_expanded.geojson"
    )
    na_gpd = gpd.read_file(url)
    return na_gpd.geometry.union_all()


def filter_frames_to_na(
    nisar_gdf: gpd.GeoDataFrame,
    na_shape: MultiPolygon | None = None,
) -> gpd.GeoDataFrame:
    """Return the subset of NISAR frames intersecting OPERA North America.

    A frame is kept if its geometry ``intersects`` or ``touches`` the OPERA NA
    polygon. Pass ``na_shape`` to reuse a previously fetched polygon and avoid
    re-downloading it.

    Parameters
    ----------
    nisar_gdf : gpd.GeoDataFrame
        NISAR TrackFrame frames (with a ``geometry`` column).
    na_shape : MultiPolygon, optional
        Pre-fetched OPERA NA geometry; fetched via :func:`get_opera_na_shape`
        when omitted.

    """
    if na_shape is None:
        na_shape = get_opera_na_shape()
    return nisar_gdf[
        nisar_gdf.geometry.intersects(na_shape) | nisar_gdf.geometry.touches(na_shape)
    ]
