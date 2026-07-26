#!/usr/bin/env python
"""Build the NISAR snow-analysis table from DISP-S1's existing snow windows.

A shortcut around `fetch_gefs.py` + `derive_blackout_windows.py`: instead of
re-running the weather analysis, borrow the per-frame windows `burst_db`
already published for DISP-S1 (`snow-analysis/opera-region4-snow-analysis`)
and resample them onto NISAR frames by spatial overlap. Snow onset and thaw are
properties of the ground, not of the sensor, so a NISAR frame inherits the
windows of the DISP-S1 frames that cover it.

Every date is mapped onto the same water year the DISP-S1 analysis used
(August-July, see `snow_month_filter.PIVOT_MONTH`), averaged across donors
weighted by the area each one contributes, and mapped back. Averaging in
water-year offsets is what keeps a November-to-April window from wrapping
through the calendar boundary.

The output matches what `derive_blackout_windows.py` writes, so
`nisar-db create-blackout-dates` consumes it unchanged. It carries three extra
provenance columns - `n_donors`, `donor_coverage`, `winter_coverage` - and no
`n_seasons`, which is not recoverable from a collapsed window table.

Examples
--------
Against a local `burst_db` checkout::

    python transfer_disp_s1_windows.py \\
        --disp-s1-table ../../../burst_db/snow-analysis/opera-region4-snow-analysis.parquet \\
        --frames-gpkg ../../notebooks/opera-nisar-disp-frames.gpkg \\
        --outfile nisar-snow-analysis-from-disp-s1.geojson

The same table straight off GitHub::

    python transfer_disp_s1_windows.py \\
        --disp-s1-table https://raw.githubusercontent.com/opera-adt/burst_db/main/snow-analysis/opera-region4-snow-analysis.geojson \\
        --frames-gpkg ../../notebooks/opera-nisar-disp-frames.gpkg

Then build the blackout JSON from it::

    nisar-db create-blackout-dates \\
        --input-file nisar-snow-analysis-from-disp-s1.geojson \\
        --max-default-duration 180 \\
        --output-file nisar-blackout-dates.json

"""

from __future__ import annotations

import argparse
import warnings
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

MODES = ("conservative", "median", "aggressive")
PIVOT_MONTH = 8
# Carried through to the output so the thresholds behind the windows stay
# visible; DISP-S1 ran one setting for the whole table.
PASSTHROUGH_COLUMNS = (
    "mask_fraction",
    "snow_threshold",
    "freezing_threshold",
    "temp_var",
)


def read_table(path: str) -> gpd.GeoDataFrame:
    """Read the DISP-S1 snow-analysis table from a path or URL.

    GeoJSON has no timestamp type, so the window columns come back as dates
    only when the driver guesses right; parse them here instead.
    """
    if str(path).endswith(".parquet"):
        table = gpd.read_parquet(path)
    else:
        table = gpd.read_file(path)

    windows = [c for c in table.columns if c.startswith(("start_", "end_"))]
    table[windows] = table[windows].apply(pd.to_datetime)
    return table


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


def _planar_area(gdf: gpd.GeoDataFrame) -> pd.Series:
    """Area in square degrees, used only for ratios and weights.

    Every quantity built from this is a ratio between geometries of the same
    frame, so the latitude distortion of an unprojected area cancels out.
    """
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", "Geometry is in a geographic CRS")
        return gdf.area


def _centroid_latitude(gdf: gpd.GeoDataFrame) -> pd.Series:
    """Centroid latitude of each frame, for reporting only."""
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", "Geometry is in a geographic CRS")
        return gdf.geometry.centroid.y


def _to_offsets(ts: pd.Series, pivot_month: int) -> pd.Series:
    """Convert window timestamps to day offsets into the water year."""
    base = pd.Timestamp(year=2000, month=pivot_month, day=1)
    pivoted = pd.to_datetime(
        {
            "year": np.where(ts.dt.month < pivot_month, 2001, 2000),
            "month": ts.dt.month,
            "day": ts.dt.day,
        }
    )
    pivoted[ts.isna()] = pd.NaT
    return (pivoted - base).dt.days


def _from_offsets(days: np.ndarray, pivot_month: int) -> pd.DatetimeIndex:
    """Invert :func:`_to_offsets`, rounding to whole days."""
    base = pd.Timestamp(year=2000, month=pivot_month, day=1)
    return base + pd.to_timedelta(np.round(days), unit="D")


def transfer_windows(
    frames: gpd.GeoDataFrame,
    donors: gpd.GeoDataFrame,
    *,
    pivot_month: int = PIVOT_MONTH,
    min_winter_coverage: float = 0.5,
) -> gpd.GeoDataFrame:
    """Resample DISP-S1 blackout windows onto NISAR frames.

    Parameters
    ----------
    frames : gpd.GeoDataFrame
        NISAR frames carrying `frame_idx`, `track`, `frame` and `geometry`.
    donors : gpd.GeoDataFrame
        DISP-S1 snow-analysis table: `start_*`/`end_*` per mode plus geometry.
        Rows whose windows are `NaT` are donors that saw no winter, and count
        against `min_winter_coverage` rather than being ignored.
    pivot_month : int
        Month starting the water year. Must match the one the DISP-S1 analysis
        used, or a late thaw lands in the wrong pivot year and windows invert.
    min_winter_coverage : float
        Fraction of a NISAR frame's donor area that must have a winter before
        the frame gets a window. Below it the frame is emitted with `NaT`
        windows, which `nisar-db create-blackout-dates` skips.

    Returns
    -------
    gpd.GeoDataFrame
        One row per NISAR frame that any donor overlaps, with the same
        `start_*`/`end_*`/`blackout_duration_*` columns as
        `derive_blackout_windows.py`.

    Notes
    -----
    Overlap areas are planar degrees squared. The latitude distortion is
    common to every donor of a given frame, so it cancels in the weights.
    Dateline-crossing frames are stored split into two-part MultiPolygons on
    both sides, so the intersection needs no unwrapping.

    """
    donors = donors.copy()
    donors["donor_row"] = np.arange(len(donors))
    donors["has_winter"] = donors[f"start_{MODES[0]}"].notna()
    for mode in MODES:
        for bound in ("start", "end"):
            col = f"{bound}_{mode}"
            donors[f"offset_{col}"] = _to_offsets(donors[col], pivot_month)

    offset_columns = [f"offset_{b}_{m}" for m in MODES for b in ("start", "end")]
    pieces = gpd.overlay(
        frames[["frame_idx", "geometry"]],
        donors[
            [
                "donor_row",
                "has_winter",
                *PASSTHROUGH_COLUMNS,
                *offset_columns,
                "geometry",
            ]
        ],
        how="intersection",
        keep_geom_type=True,
    )
    pieces["overlap_area"] = _planar_area(pieces)

    # DISP-S1 frames overlap each other, so the coverage fractions have to come
    # from the dissolved footprint; the summed piece areas only weight the mean.
    covered = _planar_area(pieces.dissolve(by="frame_idx"))
    wintry = pieces[pieces.has_winter]
    winter_covered = _planar_area(wintry.dissolve(by="frame_idx")).reindex(
        covered.index, fill_value=0.0
    )
    winter_coverage = winter_covered / covered

    # Only donors that saw a winter carry usable dates; the rest have already
    # had their say through winter_coverage.
    weights = wintry.overlap_area
    weighted = (
        wintry[offset_columns].mul(weights, axis=0).groupby(wintry.frame_idx).sum()
    )
    weighted = weighted.div(weights.groupby(wintry.frame_idx).sum(), axis=0)
    weighted = weighted.reindex(covered.index)
    weighted[winter_coverage < min_winter_coverage] = np.nan

    out = frames.set_index("frame_idx").loc[covered.index].reset_index()
    for mode in MODES:
        for bound in ("start", "end"):
            col = f"{bound}_{mode}"
            out[col] = _from_offsets(
                weighted[f"offset_{col}"].to_numpy(dtype=float), pivot_month
            ).to_numpy()
        # `.abs()` matches `snow_month_filter.get_blackout_windows`: an
        # aggressive window inverts when a frame's winters share no common
        # period, and the upstream table reports that as a positive duration.
        out[f"blackout_duration_{mode}"] = (
            (out[f"end_{mode}"] - out[f"start_{mode}"]).abs().dt.days.astype(float)
        )

    for column in PASSTHROUGH_COLUMNS:
        # One setting for the whole DISP-S1 table; keep it if that holds. Donors
        # without a winter carry no thresholds, so they do not count as a second.
        values = pieces[column].dropna().unique()
        out[column] = values[0] if len(values) == 1 else pd.NA

    frame_area = _planar_area(frames.set_index("frame_idx"))
    out["n_donors"] = pieces.groupby("frame_idx").donor_row.nunique().to_numpy()
    out["donor_coverage"] = (covered / frame_area.loc[covered.index]).to_numpy()
    out["winter_coverage"] = winter_coverage.to_numpy()
    out["source"] = "disp-s1-transfer"
    return gpd.GeoDataFrame(out, geometry="geometry", crs=frames.crs)


def write_table(gdf: gpd.GeoDataFrame, outfile: Path) -> Path:
    """Write the window table as Parquet or GeoJSON.

    Mirrors `derive_blackout_windows.write_table`: the downstream builder reads
    `frame_id`, while every other NISAR artifact keys on `frame_idx`. GeoJSON
    has no timestamp type, so the window columns go out as `YYYY-MM-DD`
    strings; `pandas.Timestamp` parses them back on read.
    """
    gdf = gdf.copy()
    gdf["frame_id"] = gdf["frame_idx"]

    if outfile.suffix == ".parquet":
        gdf.to_parquet(outfile)
        return outfile

    for col in [c for c in gdf.columns if c.startswith(("start_", "end_"))]:
        gdf[col] = gdf[col].dt.strftime("%Y-%m-%d")
    gdf.to_file(outfile, driver="GeoJSON")
    return outfile


def main() -> None:
    """Transfer the DISP-S1 windows and write the NISAR window table."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--disp-s1-table",
        required=True,
        help=(
            "burst_db snow-analysis table (path or URL), "
            "e.g. snow-analysis/opera-region4-snow-analysis.parquet."
        ),
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
        default=Path("nisar-snow-analysis-from-disp-s1.geojson"),
        help="Output table (.geojson or .parquet).",
    )
    parser.add_argument(
        "--tracks", type=int, nargs="+", help="Limit to these NISAR tracks."
    )
    parser.add_argument(
        "--pivot-month",
        type=int,
        default=PIVOT_MONTH,
        help="Month starting the water year used to average the windows.",
    )
    parser.add_argument(
        "--min-winter-coverage",
        type=float,
        default=0.5,
        help=(
            "Fraction of a frame's donor area that must have a winter before "
            "the frame gets a blackout window."
        ),
    )
    args = parser.parse_args()

    frames = load_frames(args.frames_gpkg, args.tracks)
    donors = read_table(args.disp_s1_table)
    out = transfer_windows(
        frames,
        donors,
        pivot_month=args.pivot_month,
        min_winter_coverage=args.min_winter_coverage,
    )

    uncovered = frames[~frames.frame_idx.isin(out.frame_idx)]
    with_window = int(out["start_median"].notna().sum())
    print(
        f"{len(frames)} NISAR frames: {with_window} with a window, "
        f"{len(out) - with_window} snow-free, {len(uncovered)} with no DISP-S1 donor"
    )
    inverted = int((out.end_aggressive < out.start_aggressive).sum())
    if inverted:
        print(
            f"  {inverted} frames have an inverted aggressive window (winters with "
            "no period in common); only used when the median window is long"
        )
    if len(uncovered):
        # DISP-S1 analyzed a fixed frame set; anything outside it is a genuine
        # gap, and its latitude is the quickest read on how much that matters.
        lat = _centroid_latitude(uncovered)
        print(
            f"  no-donor frames reach {lat.max():.1f} deg N "
            f"(median {lat.median():.1f} deg N); they get no blackout window"
        )
    print(f"Wrote {write_table(out, args.outfile)}")


if __name__ == "__main__":
    main()
