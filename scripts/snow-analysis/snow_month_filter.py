"""Decide which periods to blackout for NISAR frames from GFS/GEFS weather.

NISAR analogue of ``burst_db``'s ``snow-analysis/snow_month_filter.py``: same
water-year logic, keyed on NISAR ``frame_idx`` instead of the DISP-S1
``frame_id``, and aware of the dateline-crossing frames that the NISAR
TrackFrame database stores as two-part MultiPolygons.

Inputs
------
* ``ds`` - an ``xarray.Dataset`` holding at least:
    - ``categorical_snow_surface`` - categorical snow (0/1) per pixel;
    - ``temperature_2m`` - 2 m air temperature (C);
    - a ``time`` dim on 6-hour steps;
    - ``latitude``/``longitude`` dims on the regular 0.25 deg GFS/GEFS grid.
  Produced by ``fetch_gefs.py``.

* ``frames_gdf`` - ``geopandas.GeoDataFrame`` of NISAR frames carrying
  ``frame_idx`` and ``geometry`` (``opera-nisar-disp-frames.gpkg``, written by
  ``nisar-db create-frame-to-bound``).

The end product is the per-frame window table consumed by
``nisar-db create-blackout-dates``; see ``derive_blackout_windows.py``.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import xarray as xr
from shapely.geometry import MultiPolygon, Polygon
from tqdm.auto import tqdm

__all__ = [
    "Mode",
    "aggregate_weather",
    "bad_period_mask",
    "daily_bad_fraction",
    "get_annual_seasons",
    "get_blackout_windows",
    "plot_frame_timeline",
    "summarize_blackouts",
]

# Water year start. August keeps a northern-hemisphere winter contiguous while
# leaving a clean shoulder either side; the same value must be used to find the
# seasons and to collapse them.
PIVOT_MONTH = 8


def aggregate_weather(
    ds: xr.Dataset,
    *,
    snow_var: str = "categorical_snow_surface",
    temp_var: str = "temperature_2m",
    win: str | int = "1W",
    snow_agg: Literal["sum", "max"] = "sum",
) -> xr.Dataset:
    """Aggregate 6-hourly GFS/GEFS fields to a rolling window cadence.

    Parameters
    ----------
    ds : xr.Dataset
        Saved GFS/GEFS dataset.
    snow_var : str
        Name of the categorical-snow variable.
    temp_var : str
        Name of the temperature variable.
    win : str | int
        Resampling window, e.g. ``"1W"`` or ``"3D"``.
    snow_agg : {"sum", "max"}
        Whether snow days are summed or max-ed across ``win``.

    Returns
    -------
    xr.Dataset
        Dataset with ``snow`` (snow days per window), ``tmin`` and ``tmax``.

    Examples
    --------
    >>> agg = aggregate_weather(ds, win="1W")  # doctest: +SKIP
    >>> agg.snow.dims  # doctest: +SKIP
    ('time', 'latitude', 'longitude')

    """
    daily_snow = ds[snow_var].resample(time="1D").max()
    daily_temp = ds[temp_var].resample(time="1D").mean()

    if snow_agg == "sum":
        snow_roll = daily_snow.resample(time=win).sum()
    else:
        snow_roll = daily_snow.resample(time=win).max()

    out = xr.Dataset(
        {
            "snow": snow_roll,
            "tmin": daily_temp.resample(time=win).min(),
            "tmax": daily_temp.resample(time=win).max(),
        }
    )
    out.snow.attrs["long_name"] = f"days with snow per {win}"
    return out


def bad_period_mask(
    agg: xr.Dataset,
    *,
    snow_threshold: float = 3.0,
    freezing_threshold: float = -2.0,
    temp_var: Literal["tmin", "tmax"] = "tmax",
    combine: Literal["or", "and"] = "or",
) -> xr.DataArray:
    """Return a (time, latitude, longitude) bool mask, True where unusable.

    Parameters
    ----------
    agg : xr.Dataset
        Output of :func:`aggregate_weather`.
    snow_threshold : float
        Snow days per window at or above which the pixel is flagged.
    freezing_threshold : float
        Temperature (C) at or below which the pixel is flagged.
    temp_var : {"tmin", "tmax"}
        Which aggregated temperature to test.
    combine : {"or", "and"}
        Whether a pixel is bad when *either* or *both* conditions hold.

    """
    snowy = agg["snow"] >= snow_threshold
    frozen = agg[temp_var] <= freezing_threshold
    return snowy | frozen if combine == "or" else snowy & frozen


def _polygon_parts(geom: Polygon | MultiPolygon) -> list[Polygon]:
    """Return the polygon parts of ``geom``.

    NISAR frames that cross the dateline are stored as a MultiPolygon already
    split at +/-180, so each part is safely subset with its own bounding box.
    """
    return list(geom.geoms) if isinstance(geom, MultiPolygon) else [geom]


def _subset_mask_to_polygon(mask: xr.DataArray, poly: Polygon) -> xr.DataArray:
    """Clip ``mask`` to the pixel centers falling inside ``poly``."""
    minx, miny, maxx, maxy = poly.bounds
    lat = mask["latitude"].values
    lat_slice = slice(maxy, miny) if lat[0] > lat[-1] else slice(miny, maxy)
    sub = mask.sel(latitude=lat_slice, longitude=slice(minx, maxx))
    if sub.size == 0:
        return sub

    yy, xx = np.meshgrid(sub["latitude"].values, sub["longitude"].values, indexing="ij")
    pts = gpd.GeoSeries(
        gpd.points_from_xy(xx.ravel(), yy.ravel()), index=pd.RangeIndex(xx.size)
    )
    inside = pts.within(poly).values.reshape(xx.shape)
    return sub.where(inside)


def daily_bad_fraction(
    mask: xr.DataArray, geom: Polygon | MultiPolygon
) -> pd.Series | None:
    """Return the fraction of in-frame pixels flagged bad, indexed by time.

    Parts of a dateline-crossing MultiPolygon are pooled by pixel count rather
    than averaged, so the far smaller sliver on one side cannot outvote the
    other.

    Returns
    -------
    pd.Series | None
        ``None`` when the frame has no overlapping weather pixels.

    """
    bad_total: xr.DataArray | None = None
    count_total: xr.DataArray | None = None

    for poly in _polygon_parts(geom):
        sub = _subset_mask_to_polygon(mask, poly)
        if sub.size == 0:
            continue
        dims = ("latitude", "longitude")
        n_bad = sub.sum(dim=dims)
        n_pix = sub.notnull().sum(dim=dims)
        bad_total = n_bad if bad_total is None else bad_total + n_bad
        count_total = n_pix if count_total is None else count_total + n_pix

    if bad_total is None or count_total is None or int(count_total.max()) == 0:
        return None
    return (bad_total / count_total).to_series()


def plot_frame_timeline(
    frame_idx: int,
    mask: xr.DataArray,
    frames_gdf: gpd.GeoDataFrame,
    *,
    mask_fraction: float = 0.5,
    ax: plt.Axes | None = None,
) -> plt.Axes:
    """Plot good/bad periods for one frame as a red/green strip."""
    if ax is None:
        _, ax = plt.subplots(figsize=(9, 1))
    geom = frames_gdf.loc[frames_gdf.frame_idx == frame_idx, "geometry"].iloc[0]
    frac = daily_bad_fraction(mask, geom)
    if frac is None:
        raise ValueError(f"No weather pixels overlap frame_idx {frame_idx}")

    is_bad = frac > mask_fraction
    x = pd.to_datetime(is_bad.index)
    ax.bar(
        x,
        np.ones(len(is_bad)),
        width=1,
        color=["#d62728" if b else "#2ca02c" for b in is_bad.values],
    )
    ax.set_ylim(0, 1)
    ax.set_yticks([])
    ax.set_xlim(x.min(), x.max())
    ax.set_title(f"frame_idx {frame_idx}: bad (red) / good (green) periods")
    ax.figure.tight_layout()
    return ax


class Mode(Enum):
    """How yearly winter runs collapse into a single blackout window."""

    CONSERVATIVE = "conservative"
    AGGRESSIVE = "aggressive"
    MEDIAN = "median"


def summarize_blackouts(
    runs: list[tuple[pd.Timestamp, pd.Timestamp]],
    *,
    mode: Mode | str = Mode.MEDIAN,
    pivot_month: int = PIVOT_MONTH,
) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Collapse winter ``runs`` into one window that respects the year wrap.

    Every date is mapped onto a pivot water year starting at ``pivot_month``,
    so a winter running Nov-2025 to Apr-2026 stays contiguous.

    ``pivot_month`` must match the one :func:`get_annual_seasons` used, or a
    late thaw lands in the wrong pivot year and the window inverts.

    Returns
    -------
    tuple[pd.Timestamp, pd.Timestamp]
        Start and end whose *year* is arbitrary (2000/2001); only the month and
        day are meaningful, and ``start < end`` always holds.

    """
    if not runs:
        raise ValueError("No blackout runs detected")
    mode = Mode(mode) if isinstance(mode, str) else mode

    def _to_pivot(ts: pd.Timestamp) -> pd.Timestamp:
        return ts.replace(year=2000 + (ts.month < pivot_month))

    starts = [_to_pivot(s) for s, _ in runs]
    ends = [_to_pivot(e) for _, e in runs]

    if mode is Mode.CONSERVATIVE:
        return min(starts), max(ends)
    if mode is Mode.AGGRESSIVE:
        return max(starts), min(ends)
    return sorted(starts)[len(starts) // 2], sorted(ends)[len(ends) // 2]


def get_annual_seasons(
    frac_series: pd.Series,
    *,
    mask_fraction: float = 0.5,
    pivot_month: int = PIVOT_MONTH,
    min_total_winter_periods: int = 2,
    min_run_len: int = 1,
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Identify the freeze start and thaw end of each winter in the series.

    Works on a water year (default August-July) so winters spanning New Year
    are not cut in half.

    Parameters
    ----------
    frac_series : pd.Series
        Per-window fraction of bad pixels for a single frame.
    mask_fraction : float
        Fraction of bad pixels above which a window counts as bad.
    pivot_month : int
        Month that starts the water year.
    min_total_winter_periods : int
        Minimum number of bad windows for a water year to count as a winter.
    min_run_len : int
        Minimum length of a consecutive bad run to count as a freeze onset.

    Returns
    -------
    list[tuple[pd.Timestamp, pd.Timestamp]]
        One ``(freeze_start, thaw_end)`` per qualifying water year.

    """
    water_year = frac_series.index.map(
        lambda ts: ts.year if ts.month >= pivot_month else ts.year - 1
    )

    seasons: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    for _year, group in frac_series.groupby(water_year):
        is_bad = group >= mask_fraction
        bad_days = group[is_bad]
        if len(bad_days) < min_total_winter_periods:
            continue

        block_grouper = (is_bad != is_bad.shift()).cumsum()
        bad_blocks = is_bad[is_bad]
        block_sizes = bad_blocks.groupby(block_grouper).size()

        significant = block_sizes[block_sizes >= min_run_len]
        if significant.empty:
            continue

        freeze_start = bad_blocks[block_grouper == significant.index[0]].index[0]
        thaw_end = bad_days.index.max()
        seasons.append((freeze_start, thaw_end))

    return seasons


def get_blackout_windows(
    agg: xr.Dataset,
    frames_gdf: gpd.GeoDataFrame,
    *,
    snow_threshold: float = 3.0,
    freezing_threshold: float = -2.0,
    temp_var: Literal["tmin", "tmax"] = "tmax",
    mask_fraction: float = 0.5,
    pivot_month: int = PIVOT_MONTH,
    frame_idx: int | None = None,
    debug: bool = False,
) -> pd.DataFrame:
    """Derive per-frame blackout windows from aggregated weather.

    Parameters
    ----------
    agg : xr.Dataset
        Output of :func:`aggregate_weather`.
    frames_gdf : gpd.GeoDataFrame
        NISAR frames with ``frame_idx`` and ``geometry``.
    snow_threshold, freezing_threshold, temp_var : float, float, str
        Passed through to :func:`bad_period_mask`.
    mask_fraction : float
        Fraction of in-frame pixels that must be bad for the window to be bad.
    pivot_month : int
        Month starting the water year, used both to split the seasons and to
        collapse them.
    frame_idx : int, optional
        Process only this frame instead of every row of ``frames_gdf``.
    debug : bool
        Report frames skipped for lack of overlapping weather pixels.

    Returns
    -------
    pd.DataFrame
        One row per frame with ``start_*``/``end_*``/``blackout_duration_*``
        columns for the conservative, median and aggressive strategies, plus
        the thresholds used. Frames with no detected winter carry ``NaT``.

    """
    mask = bad_period_mask(
        agg,
        snow_threshold=snow_threshold,
        freezing_threshold=freezing_threshold,
        temp_var=temp_var,
        combine="or",
    )

    if frame_idx is not None:
        frames_gdf = frames_gdf[frames_gdf.frame_idx == frame_idx]

    rows: list[dict] = []
    for row in tqdm(
        frames_gdf.itertuples(), total=len(frames_gdf), desc="Processing frames"
    ):
        frac = daily_bad_fraction(mask, row.geometry)
        if frac is None:
            if debug:
                print(f"No weather pixels for frame_idx {row.frame_idx}; skipping")
            continue

        windows = {
            f"{bound}_{m.value}": pd.NaT for m in Mode for bound in ("start", "end")
        }
        seasons = get_annual_seasons(
            frac, mask_fraction=mask_fraction, pivot_month=pivot_month
        )
        if not seasons and debug:
            print(f"No winter seasons for frame_idx {row.frame_idx}")
        for mode in Mode:
            if seasons:
                start, end = summarize_blackouts(
                    seasons, mode=mode, pivot_month=pivot_month
                )
                windows[f"start_{mode.value}"] = start
                windows[f"end_{mode.value}"] = end

        rows.append(
            {
                "frame_idx": int(row.frame_idx),
                "track": int(row.track),
                "frame": int(row.frame),
                "n_seasons": len(seasons),
                "mask_fraction": mask_fraction,
                "snow_threshold": snow_threshold,
                "freezing_threshold": freezing_threshold,
                "temp_var": temp_var,
                **windows,
                "geometry": row.geometry,
            }
        )

    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs=frames_gdf.crs)
    for mode in Mode:
        gdf[f"blackout_duration_{mode.value}"] = (
            (gdf[f"end_{mode.value}"] - gdf[f"start_{mode.value}"]).abs().dt.days
        )
    return gdf
