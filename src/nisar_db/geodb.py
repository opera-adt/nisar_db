import geopandas as gpd
import pandas as pd
from shapely.geometry import MultiPolygon

def convert_to_gdf(df: pd.DataFrame, nisar_db: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
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
    url = "https://raw.githubusercontent.com/nasa/opera-sds-pcm/develop/geo/data/north_america_opera_expanded.geojson"
    na_gpd = gpd.read_file(url)
    return na_gpd.geometry.unary_union