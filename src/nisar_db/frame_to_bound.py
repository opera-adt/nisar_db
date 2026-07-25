"""Create a frame_to_bound JSON file for NISAR frames in North America.

Analogous to burst_db's frame_to_burst, but for NISAR (frame-based, no burst IDs).
Filters NISAR frames to those intersecting the OPERA North America polygon.

Usage:
    python create_frame_to_bound.py --nisar-gpkg NISAR_TrackFrame_L_20250909.gpkg
    python create_frame_to_bound.py --nisar-gpkg NISAR_TrackFrame_L_20250909.gpkg \
        --output nisar-frame-to-bound.json
"""

from __future__ import annotations

import zipfile
from datetime import datetime
from pathlib import Path

import click
import geopandas as gpd

from nisar_db.geodb import filter_frames_to_na
from nisar_db.io_json import write_zipped_json

#: Attributes carried into the simplified GeoJSON, matching the spirit of
#: burst_db's ``frame-geometries-simple``.
_GEOJSON_COLUMNS = ["frame_idx", "track", "frame", "passDirection", "epsg", "hasLand"]


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
    nisar_df = filter_frames_to_na(nisar_df)

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


def write_frame_geometries_geojson(
    frames_gdf: gpd.GeoDataFrame,
    output: Path,
    tolerance: float = 0.1,
) -> Path:
    """Write the frame polygons as a simplified, zipped GeoJSON.

    The NISAR counterpart of burst_db's ``frame-geometries-simple`` asset: the
    full-resolution polygons live in the GeoPackage, while this is the
    lightweight version meant for web maps and quick spatial joins.

    Parameters
    ----------
    frames_gdf : gpd.GeoDataFrame
        Filtered NISAR frames, carrying a ``frame_idx`` column.
    output : Path
        Destination ``.geojson``; the zip is written alongside as
        ``<output>.zip``.
    tolerance : float
        Douglas-Peucker tolerance in degrees.

    Returns
    -------
    Path
        Path to the written ``.geojson.zip``.

    """
    simplified = frames_gdf[
        [c for c in _GEOJSON_COLUMNS if c in frames_gdf.columns] + ["geometry"]
    ].copy()
    simplified["geometry"] = simplified.geometry.simplify(tolerance)
    simplified.to_file(output, driver="GeoJSON")

    zip_path = output.with_suffix(output.suffix + ".zip")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(output, arcname=output.name)
    return zip_path


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
@click.option(
    "--geojson",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=(
        "Also write the frame polygons as a simplified, zipped GeoJSON to this "
        "path, e.g. opera-nisar-disp-frame-geometries-simple.geojson."
    ),
)
@click.option(
    "--simplify-tolerance",
    type=float,
    default=0.1,
    help="Simplification tolerance, in degrees, for --geojson.",
)
def main(nisar_gpkg: str, output: str, geojson: Path | None, simplify_tolerance: float):
    """Create a frame_to_bound JSON file for NISAR.

    Extracts NISAR frames that intersect the OPERA North America polygon
    and writes their bounding boxes. Analogous to burst_db's frame_to_burst
    file, but without burst IDs since NISAR is frame-based.
    """
    result, filtered_gdf = build_frame_to_bound(nisar_gpkg_path=Path(nisar_gpkg))

    n_frames = len(result["data"])
    click.echo(f"Writing {n_frames} NISAR frame entries.")

    # Write plain JSON alongside the compressed .json.zip
    zip_path = write_zipped_json(output, result)
    click.echo(f"Written to {output}")
    click.echo(f"Written to {zip_path}")

    # Write filtered GeoPackage with actual frame polygons
    # Store the original index as "frame_idx" so search.py can look up by JSON key
    filtered_gdf = filtered_gdf.copy()
    filtered_gdf["frame_idx"] = filtered_gdf.index
    output_name = "opera-nisar-disp-frames"
    gpkg_path = Path(output).parent / f"{output_name}.gpkg"
    filtered_gdf.to_file(gpkg_path, driver="GPKG")
    click.echo(f"Written to {gpkg_path}")

    if geojson is not None:
        zip_path = write_frame_geometries_geojson(
            filtered_gdf, geojson, tolerance=simplify_tolerance
        )
        click.echo(f"Written to {geojson}")
        click.echo(f"Written to {zip_path}")


if __name__ == "__main__":
    main()
