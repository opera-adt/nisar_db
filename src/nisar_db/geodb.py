import geopandas as gpd
import pandas as pd
from shapely.geometry import MultiPolygon


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
