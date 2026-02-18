#!/usr/bin/env python
"""Create a frame_to_bound JSON file for NISAR frames in North America.

Analogous to burst_db's frame_to_burst, but for NISAR (frame-based, no burst IDs).
Filters NISAR frames to those intersecting the OPERA North America polygon.

Usage:
    python create_frame_to_bound.py --nisar-gpkg NISAR_TrackFrame_L_20250909.gpkg
    python create_frame_to_bound.py --nisar-gpkg NISAR_TrackFrame_L_20250909.gpkg \
        --output nisar-frame-to-bound.json
"""

from __future__ import annotations

import json
import zipfile
from datetime import datetime
from pathlib import Path

import click
import geopandas as gpd


def get_opera_na_shape():
    """Read the OPERA North America geometry as a shapely multipolygon."""
    url = (
        "https://raw.githubusercontent.com/"
        "nasa/opera-sds-pcm/develop/geo/data/north_america_opera_expanded.geojson"
    )
    na_gpd = gpd.read_file(url)
    return na_gpd.geometry.union_all()


def build_frame_to_bound(
    nisar_gpkg_path: Path,
) -> tuple[dict, gpd.GeoDataFrame]:
    """Build the frame_to_bound mapping for NISAR frames in North America.

    Returns a tuple of:
    - dict with "data" and "metadata" keys (the JSON output)
    - GeoDataFrame of the filtered NISAR frames (for writing as gpkg)
    """
    nisar_df = gpd.read_file(nisar_gpkg_path)

    # Filter to frames intersecting North America
    opera_na = get_opera_na_shape()
    nisar_df = nisar_df[
        nisar_df.geometry.intersects(opera_na) | nisar_df.geometry.touches(opera_na)
    ]

    click.echo(f"Found {len(nisar_df)} NISAR frames intersecting North America.")

    # Build output dict keyed by frame index
    data_dict: dict[str, dict] = {}
    for idx, row in nisar_df.iterrows():
        data_dict[str(idx)] = {
            "epsg": int(row["epsg"]),
            "is_land": bool(row.get("hasLand", False)),
            "is_north_america": True,
            "xmin": int(row["mapTopLeftX"]),
            "ymin": int(row["mapBottomRightY"]),
            "xmax": int(row["mapBottomRightX"]),
            "ymax": int(row["mapTopLeftY"]),
        }

    metadata = {
        "nisar_gpkg": str(nisar_gpkg_path),
        "last_modified": datetime.now().isoformat(),
        "description": (
            "NISAR frames intersecting OPERA North America, "
            "with their map-projected bounding boxes."
        ),
    }

    return {"data": data_dict, "metadata": metadata}, nisar_df


def write_zipped_json(json_path: str, dict_out: dict, level: int = 6):
    """Write a JSON dictionary to a compressed .json.zip file."""
    json_zip_path = str(json_path) + ".zip"
    with zipfile.ZipFile(
        json_zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=level
    ) as zf:
        zf.writestr(str(Path(json_path).name), json.dumps(dict_out))
    return json_zip_path


@click.command(context_settings={"show_default": True})
@click.option(
    "--nisar-gpkg",
    type=click.Path(exists=True, dir_okay=False),
    required=True,
    help="Path to the NISAR TrackFrame GeoPackage file.",
)
@click.option(
    "--output",
    type=click.Path(dir_okay=False),
    default="opera-nisar-disp-frame-to-bounds.json",
    help="Output JSON filename (will also create a .json.zip).",
)
def main(nisar_gpkg: str, output: str):
    """Create a frame_to_bound JSON file for NISAR.

    Extracts NISAR frames that intersect the OPERA North America polygon
    and writes their bounding boxes. Analogous to burst_db's frame_to_burst
    file, but without burst IDs since NISAR is frame-based.
    """
    result, filtered_gdf = build_frame_to_bound(nisar_gpkg_path=Path(nisar_gpkg))

    n_frames = len(result["data"])
    click.echo(f"Writing {n_frames} NISAR frame entries.")

    # Write plain JSON
    with open(output, "w") as f:
        json.dump(result, f, indent=2)
    click.echo(f"Written to {output}")

    # Write compressed zip
    zip_path = write_zipped_json(output, result)
    click.echo(f"Written to {zip_path}")

    # Write filtered GeoPackage with actual frame polygons
    # Store the original index as "frame_idx" so search.py can look up by JSON key
    filtered_gdf = filtered_gdf.copy()
    filtered_gdf["frame_idx"] = filtered_gdf.index
    output_stem = Path(output).stem.replace(".json", "")
    output_name = "opera-nisar-disp-frames"
    gpkg_path = Path(output).parent / f"{output_name}.gpkg"
    filtered_gdf.to_file(gpkg_path, driver="GPKG")
    click.echo(f"Written to {gpkg_path}")


if __name__ == "__main__":
    main()
