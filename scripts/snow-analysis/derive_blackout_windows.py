#!/usr/bin/env python
"""Derive per-frame snow blackout windows for NISAR frames.

Step 2 of the NISAR snow analysis: reads the local GEFS subset written by
``fetch_gefs.py`` plus the NISAR frames GeoPackage, and writes the snow-analysis
table that ``nisar-db create-blackout-dates`` turns into yearly blackout
windows.

The output carries a ``frame_id`` column that duplicates ``frame_idx``: the
downstream builder (``nisar_db.catalog.create_blackout_dates``) reads
``frame_id``, while every other NISAR artifact keys on ``frame_idx``, and the
blackout JSON must come out keyed the same way as the consistent-GSLC JSON.

Examples
--------
Whole North America run, GeoJSON out::

    python derive_blackout_windows.py \\
        --gefs-zarr noaa_gefs.zarr \\
        --frames-gpkg notebooks/opera-nisar-disp-frames.gpkg \\
        --outfile nisar-snow-analysis.geojson

Then build the blackout JSON from it::

    nisar-db create-blackout-dates \\
        --input-file nisar-snow-analysis.geojson \\
        --max-default-duration 180 \\
        --output-file nisar-blackout-dates.json

One frame, looser thresholds, Parquet out (keeps the timestamp dtypes)::

    python derive_blackout_windows.py \\
        --gefs-zarr noaa_gefs.zarr \\
        --frames-gpkg notebooks/opera-nisar-disp-frames.gpkg \\
        --frame-idx 6187 --snow-threshold 2 --outfile frame6187.parquet

"""

from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd
import xarray as xr

from snow_month_filter import aggregate_weather, get_blackout_windows


def load_frames(frames_gpkg: Path, tracks: list[int] | None) -> gpd.GeoDataFrame:
    """Read the NISAR frames GeoPackage, keeping only the needed columns."""
    gdf = gpd.read_file(frames_gpkg)
    if "frame_idx" not in gdf.columns:
        raise ValueError(
            f"{frames_gpkg} has no 'frame_idx' column. "
            "Run `nisar-db create-frame-to-bound` to produce the filtered GPKG."
        )
    if tracks:
        gdf = gdf[gdf.track.astype(int).isin(tracks)]
    return gdf[["track", "frame", "frame_idx", "geometry"]].reset_index(drop=True)


def write_table(gdf: gpd.GeoDataFrame, outfile: Path) -> Path:
    """Write the window table as Parquet or GeoJSON.

    GeoJSON has no timestamp type, so the window columns are serialized as
    ``YYYY-MM-DD`` strings; ``pandas.Timestamp`` parses them back on read.
    """
    gdf = gdf.copy()
    # The pivot year (2000/2001) is an artifact of the wrap-around logic; only
    # month/day are meaningful downstream.
    gdf["frame_id"] = gdf["frame_idx"]

    if outfile.suffix == ".parquet":
        gdf.to_parquet(outfile)
        return outfile

    for col in [c for c in gdf.columns if c.startswith(("start_", "end_"))]:
        gdf[col] = gdf[col].dt.strftime("%Y-%m-%d")
    gdf.to_file(outfile, driver="GeoJSON")
    return outfile


def main() -> None:
    """Run the snow analysis and write the per-frame window table."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--gefs-zarr", type=Path, required=True, help="Local GEFS Zarr from fetch_gefs."
    )
    parser.add_argument(
        "--frames-gpkg",
        type=Path,
        required=True,
        help="NISAR frames GPKG carrying frame_idx.",
    )
    parser.add_argument(
        "--outfile",
        type=Path,
        default=Path("nisar-snow-analysis.geojson"),
        help="Output table (.geojson or .parquet).",
    )
    parser.add_argument(
        "--window", default="1W", help="Aggregation window for the weather fields."
    )
    parser.add_argument(
        "--snow-threshold",
        type=float,
        default=3.0,
        help="Snow days per window at or above which a pixel is flagged.",
    )
    parser.add_argument(
        "--freezing-threshold",
        type=float,
        default=-2.0,
        help="Temperature (C) at or below which a pixel is flagged.",
    )
    parser.add_argument(
        "--temp-var",
        choices=["tmin", "tmax"],
        default="tmax",
        help="Aggregated temperature to test against the freezing threshold.",
    )
    parser.add_argument(
        "--mask-fraction",
        type=float,
        default=0.5,
        help="Fraction of in-frame pixels that must be flagged for a bad window.",
    )
    parser.add_argument(
        "--tracks", type=int, nargs="+", default=None, help="Limit to these tracks."
    )
    parser.add_argument(
        "--frame-idx", type=int, default=None, help="Process a single frame_idx."
    )
    parser.add_argument(
        "--debug", action="store_true", help="Report frames skipped and why."
    )
    args = parser.parse_args()

    frames_gdf = load_frames(args.frames_gpkg, args.tracks)
    print(f"Loaded {len(frames_gdf):,} frames from {args.frames_gpkg.name}")

    ds = xr.open_zarr(args.gefs_zarr, consolidated=False)
    agg = aggregate_weather(ds, win=args.window).compute()
    print(f"Aggregated weather to {args.window}: {dict(agg.sizes)}")

    gdf = get_blackout_windows(
        agg,
        frames_gdf,
        snow_threshold=args.snow_threshold,
        freezing_threshold=args.freezing_threshold,
        temp_var=args.temp_var,
        mask_fraction=args.mask_fraction,
        frame_idx=args.frame_idx,
        debug=args.debug,
    )

    n_with_window = int(gdf.start_median.notna().sum())
    print(f"Frames with a detected winter: {n_with_window:,} / {len(gdf):,}")
    if n_with_window:
        print(gdf.blackout_duration_median.describe().to_string())

    write_table(gdf, args.outfile)
    print(f"Wrote {args.outfile}")


if __name__ == "__main__":
    main()
