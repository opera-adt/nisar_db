#!/usr/bin/env python
"""Generate a self-contained HTML viewer for the NISAR OPERA (North America) scope.

The viewer is a single offline HTML file (MapLibre GL is vendored under
``scripts/vendor/``) that draws every OPERA NISAR-DISP frame and joins in the
GSLC catalog so each frame carries:

* the number of GSLC granules present in CMR (``gslc_count``), split into the
  acquisitions they represent (``n_unique``) and the granules that repeat a
  date and mode (``n_duplicate``),
* the *consistent* observation mode / coverage chosen for DISP time series
  (``cons_mode`` / ``cons_cov``, following the same voting rule as
  :mod:`nisar_db.consistent_gslc`), and
* the full granule list, surfaced in a click-to-open popup.

It is intentionally a design sibling of ``scripts/nisar_frame_viewer_v1.html``
(same look and controls) with these deliberate differences:

* globe (global) projection is the default at start,
* frames can be colored by GSLC acquisition count, duplicate count, or consistent
  mode / coverage,
* hovering a frame shows a dismissable summary and clicking it opens the
  granule list, a per-frame granule CSV export, and a plot of observation mode
  against acquisition date,
* a live "Consistent Mode Summary" panel aggregates the shown frames,
* the solid-earth CalVal site boxes are dropped, and the CalVal frames are the
  ascending and descending ones over the DISP-S1 validation sites and Mexico City
  (``disp_s1_calval_sites.geojson``), not the GeoPackage's flag,
* selected frames can be imported from a CSV, a GeoJSON, or a consistent-GSLC
  catalog JSON (the output of ``nisar-db create-consistent``), and
* an optional blackout-dates JSON adds blackout-duration (months) coloring,
  per-frame hover/click detail of the blacked-out ranges, and gray blackout
  bands in the per-frame plot; an optional reference-dates JSON adds the
  InSAR reference resets to the same hover/click detail, and
* every frame is tagged with the DISP-S1 rollout options it overlaps
  (``disp_s1_rollout_regions.geojson``, or ``--rollout``), which the viewer
  lists, filters and colours by, and
* an optional GUNW catalog adds a GSLC / GUNW switch: in GUNW mode frames are
  colored by interferogram count, the over-time chart and mode / polarization
  chips follow GUNW, and the hover, popup, CSV export and plot describe the
  frame's interferograms instead of its GSLC granules.

Examples
--------
Build the viewer from the notebook artifacts::

    python scripts/generate_scope_viewer.py \\
        --frames-gpkg notebooks/opera-nisar-disp-frames.gpkg \\
        --gslc-db notebooks/gslc_catalog.duckdb \\
        --output scripts/nisar_scope_viewer.html

Drive the consistent-mode fields from a published catalog instead of
recomputing them::

    python scripts/generate_scope_viewer.py \\
        --frames-gpkg notebooks/opera-nisar-disp-frames.gpkg \\
        --gslc-db notebooks/gslc_catalog.duckdb \\
        --consistent-json opera-nisar-disp-consistent-gslc-20260724.json \\
        --output scripts/nisar_scope_viewer.html

Layer in optional blackout / reference dates (both keyed by ``frame_idx``)::

    python scripts/generate_scope_viewer.py \\
        --frames-gpkg notebooks/opera-nisar-disp-frames.gpkg \\
        --gslc-db notebooks/gslc_catalog.duckdb \\
        --blackout-json nisar-blackout-dates-20260724.json \\
        --reference-json opera-disp-nisar-reference-dates.json \\
        --output scripts/nisar_scope_viewer.html

Add the GUNW view from the catalog the daily Action writes::

    python scripts/generate_scope_viewer.py \\
        --frames-gpkg notebooks/opera-nisar-disp-frames.gpkg \\
        --gslc-db notebooks/gslc_catalog.duckdb \\
        --gunw-catalog catalog/gunw_interferograms.json.gz \\
        --output scripts/nisar_scope_viewer.html
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import geopandas as gpd
import pandas as pd

# Same standard science modes used by ``nisar_db.consistent_gslc`` /
# ``nisar_db.modes``; kept local so the generator runs without importing the
# package (the notebooks env may not have it installed). Ordered most preferred
# first -- the order is the tie-break between science modes, so it must match
# ``nisar_db.modes.MODE_PRIORITY``.
MODE_PRIORITY = ("4005", "2005")
STANDARD_MODES = frozenset(MODE_PRIORITY)

# Mirrors ``nisar_db.consistent_gslc.PARTIAL_DOMINANCE_THRESHOLD``: above this
# share of partial acquisitions a frame prefers partial coverage, because the
# partial series is the one carrying the temporal coverage.
PARTIAL_DOMINANCE_THRESHOLD = 0.66

VENDOR_DIR = Path(__file__).resolve().parent / "vendor"

#: The DISP-S1 validation frames, plus the Mexico City basin as a fast-deforming
#: area; the NISAR frames over them are the CalVal frames.
CALVAL_SITES = Path(__file__).resolve().parent / "disp_s1_calval_sites.geojson"

# A frame counts once it covers this share of a site: enough to keep the
# neighbouring tracks a site straddles, not the frames that clip a corner.
CALVAL_MIN_OVERLAP = 0.10

#: The DISP-S1 North America rollout on Sentinel-1 frames, written by
#: ``make_rollout_regions.py``; NISAR frames take the options they overlap.
ROLLOUT_REGIONS = Path(__file__).resolve().parent / "disp_s1_rollout_regions.geojson"

# A NISAR frame joins a rollout option once that option's S1 frames cover this
# share of it: a frame half in a region is processed with it, one grazing the
# edge is not.
ROLLOUT_MIN_OVERLAP = 0.25

# Region names are listed for the S1 frames covering at least this share of the
# NISAR frame, so a sliver of a neighbouring state does not make the list.
ROLLOUT_REGION_MIN_SHARE = 0.05


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_frames(gpkg_path: Path) -> gpd.GeoDataFrame:
    """Load the OPERA frame polygons in EPSG:4326.

    Parameters
    ----------
    gpkg_path : Path
        Path to ``opera-nisar-disp-frames.gpkg``.

    Returns
    -------
    geopandas.GeoDataFrame
        Frames in lon/lat with a single-letter ``direction`` column added.

    """
    gdf = gpd.read_file(gpkg_path)
    if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(4326)
    gdf["direction"] = gdf["passDirection"].str[0]
    return gdf


def flag_calval_frames(
    gdf: gpd.GeoDataFrame,
    sites: gpd.GeoDataFrame,
    min_overlap: float = CALVAL_MIN_OVERLAP,
) -> pd.Series:
    """Flag the frames that cover the CalVal sites, ascending and descending.

    Every frame covering at least ``min_overlap`` of a site is flagged, whatever
    its track or pass direction.

    Parameters
    ----------
    gdf : geopandas.GeoDataFrame
        Frame polygons.
    sites : geopandas.GeoDataFrame
        CalVal site polygons, e.g. the DISP-S1 validation frames.
    min_overlap : float
        Smallest share of a site's area a frame must cover.

    Returns
    -------
    pandas.Series
        Boolean, indexed like ``gdf``.

    """
    # Overlap shares are area ratios, so they need an equal-area projection.
    frames = gdf.to_crs("EPSG:6933")
    flagged = pd.Series(False, index=gdf.index)
    for site in sites.to_crs("EPSG:6933").geometry:
        share = frames.geometry.intersection(site).area / site.area
        flagged |= share >= min_overlap
    return flagged


def rollout_by_frame(
    gdf: gpd.GeoDataFrame,
    source: Path,
    min_overlap: float = ROLLOUT_MIN_OVERLAP,
) -> tuple[list[str], list[list[str]], list[list[str]]]:
    """Tag every frame with the rollout options (and regions) it belongs to.

    Two sources are accepted. A GeoJSON of polygons carrying ``rollout`` (and
    optionally ``region_name``), such as :data:`ROLLOUT_REGIONS`, is matched by
    overlap: a frame joins an option once the option's polygons cover
    ``min_overlap`` of it. A JSON object ``{"<option>": [frame_idx, ...]}``, the
    layout of the OPERA PCM region database, is taken as is.

    Parameters
    ----------
    gdf : geopandas.GeoDataFrame
        Frame polygons with a ``frame_idx`` column.
    source : Path
        Rollout GeoJSON or ``{option: [frame_idx, ...]}`` JSON.
    min_overlap : float
        Smallest share of a frame an option must cover (GeoJSON source only).

    Returns
    -------
    options : list of str
        Every rollout option, in rollout order.
    rollout : list of list of str
        Per frame (aligned with ``gdf``), the options it belongs to.
    regions : list of list of str
        Per frame, the region names it overlaps; empty for a frame-list source.

    """
    payload = json.loads(Path(source).read_text())
    if payload.get("type") != "FeatureCollection":
        options = list(payload)
        members = {opt: {int(i) for i in payload[opt]} for opt in options}
        tagged = [
            [opt for opt in options if int(idx) in members[opt]]
            for idx in gdf["frame_idx"]
        ]
        return options, tagged, [[] for _ in tagged]

    regions_gdf = gpd.read_file(source).to_crs("EPSG:6933")
    frames = gdf.reset_index(drop=True).to_crs("EPSG:6933")
    frame_area = frames.geometry.area
    options = sorted(regions_gdf["rollout"].unique())
    rollout: list[list[str]] = [[] for _ in range(len(frames))]
    for opt in options:
        union = regions_gdf.loc[regions_gdf["rollout"] == opt].geometry.union_all()
        share = frames.geometry.intersection(union).area / frame_area
        for i in share.index[share >= min_overlap]:
            rollout[i].append(opt)

    names: list[list[str]] = [[] for _ in range(len(frames))]
    if "region_name" in regions_gdf:
        pairs = gpd.sjoin(
            frames[["geometry"]], regions_gdf, how="inner", predicate="intersects"
        )
        for i, row in pairs.iterrows():
            if row["rollout"] not in rollout[i]:
                continue
            other = regions_gdf.geometry.iloc[row["index_right"]]
            share = frames.geometry.iloc[i].intersection(other).area / frame_area[i]
            if share >= ROLLOUT_REGION_MIN_SHARE and row["region_name"] not in names[i]:
                names[i].append(row["region_name"])
    return options, rollout, [sorted(n) for n in names]


# Overview polygons are simplified to about a kilometre: they are drawn at
# continent scale, and the frame outlines carry the detail.
ROLLOUT_OVERVIEW_TOLERANCE = 0.01


def rollout_overview(
    gdf: gpd.GeoDataFrame,
    source: Path,
    options: list[str],
    rollout: list[list[str]],
) -> dict:
    """Dissolve the rollout into one outline per option for the overview layer.

    A GeoJSON source is drawn as it is defined, from its own polygons; a frame
    list has no geometry of its own, so its options are drawn as the union of
    their frames.

    Parameters
    ----------
    gdf : geopandas.GeoDataFrame
        Frame polygons, aligned with ``rollout``.
    source : Path
        The rollout source passed to :func:`rollout_by_frame`.
    options : list of str
        Rollout options, in rollout order.
    rollout : list of list of str
        Per frame, the options it belongs to.

    Returns
    -------
    dict
        GeoJSON ``FeatureCollection``, one feature per option carrying
        ``rollout``, ``n_frames`` (frames tagged with it), ``n_source`` (source
        polygons, 0 for a frame list) and ``regions``.

    """
    frames = gdf.reset_index(drop=True)
    payload = json.loads(Path(source).read_text())
    is_geojson = payload.get("type") == "FeatureCollection"
    regions_gdf = gpd.read_file(source) if is_geojson else None
    features = []
    for opt in options:
        if regions_gdf is not None:
            part = regions_gdf.loc[regions_gdf["rollout"] == opt]
            names = (
                sorted(part["region_name"].dropna().unique())
                if "region_name" in part
                else []
            )
            n_source = len(part)
        else:
            part = frames.loc[[opt in r for r in rollout]]
            names, n_source = [], 0
        if part.empty:
            continue
        shape = part.geometry.union_all().simplify(ROLLOUT_OVERVIEW_TOLERANCE)
        features.append(
            {
                "type": "Feature",
                "geometry": shape.__geo_interface__,
                "properties": {
                    "rollout": opt,
                    "n_frames": sum(opt in r for r in rollout),
                    "n_source": n_source,
                    "regions": [str(n) for n in names],
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


#: Per-granule columns the viewer summarizes a frame with.
_CATALOG_COLUMNS = [
    "track",
    "frame",
    "direction",
    "mode",
    "coverage",
    "polarization",
    "cycle",
    "granule_id",
    "start_datetime",
]


def load_gslc_catalog(db_path: Path) -> pd.DataFrame:
    """Read the GSLC product catalog from the DuckDB store.

    Returns one row per granule with the columns needed to summarize a frame.
    """
    # Only the bucket-scan route needs DuckDB; the CMR route reads a CSV.
    import duckdb

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        df = con.execute(
            f"SELECT {', '.join(_CATALOG_COLUMNS)} FROM products"
        ).fetchdf()
    finally:
        con.close()
    return _add_date_column(df)


def load_gslc_catalog_csv(csv_path: Path) -> pd.DataFrame:
    """Read the GSLC catalog CSV written by ``nisar-db create-gslc-csv``.

    The CSV route is what the CMR-sourced pipeline produces: a bucket scan
    (`build-s3-catalog`) yields the DuckDB store, but CMR gives granule names,
    which `create-gslc-csv` parses into the same per-granule fields under
    slightly different names.
    """
    df = pd.read_csv(csv_path, dtype={"mode": str, "cycle": str, "crid": str})
    df = df.rename(
        columns={"pass_direction": "direction", "sensing_time": "start_datetime"}
    )
    missing = [c for c in _CATALOG_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"{csv_path} is missing catalog columns: {missing}")
    return _add_date_column(df[_CATALOG_COLUMNS].copy())


def _add_date_column(df: pd.DataFrame) -> pd.DataFrame:
    """Add the ``date`` column the timeline chart groups on."""
    df["date"] = pd.to_datetime(df["start_datetime"]).dt.strftime("%Y-%m-%d")
    return df


def load_consistent_json(path: Path) -> dict[str, dict]:
    """Load a consistent-GSLC catalog (plain ``.json`` or ``.json.zip``).

    Returns the ``data`` mapping keyed by ``frame_idx`` (as string), matching
    the schema written by :mod:`nisar_db.consistent_gslc`.
    """
    payload = _load_json_payload(path)
    return payload.get("data", payload)


def _load_json_payload(path: Path) -> dict:
    """Read a JSON (or ``.json.zip``) file and return the parsed object."""
    if path.suffix == ".zip" or zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            inner = next(n for n in zf.namelist() if n.endswith(".json"))
            return json.loads(zf.read(inner))
    return json.loads(path.read_text())


def load_period_json(path: Path, *keys: str) -> dict[str, list]:
    """Load a per-frame blackout/reference JSON keyed by ``frame_idx``.

    Handles both the ``nisar_db`` and ``burst_db`` schemas: the per-frame map
    lives under ``blackout_dates`` / ``data`` (whichever is present).

    Parameters
    ----------
    path : Path
        JSON or ``.json.zip`` file.
    *keys : str
        Candidate top-level keys to look under, in priority order.

    Returns
    -------
    dict
        ``{frame_idx (str): value}``.

    """
    payload = _load_json_payload(path)
    for key in keys:
        if key in payload:
            return payload[key]
    return payload


# ---------------------------------------------------------------------------
# Consistent-mode voting (mirrors nisar_db.consistent_gslc._common_mode_coverage)
# ---------------------------------------------------------------------------
def common_mode_coverage(group: pd.DataFrame) -> tuple[str, str]:
    """Return the ``(common_mode, common_coverage)`` for one frame's granules.

    Priority, as in :func:`nisar_db.consistent_gslc._common_mode_coverage`.
    Each mode settles its own coverage by majority (``F`` when ``n_F >= n_P``),
    then the modes compete on:

    1. coverage — full-frame (``F``) beats partial (``P``), reversed when more
       than :data:`PARTIAL_DOMINANCE_THRESHOLD` of the candidates are partial,
    2. the acquisition count of the selected ``(mode, coverage)`` combo, and
    3. mode — :data:`MODE_PRIORITY` (``4005`` then ``2005``) settles modes level
       on coverage and count.

    Only the science modes in :data:`STANDARD_MODES` compete: OPERA processes
    no other mode, so a frame seen only in others (``0505``, ``0005``, ...) gets
    ``("none", "none")``, as the consistent-GSLC catalog leaves it out.
    """
    counts = (
        group.groupby(["mode", "coverage"])
        .size()
        .unstack(fill_value=0)
        .reindex(columns=["F", "P"], fill_value=0)
    )
    candidates = counts[counts.index.isin(STANDARD_MODES)]
    if candidates.empty:
        return "none", "none"

    partial_share = candidates["P"].sum() / candidates.to_numpy().sum()
    preferred_coverage = "P" if partial_share > PARTIAL_DOMINANCE_THRESHOLD else "F"

    ranked = pd.DataFrame(
        {
            "mode": candidates.index,
            "coverage": (
                candidates["F"].ge(candidates["P"]).map({True: "F", False: "P"})
            ),
            "n_selected": candidates[["F", "P"]].max(axis=1),
        }
    )
    ranked["coverage_rank"] = (ranked["coverage"] != preferred_coverage).astype(int)
    ranked["mode_rank"] = [
        MODE_PRIORITY.index(m) if m in MODE_PRIORITY else len(MODE_PRIORITY)
        for m in ranked["mode"]
    ]
    winner = ranked.sort_values(
        ["coverage_rank", "n_selected", "mode_rank"],
        ascending=[True, False, True],
        kind="stable",
    ).iloc[0]
    return str(winner["mode"]), str(winner["coverage"])


def summarize_frame(group: pd.DataFrame) -> dict:
    """Compute per-frame catalog stats and the consistent (mode, coverage)."""
    cons_mode, cons_cov = common_mode_coverage(group)
    granules = (
        group.sort_values("start_datetime")[
            ["granule_id", "date", "mode", "coverage", "polarization", "cycle"]
        ]
        .rename(columns={"granule_id": "gid", "coverage": "cov", "polarization": "pol"})
        .to_dict("records")
    )
    # The catalog reads the cycle as text ("024") to keep its zero padding; the
    # viewer compares it as a number.
    for g in granules:
        if str(g["cycle"]).isdigit():
            g["cycle"] = int(g["cycle"])
    # A pass split into several granules of the same mode -- partial segments of
    # one acquisition -- lands on a single point of the timeline chart, so the
    # granule count overstates how many acquisitions a frame really has.
    n_unique = int(len(group.drop_duplicates(subset=["date", "mode", "coverage"])))
    return {
        "gslc_count": int(len(group)),
        "n_unique": n_unique,
        "n_duplicate": int(len(group)) - n_unique,
        "n_modes": int(group["mode"].nunique()),
        "n_full": int((group["coverage"] == "F").sum()),
        "n_partial": int((group["coverage"] == "P").sum()),
        "cons_mode": cons_mode,
        "cons_cov": cons_cov,
        "gslc_modes": sorted(group["mode"].dropna().unique().tolist()),
        "gslc_pols": sorted(group["polarization"].dropna().unique().tolist()),
        "granules": granules,
    }


# ---------------------------------------------------------------------------
# Blackout windows
# ---------------------------------------------------------------------------
_MONTHS = [
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
]


def blackout_summary(windows: list) -> dict:
    """Summarize a frame's blackout windows into a recurring month range.

    The per-frame windows repeat yearly (e.g. ``Nov 01 -> May 31`` every year),
    so the first window defines the recurring pattern: its start/end months and
    the duration in months.

    Parameters
    ----------
    windows : list
        ``[[start_iso, end_iso], ...]`` as stored in the blackout JSON.

    Returns
    -------
    dict
        ``months`` (float duration), ``label`` (e.g. ``"Nov-May"``),
        ``start_month`` / ``end_month`` (1-12), ``n_windows``, and ``ranges``
        (each window as ``"YYYY-MM-DD -> YYYY-MM-DD"`` for the popup).
    """
    if not windows:
        return {
            "months": 0.0,
            "label": "",
            "start_month": 0,
            "end_month": 0,
            "n_windows": 0,
            "ranges": [],
        }
    start = datetime.fromisoformat(str(windows[0][0]))
    end = datetime.fromisoformat(str(windows[0][1]))
    months = round(((end - start).days + 1) / 30.44, 1)
    return {
        "months": months,
        "label": f"{_MONTHS[start.month - 1]}-{_MONTHS[end.month - 1]}",
        "start_month": start.month,
        "end_month": end.month,
        "n_windows": len(windows),
        "ranges": [f"{str(a)[:10]} -> {str(b)[:10]}" for a, b in windows],
    }


#: Per-interferogram columns the viewer summarizes a frame's GUNWs with.
_GUNW_COLUMNS = [
    "track",
    "frame",
    "direction",
    "ref",
    "sec",
    "mode",
    "coverage",
    "polarization",
    "granule_id",
]


def load_gunw_catalog(path: Path) -> pd.DataFrame:
    """Read the GUNW catalog written by ``create_gunw_catalog``.

    Accepts ``gunw_interferograms.json`` or its gzipped ``.json.gz`` and returns
    one row per interferogram granule, i.e. per pair and polarization. Mode and
    coverage come from the granule name, whose fields match
    ``nisar_db.filenames.GUNWFilename`` (mode at 9, coverage at 17).
    """
    opener = gzip.open if Path(path).suffix == ".gz" else open
    with opener(path, "rt") as f:
        ifgs = json.load(f)["interferograms"]
    rows = []
    for ifg in ifgs:
        for pol, granule in (ifg.get("polarizations") or {}).items():
            parts = str(granule["id"]).split("_")
            rows.append(
                {
                    "track": int(ifg["track"]),
                    "frame": int(ifg["frame"]),
                    "direction": ifg["pass_direction"],
                    "ref": _iso_date(ifg["ref_date"]),
                    "sec": _iso_date(ifg["sec_date"]),
                    "mode": parts[9] if len(parts) > 9 else "",
                    "coverage": parts[17] if len(parts) > 17 else "",
                    "polarization": pol,
                    "granule_id": granule["id"],
                }
            )
    return pd.DataFrame(rows, columns=_GUNW_COLUMNS)


def _iso_date(yyyymmdd: str) -> str:
    """``20260626`` -> ``2026-06-26``."""
    s = str(yyyymmdd)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def summarize_gunw(group: pd.DataFrame) -> dict:
    """Summarize one frame's GUNW granules for the viewer."""
    group = group.sort_values(["ref", "sec", "polarization"])
    days = (pd.to_datetime(group["sec"]) - pd.to_datetime(group["ref"])).dt.days
    ifgs = [
        {
            "ref": r.ref,
            "sec": r.sec,
            "dt": int(dt),
            "mode": r.mode,
            "cov": r.coverage,
            "pol": r.polarization,
            "gid": r.granule_id,
        }
        for r, dt in zip(group.itertuples(index=False), days)
    ]
    return {
        "gunw_count": int(len(group)),
        # One pair delivered in several polarizations is one interferogram pair.
        "gunw_pairs": int(len(group.drop_duplicates(subset=["ref", "sec"]))),
        "gunw_modes": sorted(group["mode"].dropna().unique().tolist()),
        "gunw_pols": sorted(group["polarization"].dropna().unique().tolist()),
        "gunw_dt_min": int(days.min()),
        "gunw_dt_max": int(days.max()),
        "gunw_ifgs": ifgs,
    }


# ---------------------------------------------------------------------------
# Feature building
# ---------------------------------------------------------------------------
def build_frame_data(
    gdf: gpd.GeoDataFrame,
    catalog: pd.DataFrame,
    consistent: dict[str, dict] | None,
    blackout: dict[str, list] | None = None,
    reference: dict[str, list] | None = None,
    gunw: pd.DataFrame | None = None,
) -> dict:
    """Assemble the frames ``FeatureCollection`` embedded in the viewer."""
    stats = {
        key: summarize_frame(grp)
        for key, grp in catalog.groupby(["track", "frame", "direction"])
    }
    gunw_stats = (
        {
            key: summarize_gunw(grp)
            for key, grp in gunw.groupby(["track", "frame", "direction"])
        }
        if gunw is not None
        else {}
    )

    features = []
    for _, row in gdf.iterrows():
        key = (int(row["track"]), int(row["frame"]), row["direction"])
        s = stats.get(key)
        frame_idx = int(row["frame_idx"])

        props = {
            "id": f"{int(row['track'])}_{int(row['frame'])}",
            "frame_idx": frame_idx,
            "track": int(row["track"]),
            "frame": int(row["frame"]),
            "passDirection": row["passDirection"],
            # ``main`` overwrites the GeoPackage's isCalVal with the frames over
            # the DISP-S1 validation sites; isSNWG / isDNC are the GeoPackage's.
            "isCalVal": bool(row["isCalVal"]),
            "isSNWG": bool(row["isSNWG"]),
            "isDNC": bool(row["isDNC"]),
            "gslc_count": s["gslc_count"] if s else 0,
            "n_unique": s["n_unique"] if s else 0,
            "n_duplicate": s["n_duplicate"] if s else 0,
            "n_modes": s["n_modes"] if s else 0,
            "n_full": s["n_full"] if s else 0,
            "n_partial": s["n_partial"] if s else 0,
            "cons_mode": s["cons_mode"] if s else "none",
            "cons_cov": s["cons_cov"] if s else "none",
            "gslc_modes": s["gslc_modes"] if s else [],
            "gslc_pols": s["gslc_pols"] if s else [],
            "granules": s["granules"] if s else [],
        }

        # Rollout options, attached by ``main`` from ``rollout_by_frame``.
        if "rollout" in row.index:
            props["rollout"] = list(row["rollout"])
            props["rollout_regions"] = list(row["rollout_regions"])

        # A published consistent-GSLC catalog wins over the recomputed choice.
        if consistent is not None:
            entry = consistent.get(str(frame_idx))
            if entry is not None:
                props["cons_mode"] = entry.get("common_mode", props["cons_mode"])
                props["cons_cov"] = entry.get("common_coverage", props["cons_cov"])
                props["n_consistent"] = len(entry.get("sensing_time_list", []))
                props["in_consistent"] = True
            else:
                # The catalog is the processing list: a frame it leaves out has
                # no consistent mode, whatever the catalog view would pick.
                props["cons_mode"] = "none"
                props["cons_cov"] = "none"
                props["n_consistent"] = 0
                props["in_consistent"] = False

        # Optional per-frame blackout windows (recurring seasonal snow, etc.).
        if blackout is not None:
            bo = blackout_summary(blackout.get(str(frame_idx), []))
            props["has_blackout"] = bo["n_windows"] > 0
            props["blackout_months"] = bo["months"]
            props["blackout_label"] = bo["label"]
            props["blackout_start_month"] = bo["start_month"]
            props["blackout_end_month"] = bo["end_month"]
            props["blackout_windows"] = bo["n_windows"]
            props["blackout_ranges"] = bo["ranges"]

        # Optional per-frame InSAR reference-date resets.
        if reference is not None:
            refs = [str(d)[:10] for d in reference.get(str(frame_idx), [])]
            props["has_reference"] = len(refs) > 0
            props["reference_dates"] = refs

        # Optional GUNW interferograms, shown when the viewer is switched to GUNW.
        if gunw is not None:
            g = gunw_stats.get(key)
            props["gunw_count"] = g["gunw_count"] if g else 0
            props["gunw_pairs"] = g["gunw_pairs"] if g else 0
            props["gunw_modes"] = g["gunw_modes"] if g else []
            props["gunw_pols"] = g["gunw_pols"] if g else []
            props["gunw_dt_min"] = g["gunw_dt_min"] if g else 0
            props["gunw_dt_max"] = g["gunw_dt_max"] if g else 0
            props["gunw_ifgs"] = g["gunw_ifgs"] if g else []

        features.append(
            {
                "type": "Feature",
                "geometry": row["geometry"].__geo_interface__,
                "properties": props,
            }
        )

    return {"type": "FeatureCollection", "features": features}


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------
#: Nevada Geodetic Laboratory station map; the page embeds the site table itself.
NGL_STATION_MAP = "https://geodesy.unr.edu/NGLStationPages/gpsnetmap/GPSNetMap.html"

#: Sites outside North America are dropped: the whole network is ~23k points.
NA_BBOX = (-170.0, 14.0, -52.0, 75.0)

#: ``["SITE", lat, lon, "REFERENCE_FRAME", n]`` rows of the page's stalatlon array.
_STATION_ROW = re.compile(
    r'\["(?P<id>[A-Z0-9_]+)",\s*(?P<lat>-?\d+\.\d+),\s*(?P<lon>-?\d+\.\d+),\s*"(?P<frame>[^"]+)"'
)


def parse_gps_sites(
    text: str, bbox: tuple[float, float, float, float] = NA_BBOX
) -> dict:
    """Turn the NGL station map page into a GeoJSON ``FeatureCollection``.

    Parameters
    ----------
    text : str
        Contents of :data:`NGL_STATION_MAP` (or a local copy of it).
    bbox : tuple of float
        ``(west, south, east, north)`` filter, defaulting to North America.

    Returns
    -------
    dict
        Point features carrying ``id`` and ``frame`` (the reference frame, which
        is also the directory its time-series plot lives in).

    Raises
    ------
    ValueError
        If the page holds no station rows, i.e. its format changed.

    """
    west, south, east, north = bbox
    features = []
    for match in _STATION_ROW.finditer(text):
        lat = float(match["lat"])
        # The page carries longitudes shifted below -180; fold them back.
        lon = float(match["lon"])
        lon = (lon + 180.0) % 360.0 - 180.0
        if not (west <= lon <= east and south <= lat <= north):
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [round(lon, 4), round(lat, 4)],
                },
                "properties": {"id": match["id"], "frame": match["frame"]},
            }
        )
    if not features:
        raise ValueError("No GPS stations parsed; the NGL page layout has changed.")
    return {"type": "FeatureCollection", "features": features}


def load_granule_flags(path: Path) -> dict[str, dict]:
    """Read a per-granule cache written by ``collect_granule_flags.py`` or ``_qa.py``.

    Parameters
    ----------
    path : Path
        Gzipped JSON mapping granule id to its flags (or QA metrics).

    Returns
    -------
    dict
        Granule id to flags (``j f o r m d``) or QA metrics.

    """
    with gzip.open(path, "rt") as fh:
        return json.load(fh)


def attach_granule_flags(frame_data: dict, flags: dict[str, dict]) -> int:
    """Attach cached flags to every GSLC granule and GUNW interferogram.

    Each entry found in ``flags`` gains an ``fl`` field; entries not yet
    collected are left without one, and the viewer shows them as not collected.

    Returns
    -------
    int
        Number of granules and interferograms that received flags.

    """
    return _attach_per_granule(frame_data, flags, "fl")


def attach_granule_qa(frame_data: dict, qa: dict[str, dict]) -> int:
    """Attach cached QA metrics to every GSLC granule and GUNW interferogram.

    Each entry with metrics in ``qa`` (from ``collect_granule_qa.py``) gains a
    ``qa`` field. Entries not yet read, or withdrawn from the archive (cached
    empty), are left without one.

    Returns
    -------
    int
        Number of granules and interferograms that received metrics.

    """
    return _attach_per_granule(frame_data, {k: v for k, v in qa.items() if v}, "qa")


def _attach_per_granule(frame_data: dict, values: dict[str, dict], field: str) -> int:
    attached = 0
    for feature in frame_data["features"]:
        props = feature["properties"]
        ifgs = props.get("gunw_ifgs") or []
        # A viewer built by an older generator stores the interferograms as a string.
        if isinstance(ifgs, str):
            ifgs = json.loads(ifgs)
            props["gunw_ifgs"] = ifgs
        for entry in [*(props.get("granules") or []), *ifgs]:
            value = values.get(entry.get("gid"))
            if value is not None:
                entry[field] = value
                attached += 1
    return attached


def load_gps_sites(source: str | Path | None) -> dict:
    """Fetch (or read) the UNR GPS sites, or an empty collection when disabled.

    Parameters
    ----------
    source : str or Path or None
        A URL to the NGL station map, a local copy of that page, or a GeoJSON
        file. ``None`` builds the viewer without the GPS layer.

    Returns
    -------
    dict
        A GeoJSON ``FeatureCollection``.

    """
    empty: dict = {"type": "FeatureCollection", "features": []}
    if source is None:
        return empty
    if str(source).startswith(("http://", "https://")):
        try:
            with urlopen(str(source), timeout=120) as response:  # noqa: S310
                text = response.read().decode("utf8", errors="replace")
        except (URLError, TimeoutError) as exc:
            # The layer is a convenience: an unreachable NGL should not take the
            # whole viewer build down with it.
            print(f"  GPS sites unavailable ({exc}); building without the layer")
            return empty
    else:
        text = Path(source).read_text()
    if text.lstrip().startswith("{"):
        return json.loads(text)
    return parse_gps_sites(text)


def render_html(
    frame_data: dict,
    meta: dict,
    gps_sites: dict | None = None,
    rollout_regions: dict | None = None,
) -> str:
    """Render the full self-contained HTML document as a string."""
    maplibre_css = (VENDOR_DIR / "maplibre-gl.css").read_text()
    maplibre_js = (VENDOR_DIR / "maplibre-gl.js").read_text()

    data_js = (
        "const FRAME_DATA = "
        + json.dumps(frame_data, separators=(",", ":"))
        + ";\nconst META = "
        + json.dumps(meta, separators=(",", ":"))
        + ";\nconst UNR_GPS_DATA = "
        + json.dumps(
            gps_sites or {"type": "FeatureCollection", "features": []},
            separators=(",", ":"),
        )
        + ";\nconst ROLLOUT_DATA = "
        + json.dumps(
            rollout_regions or {"type": "FeatureCollection", "features": []},
            separators=(",", ":"),
        )
        + ";"
    )

    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '<meta charset="UTF-8">\n'
        f"<title>{meta['title']}</title>\n"
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f"<style>{maplibre_css}</style>\n"
        f"<style>{APP_CSS}</style>\n"
        "</head>\n"
        f"{BODY_HTML}\n"
        f"<script>{maplibre_js}</script>\n"
        f"<script>{data_js}</script>\n"
        f"<script>{APP_JS}</script>\n"
        "</body>\n</html>\n"
    )


APP_CSS = r"""
  /* OPERA palette: brand darks and blue/green accents for the chrome. Frame
     colours are data, not chrome, and keep their own palettes further down. */
  :root{
    --bg:#000000; --panel:#303030; --panel2:#262626; --inset:#1f1f1f; --border:#4a4a4a;
    --text:#f5f5f5; --text-dim:#9db4c6; --accent:#76aedf; --accent2:#aad3c1;
    --hairline:rgba(245,245,245,.12); --scrim:rgba(0,0,0,.8);
  }
  body.theme-light{
    --bg:#f5f5f5; --panel:#ffffff; --panel2:#f5f5f5; --inset:#ffffff; --border:#d8d8d8;
    --text:#303030; --text-dim:#467b7b; --accent:#467b7b; --accent2:#6cbab8;
    --hairline:rgba(48,48,48,.14); --scrim:rgba(255,255,255,.88);
  }
  *{box-sizing:border-box;}
  html,body{margin:0;height:100%;font-family:Metropolis,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--text);}
  #app{display:flex;height:100vh;width:100vw;overflow:hidden;}
  #sidebar{width:340px;min-width:340px;background:var(--panel);border-right:1px solid var(--border);
    display:flex;flex-direction:column;height:100%;overflow:hidden;}
  #sidebar-scroll{overflow-y:auto;flex:1;padding:12px 14px 8px 14px;}
  #map{flex:1;position:relative;}
  h1{position:relative;font-size:15px;margin:0;padding:14px 14px 10px 14px;border-bottom:1px solid var(--border);font-weight:600;}
  h1 small{display:block;font-weight:400;color:var(--text-dim);font-size:11px;margin-top:2px;}
  #hdr-queried{font-size:10.5px;}
  .section{margin-bottom:14px;border:1px solid var(--border);border-radius:8px;background:var(--panel2);}
  .section-head{padding:8px 10px;font-size:12px;font-weight:600;letter-spacing:.3px;color:var(--text-dim);
    text-transform:uppercase;cursor:pointer;display:flex;justify-content:space-between;align-items:center;user-select:none;}
  .section-body{padding:0 10px 10px 10px;font-size:12.5px;}
  .section.collapsed .section-body{display:none;}
  .chev{transition:transform .15s;font-size:10px;}
  .section.collapsed .chev{transform:rotate(-90deg);}
  label{display:block;margin:6px 0 3px 0;color:var(--text-dim);font-size:11px;}
  input[type=text], select{
    width:100%;background:var(--inset);border:1px solid var(--border);color:var(--text);
    border-radius:5px;padding:5px 7px;font-size:12.5px;
  }
  .row{display:flex;gap:6px;}
  .row > *{flex:1;}
  .radio-group{display:flex;gap:10px;margin-top:4px;flex-wrap:wrap;}
  .radio-group label{display:flex;align-items:center;gap:4px;color:var(--text);margin:0;font-size:12px;}
  .chip-grid{display:flex;flex-wrap:wrap;gap:5px;margin-top:5px;max-height:130px;overflow-y:auto;padding:2px;}
  .chip{border:1px solid var(--border);border-radius:4px;padding:3px 7px;font-size:11px;cursor:pointer;
    background:var(--inset);color:var(--text-dim);user-select:none;}
  .chip.active{background:var(--accent);border-color:var(--accent);color:#1a1a1a;}
  .check-row{display:flex;align-items:center;gap:6px;margin:5px 0;font-size:12px;}
  .check-row input{width:auto;}
  .stat-line{color:var(--text-dim);font-size:11px;margin-top:6px;}
  .btn{background:var(--inset);border:1px solid var(--border);color:var(--text);border-radius:5px;
    padding:6px 10px;font-size:12px;cursor:pointer;}
  .btn:hover{border-color:var(--accent);color:var(--accent);}
  .btn.primary{background:var(--accent);border-color:var(--accent);color:#1a1a1a;font-weight:600;}
  .btn.primary:hover{filter:brightness(1.08);color:#1a1a1a;}
  .btn.small{padding:3px 7px;font-size:11px;}
  .btn.danger:hover{border-color:#ff5d5d;color:#ff5d5d;}
  #palette{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px;align-items:center;}
  .swatch{width:20px;height:20px;border-radius:4px;cursor:pointer;border:2px solid transparent;}
  .swatch.selected{border-color:#f5f5f5;}
  #custom-color{width:28px;height:22px;padding:0;border:1px solid var(--border);border-radius:4px;background:none;cursor:pointer;}
  #selected-list{list-style:none;margin:0;padding:0;max-height:260px;overflow-y:auto;}
  #selected-list li{display:flex;align-items:center;gap:6px;padding:5px 4px;border-bottom:1px solid var(--border);font-size:11.5px;}
  #selected-list li:hover{background:var(--inset);}
  .li-swatch{width:12px;height:12px;border-radius:3px;flex-shrink:0;cursor:pointer;}
  .li-label{flex:1;cursor:pointer;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .li-label .sub{color:var(--text-dim);}
  .li-x{background:none;border:none;color:var(--text-dim);cursor:pointer;font-size:13px;padding:0 3px;}
  .li-x:hover{color:#ff5d5d;}
  .footer-actions{padding:10px 14px;border-top:1px solid var(--border);display:flex;gap:8px;flex-wrap:wrap;}
  #product-ctrl{position:absolute;top:10px;left:10px;background:var(--scrim);border:1px solid var(--border);
    border-radius:6px;padding:3px;z-index:5;font-size:11.5px;display:flex;gap:2px;}
  #product-ctrl[hidden]{display:none;}
  #product-ctrl button{background:none;border:none;color:var(--text);padding:3px 10px;border-radius:4px;cursor:pointer;font:inherit;}
  #product-ctrl button.active{background:var(--accent);color:var(--bg);font-weight:600;}
  #map.has-product #pass-ctrl{top:48px;}
  #map.has-product #search{top:86px;}
  #pass-ctrl{position:absolute;top:10px;left:10px;background:var(--scrim);border:1px solid var(--border);
    border-radius:6px;padding:6px 8px;z-index:5;font-size:11.5px;display:flex;gap:8px;}
  #pass-ctrl label{display:flex;align-items:center;gap:4px;color:var(--text);margin:0;cursor:pointer;}
  #click-ctrl{position:absolute;top:48px;right:10px;background:var(--scrim);border:1px solid var(--border);
    border-radius:6px;padding:6px 8px;z-index:5;font-size:11.5px;}
  #click-ctrl label{display:flex;align-items:center;gap:4px;color:var(--text);margin:0;cursor:pointer;}
  #search{position:absolute;top:48px;left:10px;z-index:6;width:268px;}
  #search input{width:100%;box-sizing:border-box;background:var(--scrim);color:var(--text);border:1px solid var(--border);
    border-radius:6px;padding:6px 9px;font:inherit;font-size:11.5px;}
  #search input:focus{outline:2px solid var(--accent);outline-offset:-1px;}
  #search-results{margin-top:4px;background:var(--panel);border:1px solid var(--border);border-radius:6px;
    max-height:260px;overflow-y:auto;box-shadow:0 4px 16px rgb(0 0 0 / .25);}
  #search-results[hidden]{display:none;}
  #search-results button{display:block;width:100%;text-align:left;background:none;border:none;
    border-bottom:1px solid var(--hairline);color:var(--text);padding:6px 9px;cursor:pointer;font:inherit;font-size:11.5px;line-height:1.35;}
  #search-results button:hover{background:var(--panel2);}
  #search-results button small{display:block;color:var(--text-dim);font-size:10.5px;}
  #search-results .msg{padding:6px 9px;color:var(--text-dim);font-size:11px;}
  #top-hint{position:absolute;bottom:24px;left:10px;background:var(--scrim);color:var(--text-dim);
    font-size:11.5px;padding:6px 10px;border-radius:6px;border:1px solid var(--border);pointer-events:none;z-index:5;}
  #basemap-ctrl{position:absolute;top:10px;right:10px;background:var(--scrim);border:1px solid var(--border);
    border-radius:6px;padding:6px 8px;z-index:5;font-size:11.5px;display:flex;gap:8px;}
  #basemap-ctrl label{display:flex;align-items:center;gap:4px;color:var(--text);margin:0;cursor:pointer;}
  .maplibregl-ctrl-group button.hover-info-btn{display:flex;align-items:center;justify-content:center;color:#303030;}
  .maplibregl-ctrl-group button.hover-info-btn.active{background:#b2daf7;color:#14425e;}
  .maplibregl-popup-content{background:var(--panel);color:var(--text);font-size:12px;border-radius:6px;padding:8px 22px 8px 10px;}
  .maplibregl-popup-close-button{color:var(--text-dim);font-size:15px;line-height:1;padding:2px 6px;background:none;border:none;}
  .maplibregl-popup-close-button:hover{background:none;color:#ff5d5d;}
  .pop-actions{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px;}
  .btn[disabled]{opacity:.45;cursor:default;}
  .btn[disabled]:hover{border-color:var(--border);color:var(--text);}
  .maplibregl-popup-tip{border-top-color:var(--panel) !important;border-bottom-color:var(--panel) !important;}
  .maplibregl-ctrl-attrib{font-size:10px;}
  .pop-title{font-weight:600;margin-bottom:3px;}
  .pop-row{color:var(--text-dim);}
  .granule-list{max-height:220px;overflow-y:auto;margin-top:6px;border-top:1px solid var(--border);padding-top:4px;}
  /* The frame popup can be dragged larger from its corner, or blown up with ⤢;
     its lists then use the room instead of keeping their own scroll height. */
  .frame-pop .maplibregl-popup-content{resize:both;overflow:auto;width:340px;min-width:240px;min-height:110px;
    max-width:92vw;max-height:80vh;}
  .frame-pop .maplibregl-popup-content{padding-right:44px;}
  .frame-pop.pop-big .maplibregl-popup-content{width:min(680px,90vw);}
  .frame-pop.pop-big .granule-list,.frame-pop.pop-resized .granule-list{max-height:none;}
  .pop-expand{position:absolute;top:1px;right:22px;background:none;border:none;color:var(--text-dim);
    font-size:14px;line-height:1;padding:3px 5px;cursor:pointer;}
  .pop-expand:hover{color:var(--accent);}
  .granule-row{font-size:10.5px;color:var(--text-dim);padding:2px 0;border-bottom:1px solid var(--hairline);font-family:ui-monospace,Menlo,Consolas,monospace;}
  .granule-row .gdate{color:var(--text);}
  .granule-row .gmode{color:var(--accent2);}
  .granule-row.dup-gid{padding-left:12px;}
  .day-bar{fill:var(--accent);}
  .day-bar:hover{fill:#b2daf7;}
  .day-base{stroke:var(--border);stroke-width:1;}
  .day-axis{fill:var(--text-dim);font-size:9.5px;}
  .date-row{display:flex;gap:6px;align-items:center;margin-top:6px;}
  .date-row input[type=date]{flex:1;background:var(--inset);border:1px solid var(--border);color:var(--text);
    border-radius:5px;padding:3px 5px;font-size:11px;color-scheme:dark;}
  body.theme-light .date-row input[type=date]{color-scheme:light;}
  .legend-color{width:28px;height:20px;padding:0;border:1px solid var(--border);border-radius:4px;background:none;cursor:pointer;}
  .cmap-bar-click{cursor:pointer;}
  .cmap-bar-click:hover .cmap-ramp{outline:1px solid var(--accent);}
  .cmap-ramp{height:12px;border-radius:4px;}
  .cmap-labels{display:flex;justify-content:space-between;font-size:10px;color:var(--text-dim);margin-top:2px;}
  .cmap-pop{margin-top:6px;padding:6px;border:1px solid var(--border);border-radius:6px;background:var(--inset);}
  .cmap-pop[hidden]{display:none;}
  .cmap-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:3px;padding:2px 0 4px;}
  .cmap-group{border-bottom:1px solid var(--hairline);}
  .cmap-group:last-child{border-bottom:none;}
  .cmap-group summary{cursor:pointer;font-size:10px;letter-spacing:.4px;text-transform:uppercase;color:var(--text-dim);
    padding:4px 0;user-select:none;}
  .cmap-group summary:hover{color:var(--accent);}
  .cmap-group .cmap-count{opacity:.7;}
  .cmap-group .cmap-cur{text-transform:none;letter-spacing:0;color:var(--text);}
  .cmap-grid button{display:flex;flex-direction:column;gap:2px;padding:3px 5px;font-size:10px;text-align:left;
    border-radius:6px;overflow:hidden;background:var(--panel2);border:1px solid var(--border);color:var(--text);cursor:pointer;}
  .cmap-grid button.active{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent);}
  .cmap-grid .cmap-swatch{display:block;height:7px;border-radius:3px;}
  .cmap-range{display:flex;gap:4px;align-items:center;margin-top:6px;font-size:11px;color:var(--text-dim);}
  .cmap-range input[type=number]{width:64px;background:var(--panel2);border:1px solid var(--border);color:var(--text);
    border-radius:4px;padding:2px 4px;font-size:11px;}
  .cmap-range label{display:flex;align-items:center;gap:3px;margin:0;color:var(--text);font-size:11px;}
  .cmap-range input[type=checkbox]{width:auto;margin:0;}
  .day-brush{fill:var(--accent);fill-opacity:.18;stroke:var(--accent);stroke-width:1;pointer-events:none;}
  #daily-chart svg{cursor:crosshair;}
  .bo-bar{fill:var(--accent);cursor:pointer;}
  .bo-bar-any{fill:var(--accent);fill-opacity:.35;cursor:pointer;}
  .bo-bar.sel,.bo-bar-any.sel{stroke:var(--text);stroke-width:1;}
  .month-strip{display:grid;grid-template-columns:repeat(12,1fr);gap:2px;margin-top:4px;}
  .month-strip button{display:flex;flex-direction:column;align-items:center;font:inherit;font-size:9px;line-height:1.15;
    padding:2px 0;border-radius:3px;color:var(--text);cursor:pointer;background:var(--inset);border:1px solid var(--hairline);}
  .month-strip button small{font-size:8.5px;color:var(--text-dim);}
  .month-strip button.none{opacity:.5;}
  .month-strip button:hover{border-color:var(--accent);}
  .month-strip button.active{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent);}
  .month-list[hidden]{display:none;}
  .month-strip span{font-size:9px;text-align:center;padding:2px 0;border-radius:2px;color:var(--text);
    background:var(--inset);border:1px solid var(--hairline);}
  .rollout-row{display:flex;align-items:center;gap:6px;margin:3px 0;font-size:11px;cursor:pointer;user-select:none;}
  .rollout-row .rl{width:42px;color:var(--text);flex-shrink:0;font-weight:600;}
  .rollout-row .bn{width:62px;text-align:right;color:var(--text);flex-shrink:0;}
  .rollout-row.off{opacity:.4;}
  .rollout-row:hover .rl{color:var(--accent);}
  .cat-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:3px 8px;}
  .cat-row{display:flex;align-items:center;gap:6px;margin:0;font-size:11px;color:var(--text-dim);cursor:pointer;}
  .cat-row span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .cb-tabs{display:flex;gap:2px;background:var(--inset);border:1px solid var(--border);border-radius:6px;padding:2px;}
  .cb-tabs button{background:none;border:none;color:var(--text-dim);font:inherit;font-size:11.5px;padding:3px 10px;border-radius:4px;cursor:pointer;}
  .cb-tabs button.active{background:var(--accent);color:#1a1a1a;font-weight:600;}
  .cb-group{font-size:10px;letter-spacing:.4px;text-transform:uppercase;color:var(--text-dim);margin:8px 0 2px;}
  .cb-opt{display:flex;align-items:center;gap:8px;width:100%;background:none;border:1px solid transparent;border-radius:6px;
    color:var(--text);font:inherit;font-size:11.5px;text-align:left;padding:3px 6px;cursor:pointer;}
  .cb-opt:hover{background:var(--panel2);}
  .cb-opt.active{border-color:var(--accent);background:var(--panel2);}
  .cb-opt .cb-name{flex:1;}
  .cb-prev{display:inline-flex;width:54px;height:9px;border-radius:3px;overflow:hidden;flex-shrink:0;}
  .cb-prev-cat i{flex:1;}
  details.cb-more{border-bottom:1px solid var(--hairline);}
  details.cb-more:last-child{border-bottom:none;}
  details.cb-more summary{cursor:pointer;font-size:10px;letter-spacing:.4px;text-transform:uppercase;color:var(--text-dim);
    padding:5px 0;user-select:none;}
  details.cb-more summary:hover{color:var(--accent);}
  details.cb-more[open] summary{padding-bottom:2px;}
  #colorby-panel{width:280px;max-height:min(62vh,500px);}
  #colorby-panel .cmap-grid{grid-template-columns:repeat(2,minmax(0,1fr));}
  .cb-slider{display:flex;align-items:center;gap:8px;margin:6px 0 2px;font-size:11.5px;color:var(--text-dim);}
  .cb-slider input[type=range]{flex:1;}
  .cb-slider b{width:38px;text-align:right;color:var(--text);font-weight:600;}
  .cb-presets{display:flex;gap:4px;margin-bottom:6px;}
  .cb-presets .chip{flex:1;text-align:center;}
  .cb-note{margin:6px 0;padding:6px 8px;border:1px solid var(--border);border-radius:6px;background:var(--inset);font-size:11px;color:var(--text-dim);}
  .cb-note[hidden]{display:none;}
  .cb-legend{margin-top:8px;padding-top:6px;border-top:1px solid var(--hairline);}
  .mini-cats{display:flex;flex-wrap:wrap;gap:3px 9px;font-size:10.5px;color:var(--text-dim);}
  .mini-cats span{display:inline-flex;align-items:center;gap:4px;}
  .mini-cats i{width:10px;height:10px;border-radius:2px;display:inline-block;}
  #map-legend{position:absolute;left:10px;bottom:34px;z-index:5;width:230px;background:var(--scrim);border:1px solid var(--border);
    border-radius:6px;padding:6px 8px;font-size:11px;color:var(--text);cursor:pointer;}
  #map-legend[hidden]{display:none;}
  #map-legend .ml-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;font-weight:600;
    cursor:move;touch-action:none;user-select:none;}
  #map-legend .ml-head .ml-grip{color:var(--text-dim);margin-right:4px;letter-spacing:-1px;}
  .maplibregl-ctrl-group button.overlay-btn{display:flex;align-items:center;justify-content:center;color:#303030;}
  .maplibregl-ctrl-group button.overlay-btn.active{background:#b2daf7;color:#14425e;}
  .overlay-panel{position:absolute;right:52px;bottom:30px;z-index:6;width:300px;max-height:min(60vh,520px);overflow-y:auto;
    background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:8px 10px;font-size:11.5px;
    box-shadow:0 4px 16px rgb(0 0 0 / .3);}
  .overlay-panel[hidden]{display:none;}
  .eq-form{display:grid;grid-template-columns:auto 1fr;gap:5px 8px;align-items:center;margin:6px 0;}
  .eq-form label{color:var(--text-dim);font-size:11px;}
  .eq-form select,.eq-form input{width:100%;margin:0;padding:2px 4px;font-size:11px;box-sizing:border-box;}
  .eq-dates{grid-column:1 / -1;display:flex;gap:4px;align-items:center;}
  .eq-dates[hidden]{display:none;}
  .eq-legend{display:flex;flex-wrap:wrap;gap:3px 9px;margin-top:6px;font-size:10.5px;color:var(--text-dim);}
  .eq-legend i{display:inline-block;border-radius:50%;margin-right:3px;vertical-align:-1px;border:1px solid #fff;}
  #browse-card{position:absolute;left:10px;top:128px;z-index:5;width:360px;max-width:calc(100% - 20px);
    max-height:calc(100% - 140px);overflow:auto;resize:both;background:var(--panel);border:1px solid var(--border);
    border-radius:8px;padding:8px 10px;font-size:11.5px;box-shadow:0 4px 16px rgb(0 0 0 / .35);}
  #browse-card[hidden]{display:none;}
  #browse-tabs{flex-wrap:wrap;margin:6px 0 0;}
  #browse-tabs button{padding:2px 7px;font-size:11px;}
  #browse-tabs .bc-wait{color:var(--text-dim);font-size:10.5px;padding:3px 6px;}
  .bc-sub{color:var(--text-dim);font-size:10.5px;line-height:1.35;word-break:break-all;}
  .bc-img{position:relative;min-height:140px;margin:6px 0;border-radius:5px;display:flex;align-items:center;justify-content:center;
    background:repeating-conic-gradient(var(--inset) 0 25%, var(--panel2) 0 50%) 0 0/14px 14px;}
  .bc-img img{max-width:100%;max-height:360px;display:block;}
  .bc-img img[hidden]{display:none;}
  #browse-msg{position:absolute;left:6px;right:6px;bottom:6px;text-align:center;color:var(--text-dim);font-size:10.5px;}
  .bc-ctl{display:flex;flex-wrap:wrap;align-items:center;gap:4px 10px;}
  .bc-ctl label{display:flex;align-items:center;gap:4px;cursor:pointer;}
  .bc-ctl input[type=checkbox]{width:auto;margin:0;}
  .bc-ctl input[type=range]{width:90px;}
  .bc-ctl a{color:var(--accent);}
  .bc-helper code{font-size:10px;user-select:all;}
  .btn.tiny{padding:0 5px;font-size:10px;margin-left:6px;vertical-align:1px;}
  .strip-ctl{display:flex;align-items:center;gap:6px;margin-top:6px;font-size:11px;color:var(--text-dim);}
  .strip-ctl select{width:auto;margin:0;padding:1px 4px;font-size:11px;}
  .strip{display:flex;gap:6px;overflow-x:auto;padding:6px 0 4px;}
  .strip-th{flex:0 0 auto;width:86px;padding:2px;border:2px solid var(--border);border-radius:5px;background:var(--inset);
    cursor:pointer;color:var(--text-dim);font:inherit;font-size:9.5px;line-height:1.2;}
  .strip-th img{width:78px;height:78px;object-fit:contain;display:block;margin:0 auto 2px;}
  .overlay-head{display:flex;justify-content:space-between;align-items:center;font-weight:600;margin-bottom:4px;}
  .ov-row{display:flex;gap:7px;align-items:flex-start;padding:4px 2px;border-bottom:1px solid var(--hairline);cursor:pointer;}
  .ov-row:hover{background:var(--panel2);}
  .ov-sw{width:12px;height:12px;border-radius:3px;flex-shrink:0;margin-top:2px;}
  .ov-row .ov-name{font-weight:600;color:var(--text);}
  .ov-row .ov-sub{color:var(--text-dim);font-size:10.5px;line-height:1.35;}
  .month-chips{display:grid;grid-template-columns:repeat(7,1fr);gap:3px;margin:6px 0;}
  .month-chips .chip{text-align:center;padding:3px 0;}
  .snow-ramp{height:10px;border-radius:4px;background:linear-gradient(90deg,#f2f7fc,#c6dbef,#9ecae1,#6baed6,#3182bd,#08519c,#08306b);
    border:1px solid var(--border);}
  #theme-toggle{position:absolute;top:11px;right:12px;background:none;border:none;color:var(--text-dim);cursor:pointer;font-size:15px;line-height:1;padding:2px 4px;}
  #theme-toggle:hover{color:var(--accent);}
  #edl-btn{position:absolute;top:10px;right:36px;background:none;border:none;color:var(--text-dim);cursor:pointer;
    padding:2px 4px;line-height:0;}
  #edl-btn:hover{color:var(--accent);}
  #srch-btn{position:absolute;top:10px;right:60px;background:none;border:none;color:var(--text-dim);cursor:pointer;
    padding:2px 4px;line-height:0;}
  #srch-btn[hidden]{display:none;}
  #srch-btn:hover,#srch-btn.armed{color:var(--accent);}
  #srch-pop{position:absolute;top:40px;right:8px;z-index:30;width:290px;max-width:calc(100% - 16px);background:var(--panel);
    border:1px solid var(--border);border-radius:8px;padding:8px 10px;font-size:11.5px;font-weight:400;
    box-shadow:0 4px 16px rgb(0 0 0 / .35);}
  #srch-pop[hidden]{display:none;}
  #srch-pop .srch-opt{display:flex;gap:7px;align-items:flex-start;padding:5px 2px;border-bottom:1px solid var(--hairline);cursor:pointer;}
  #srch-pop .srch-opt input{width:auto;margin:2px 0 0;}
  #srch-pop .srch-opt b{display:block;color:var(--text);font-weight:600;}
  #srch-pop .srch-opt span{color:var(--text-dim);font-size:10.5px;}
  #build-status{position:absolute;left:10px;bottom:52px;z-index:8;display:flex;align-items:center;gap:8px;max-width:min(420px,calc(100% - 80px));
    background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:7px 10px;font-size:11.5px;color:var(--text);
    box-shadow:0 4px 16px rgb(0 0 0 / .3);}
  #build-status[hidden]{display:none;}
  #build-status .spin{width:14px;height:14px;flex-shrink:0;border-radius:50%;border:2px solid var(--border);border-top-color:var(--accent);
    animation:bspin .9s linear infinite;}
  #build-status.err .spin{display:none;}
  #build-status.err{border-color:#e5484d;}
  @keyframes bspin{to{transform:rotate(360deg);}}
  #edl-btn .edl-dot{position:absolute;right:1px;bottom:1px;width:7px;height:7px;border-radius:50%;
    background:#6b6b6b;border:1px solid var(--panel);}
  #edl-btn.on .edl-dot{background:#2fbf71;}
  #edl-btn.warn .edl-dot{background:#ffd24d;}
  #edl-pop{position:absolute;top:40px;right:8px;z-index:30;width:290px;max-width:calc(100% - 16px);background:var(--panel);
    border:1px solid var(--border);border-radius:8px;padding:8px 10px;font-size:11.5px;font-weight:400;
    box-shadow:0 4px 16px rgb(0 0 0 / .35);}
  #edl-pop[hidden],#edl-form[hidden]{display:none;}
  #edl-pop label{display:block;margin:6px 0 2px;color:var(--text-dim);font-size:11px;}
  #edl-pop input{width:100%;box-sizing:border-box;}
  #edl-pop code{font-size:10px;user-select:all;word-break:break-all;}
  #edl-pop .bc-ctl{margin-top:8px;}
  .seg{display:flex;gap:4px;margin:6px 0 2px 0;}
  .seg .chip{flex:1;text-align:center;}
  #chart-modal{position:fixed;inset:0;z-index:20;background:rgba(0,0,0,.66);display:flex;align-items:center;justify-content:center;}
  #chart-modal[hidden]{display:none;}
  .chart-card{position:relative;background:var(--panel);border:1px solid var(--border);border-radius:8px;
    padding:12px 14px;width:min(780px,94vw);min-width:min(360px,94vw);min-height:200px;max-width:98vw;max-height:94vh;
    overflow:auto;resize:both;}
  .chart-card.big{width:96vw;height:92vh;}
  #chart-expand{font-size:15px;}
  .chart-head{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;}
  .chart-sub{color:var(--text-dim);font-size:11px;margin:2px 0 8px 0;}
  .chart-tick{fill:var(--text-dim);font-size:10px;}
  .chart-row-label{fill:var(--text);font-size:10.5px;font-family:ui-monospace,Menlo,Consolas,monospace;}
  .chart-grid{stroke:var(--border);stroke-width:1;}
  .chart-blackout{fill:#8c8c8c;fill-opacity:.24;}
  .chart-dot{stroke:var(--panel);stroke-width:2;cursor:pointer;}
  .chart-dot:hover{stroke:#f5f5f5;}
  .chart-ifg{stroke-width:3;stroke-linecap:round;cursor:pointer;}
  .chart-ifg:hover{stroke-width:5;}
  .chart-ifg-end{pointer-events:none;stroke:var(--panel);stroke-width:1;}
  .chart-gap{fill:#e5484d;fill-opacity:.16;stroke:#e5484d;stroke-width:1;stroke-dasharray:4 3;}
  .chart-warn{color:#e5484d;font-weight:600;}
  .chart-ctls{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:2px 10px;margin:2px 8px 0 auto;}
  .chart-flag-ctl{display:flex;align-items:center;gap:4px;font-size:11px;color:var(--text-dim);white-space:nowrap;cursor:pointer;}
  .chart-flag-ctl select{width:auto;margin:0;padding:1px 4px;font-size:11px;}
  .qa-stat{display:flex;flex-wrap:wrap;align-items:center;gap:4px 6px;margin:2px 0 8px;font-size:11px;color:var(--text-dim);}
  .qa-stat .chip{padding:2px 8px;}
  .qa-stat input{width:64px;margin:0;padding:2px 4px;}
  .chart-flag-ctl[hidden]{display:none;}
  .chart-flag-ctl input{width:auto;margin:0;}
  .chart-ifg-off{stroke:#e5484d;stroke-opacity:.45;stroke-width:10;stroke-linecap:round;pointer-events:none;}
  .chart-tip{position:absolute;pointer-events:none;background:var(--inset);border:1px solid var(--border);border-radius:5px;
    padding:5px 7px;font-size:11px;color:var(--text);white-space:nowrap;z-index:2;}
  .chart-tip[hidden]{display:none;}
  .chart-tip .tdim{color:var(--text-dim);}
  .summary-grid{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px;}
  .stat-tile{flex:1;min-width:70px;background:var(--inset);border:1px solid var(--border);border-radius:6px;padding:6px 8px;}
  .stat-tile .num{font-size:16px;font-weight:700;color:var(--text);}
  .stat-tile .cap{font-size:10px;color:var(--text-dim);text-transform:uppercase;letter-spacing:.3px;}
  .bar-row{display:flex;align-items:center;gap:6px;margin:3px 0;font-size:11px;}
  .bar-row .bl{width:64px;color:var(--text-dim);flex-shrink:0;}
  .bar-track{flex:1;height:10px;background:var(--inset);border-radius:5px;overflow:hidden;}
  .bar-fill{display:block;height:100%;border-radius:5px;}
  .bar-row .bn{width:34px;text-align:right;color:var(--text);flex-shrink:0;}
  #menu-btn,#sidebar-close,#sidebar-backdrop{display:none;}
  #daily-chart svg{touch-action:pan-y;}

  /* Phones: the map takes the whole screen and the sidebar slides in over it
     from the menu button. Inputs are 16px so iOS does not zoom on focus. */
  @media (max-width: 768px){
    #app{display:block;height:100dvh;}
    #map{position:absolute;inset:0;}
    #sidebar{position:fixed;top:0;left:0;bottom:0;z-index:40;width:min(88vw,360px);min-width:0;
      transform:translateX(-102%);transition:transform .22s ease;box-shadow:4px 0 18px rgb(0 0 0 / .45);
      padding-bottom:env(safe-area-inset-bottom);}
    #sidebar.open{transform:none;}
    #sidebar-backdrop{position:fixed;inset:0;z-index:39;background:rgb(0 0 0 / .45);}
    #sidebar.open ~ #sidebar-backdrop{display:block;}
    /* The close button takes the corner; the theme, key and search icons
       step left of it. */
    #theme-toggle{right:40px;}
    #edl-btn{right:64px;}
    #srch-btn{right:88px;}
    #sidebar-close{display:block;position:absolute;top:9px;right:6px;background:none;border:none;
      color:var(--text-dim);font-size:22px;line-height:1;padding:2px 6px;cursor:pointer;}
    #menu-btn{display:flex;align-items:center;justify-content:center;position:absolute;top:10px;left:10px;z-index:7;
      width:40px;height:40px;border-radius:8px;border:1px solid var(--border);background:var(--scrim);color:var(--text);
      font-size:20px;cursor:pointer;}
    #search,#map.has-product #search{top:10px;left:58px;right:10px;width:auto;}
    #search input{font-size:16px;padding:8px 10px;}
    #product-ctrl,#map.has-product #pass-ctrl,#pass-ctrl{top:58px;}
    #product-ctrl{left:10px;}
    #map.has-product #pass-ctrl{left:132px;}
    #pass-ctrl{left:10px;padding:5px 7px;gap:6px;}
    #basemap-ctrl{top:96px;left:10px;right:auto;flex-wrap:wrap;gap:6px;padding:5px 7px;font-size:11px;}
    #top-hint,.maplibregl-ctrl-zoom-in,.maplibregl-ctrl-zoom-out{display:none !important;}
    #click-ctrl{top:134px;left:10px;right:auto;padding:5px 7px;}
    .maplibregl-ctrl-bottom-right{margin-bottom:env(safe-area-inset-bottom);}
    .overlay-panel{left:8px;right:56px;width:auto;bottom:calc(8px + env(safe-area-inset-bottom));max-height:52vh;}
    #browse-card{left:8px;right:8px;width:auto;max-width:none;top:auto;bottom:calc(8px + env(safe-area-inset-bottom));
      max-height:62vh;resize:none;}
    #colorby-panel{width:auto;max-height:52vh;}
    #map-legend{left:8px;bottom:calc(34px + env(safe-area-inset-bottom));width:auto;max-width:calc(100vw - 72px);}
    .maplibregl-popup{max-width:92vw !important;}
    .maplibregl-popup-content{max-height:58vh;overflow-y:auto;}
    .chart-card{width:98vw;max-width:98vw;max-height:92dvh;padding:10px;}
    input[type=text],input[type=search],input[type=number],select{font-size:16px;}
    .date-row input[type=date]{font-size:14px;min-width:0;}
    .footer-actions{padding:8px 10px calc(8px + env(safe-area-inset-bottom));gap:6px;}
    .footer-actions .btn{flex:1 1 45%;padding:8px 6px;}
    .chip{padding:5px 9px;font-size:12px;}
    .cmap-grid{grid-template-columns:repeat(2,minmax(0,1fr));}
  }

  ::-webkit-scrollbar{width:8px;height:8px;}
  ::-webkit-scrollbar-thumb{background:var(--border);border-radius:4px;}
  #count-badge{background:var(--accent);color:#1a1a1a;border-radius:10px;padding:0 6px;font-size:10px;font-weight:700;}
"""


BODY_HTML = r"""<body>
<div id="app">
  <div id="sidebar">
    <h1>OPERA NISAR-DB Viewer
      <button id="theme-toggle" title="Switch to the light theme">&#9788;</button>
      <button id="srch-btn" hidden title="Search CMR and rebuild the viewer (local)" aria-label="Search and rebuild" aria-expanded="false">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
          stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"/><path d="m20 20-4.8-4.8"/></svg>
      </button>
      <div id="srch-pop" hidden>
        <div class="overlay-head"><span>Search CMR &amp; rebuild</span>
          <button class="li-x" id="srch-close" title="Close">&times;</button></div>
        <label class="srch-opt"><input type="radio" name="srch-scope" value="na" checked>
          <div><b>OPERA North America</b><span>the 1,295 OPERA frames, as published (about a minute)</span></div></label>
        <label class="srch-opt"><input type="radio" name="srch-scope" value="globe">
          <div><b>Globe</b><span>every NISAR frame (~30,000); a few minutes and a large page</span></div></label>
        <label class="srch-opt"><input type="radio" name="srch-scope" value="bbox">
          <div><b>Screen view</b><span id="srch-bbox">the frames in the map's current view</span></div></label>
        <div class="bc-ctl"><button type="button" class="btn small primary" id="srch-go">Search &amp; rebuild</button>
          <a href="/" id="srch-home">back to the published view</a></div>
        <div class="stat-line">Searches CMR for GSLC and GUNW granules, builds the page on the QA helper and opens it.
          Clicking the magnifier again starts it too.</div>
      </div>
      <button id="edl-btn" title="Earthdata login for the QA images" aria-label="Earthdata login" aria-expanded="false">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
          stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <circle cx="7.5" cy="15.5" r="4.5"/><path d="M10.7 12.3 20 3M16 7l3 3M14 9l2 2"/></svg>
        <span class="edl-dot"></span>
      </button>
      <div id="edl-pop" hidden>
        <div class="overlay-head"><span>Earthdata login &middot; QA images</span>
          <button class="li-x" id="edl-close" title="Close">&times;</button></div>
        <div class="stat-line bc-helper" id="edl-status">Checking for the QA helper...</div>
        <form id="edl-form" hidden>
          <label for="edl-user">Earthdata username</label>
          <input type="text" id="edl-user" autocomplete="username" spellcheck="false">
          <label for="edl-pass">Password</label>
          <input type="password" id="edl-pass" autocomplete="current-password">
          <div class="bc-ctl"><button type="submit" class="btn small primary">Log in</button><span id="edl-msg"></span></div>
        </form>
        <div class="bc-ctl" id="edl-actions"></div>
        <div class="stat-line">The login goes only to the QA helper on this machine, which keeps it in memory and
          uses it only with Earthdata. The page itself never stores the password.</div>
      </div>
      <small><span id="hdr-scope">North America</span> &middot; <span id="hdr-count">0</span> frames shown</small>
      <small id="hdr-queried">CMR queried: unknown</small>
      <button id="sidebar-close" title="Close the panel" aria-label="Close the panel">&times;</button>
    </h1>
    <div id="sidebar-scroll">

      <div class="section">
        <div class="section-head" data-target="sec-daily"><span id="daily-title">GSLC Acquisitions Over Time</span><span class="chev">&#9660;</span></div>
        <div class="section-body" id="sec-daily">
          <div class="stat-line" id="daily-note">GSLC granules in CMR over North America, counted by acquisition date across the frames currently shown. Hover a bar for its count.</div>
          <div class="stat-line">Drag across the bars to pick a date range; the frame colours count only what falls inside it.</div>
          <div class="date-row">
            <input type="date" id="f-date-start" aria-label="First acquisition date">
            <span style="color:var(--text-dim);font-size:11px;">to</span>
            <input type="date" id="f-date-end" aria-label="Last acquisition date">
            <button class="btn small" id="btn-date-reset" title="Show the full record">All</button>
          </div>
          <div id="daily-wrap" style="position:relative;margin-top:6px;">
            <div id="daily-chart"></div>
            <div class="chart-tip" id="daily-tip" hidden></div>
          </div>
        </div>
      </div>


      <div class="section">
        <div class="section-head" data-target="sec-cons"><span>Consistent Mode Summary</span><span class="chev">&#9660;</span></div>
        <div class="section-body" id="sec-cons">
          <div class="stat-line">Consistent (mode, coverage) chosen per frame for DISP time series, aggregated over the frames currently shown.</div>
          <div id="cons-summary"></div>
        </div>
      </div>



      <div class="section">
        <div class="section-head" data-target="sec-style"><span>Frame Color / Opacity</span><span class="chev">&#9660;</span></div>
        <div class="section-body" id="sec-style">
          <div class="cb-tabs" id="sb-tabs" role="tablist" style="margin:2px 0 6px;">
            <button type="button" data-sbtab="1" class="active">Color by</button>
            <button type="button" data-sbtab="2">Style</button>
          </div>
          <select id="color-by" hidden aria-hidden="true">
            <option value="passDirection">Pass Direction</option>
            <option data-product="gslc" value="gslc_count" selected>GSLC acquisitions in CMR (default)</option>
            <option data-product="gslc" value="n_duplicate">Duplicate granules (same date &amp; mode)</option>
            <option data-product="gslc" value="cons_mode">Consistent mode</option>
            <option data-product="gslc" value="cons_cov">Consistent coverage (full/partial)</option>
            <option data-product="gslc" value="n_modes">Distinct modes per frame</option>
            <option value="rollout" id="opt-rollout" hidden>Rollout option (earliest)</option>
            <option value="blackout_months" id="opt-blackout" hidden>Blackout duration (months)</option>
            <option value="blackout_month" id="opt-blackout-month" hidden>Blackout share of a month</option>
            <option data-product="gslc" value="gslc_modes">GSLC mode (most common)</option>
            <option data-product="gslc" value="gslc_pols">GSLC polarization (most common)</option>
            <option data-product="gunw" value="gunw_count" id="opt-gunw" hidden>GUNW interferograms</option>
            <option data-product="gunw" value="gunw_net" id="opt-gunw-net" hidden>GUNW network (connected / disconnected)</option>
            <option value="flag_j" class="opt-flag" hidden>Flag: joint observation</option>
            <option value="flag_f" class="opt-flag" hidden>Flag: full frame</option>
            <option value="flag_o" class="opt-flag" hidden>Flag: orbit type</option>
            <option value="flag_r" class="opt-flag" hidden>Flag: RFI mitigation applied</option>
            <option value="flag_m" class="opt-flag" hidden>Flag: mixed mode</option>
            <option value="flag_d" class="opt-flag" hidden>Flag: dithered</option>
            <option data-product="gslc" value="gslc_qa_rl" class="opt-qa" hidden>QA: RFI likelihood</option>
            <option data-product="gunw" value="gunw_qa_cm" class="opt-qa" hidden>QA: coherence median</option>
            <option data-product="gunw" value="gunw_qa_ca" class="opt-qa" hidden>QA: coherence mean</option>
            <option data-product="gunw" value="gunw_qa_v" class="opt-qa" hidden>QA: valid unwrapped (%)</option>
            <option data-product="gunw" value="gunw_qa_l" class="opt-qa" hidden>QA: largest region (%)</option>
            <option data-product="gunw" value="gunw_qa_n" class="opt-qa" hidden>QA: connected components</option>
            <option data-product="gunw" value="gunw_qa_im" class="opt-qa" hidden>QA: ionosphere mean</option>
            <option data-product="gunw" value="gunw_qa_imd" class="opt-qa" hidden>QA: ionosphere median</option>
            <option data-product="gunw" value="gunw_qa_is" class="opt-qa" hidden>QA: ionosphere spread</option>
            <option data-product="gunw" value="gunw_qa_iu" class="opt-qa" hidden>QA: ionosphere uncertainty</option>
            <option data-product="gunw" value="gunw_qa_rl" class="opt-qa" hidden>QA: RFI likelihood (pair)</option>
          </select>
          <div id="sb-tab-options">
            <div id="sb-options"></div>
            <div id="bo-month-row" hidden>
              <label>Month</label>
              <select id="bo-month"></select>
            </div>
            <div class="stat-line" id="colorby-scope">Counts and QA follow the mode / polarization chips and the date range.</div>
            <div class="cb-legend" id="sb-legend"></div>
          </div>
          <div id="sb-tab-style" hidden>
            <div class="cb-slider">Fill <input type="range" id="fill-opacity" min="0" max="100" value="32"><b><span id="opacity-val">32</span>%</b></div>
            <div class="cb-presets" id="sb-presets"></div>
            <div class="cb-slider">Outline <input type="range" id="outline-opacity" min="0" max="100" value="70"><b><span id="outline-val">70</span>%</b></div>
            <div id="colorby-legend" style="margin-top:6px;"></div>
            <button class="btn small" id="btn-reset-style" style="margin-top:8px;">Reset all to default</button>
          </div>
        </div>
      </div>


      <div class="section">
        <div class="section-head" data-target="sec-gslc"><span id="chips-title">GSLC Mode / Polarization / CRID</span><span class="chev">&#9660;</span></div>
        <div class="section-body" id="sec-gslc">
          <label>Mode (click to toggle)</label>
          <div class="chip-grid" id="chips-gslc-mode"></div>
          <label>Polarization</label>
          <div class="chip-grid" id="chips-gslc-pol"></div>
          <label>CRID (composite release ID in the file name; none = all)</label>
          <div class="chip-grid" id="chips-crid"></div>
        </div>
      </div>


      <div class="section collapsed" id="sec-rollout-wrap" hidden>
        <div class="section-head" data-target="sec-rollout"><span>Rollout Regions</span><span class="chev">&#9660;</span></div>
        <div class="section-body" id="sec-rollout">
          <div class="stat-line" id="rollout-note"></div>
          <div id="rollout-list"></div>
          <div class="stat-line">Click an option to show only its frames; frame counts follow the other filters.</div>
        </div>
      </div>


      <div class="section collapsed" id="sec-bo-wrap" hidden>
        <div class="section-head" data-target="sec-bo"><span>Blackout by Month</span><span class="chev">&#9660;</span></div>
        <div class="section-body" id="sec-bo">
          <div class="stat-line">Frames shown with each calendar month excluded by their blackout window. Dark: at least half the month; light: any part of it. Click a month to colour the map by it.</div>
          <div id="bo-wrap" style="position:relative;margin-top:6px;">
            <div id="bo-chart"></div>
            <div class="chart-tip" id="bo-tip" hidden></div>
          </div>
        </div>
      </div>


      <div class="section collapsed">
        <div class="section-head" data-target="sec-loc"><span>Location (Track / Frame / Cycle)</span><span class="chev">&#9660;</span></div>
        <div class="section-body" id="sec-loc">
          <label>Track (e.g. "12" or "10-20" or "12,34,56")</label>
          <input type="text" id="f-track" placeholder="all tracks">
          <label>Frame</label>
          <input type="text" id="f-frame" placeholder="all frames">
          <label>Cycle (e.g. "23" or "20-25"); a GUNW pair matches on either acquisition</label>
          <input type="text" id="f-cycle" placeholder="all cycles">
          <label>Frame ID (e.g. 8109) or track_frame (e.g. 34_19)</label>
          <input type="text" id="f-id" placeholder="e.g. 8109 or 34_19">
        </div>
      </div>


      <div class="section">
        <div class="section-head" data-target="sec-flags"><span>Product / Site Flags</span><span class="chev">&#9660;</span></div>
        <div class="section-body" id="sec-flags">
          <div class="check-row"><input type="checkbox" id="f-calval"><label for="f-calval" style="margin:0;color:var(--text)">CalVal frames only</label></div>
          <div class="check-row"><input type="checkbox" id="f-selected-only"><label for="f-selected-only" style="margin:0;color:var(--text)">Show only selected frames</label></div>
          <div class="check-row" id="row-gps" hidden><input type="checkbox" id="f-gps-show"><label for="f-gps-show" style="margin:0;color:var(--text)">Show UNR GPS sites (<span id="gps-count">0</span>)</label></div>
          <div class="stat-line" id="gps-hint" hidden>Nevada Geodetic Laboratory sites; click one for its position time series.</div>
        </div>
      </div>




      <div class="section collapsed">
        <div class="section-head" data-target="sec-paint"><span>Paint / Select Frames</span><span class="chev">&#9660;</span></div>
        <div class="section-body" id="sec-paint">
          <div id="palette"></div>
          <div class="stat-line">Click a frame on the map to open its granule list and add/remove it from your selection with this color.</div>
          <label style="margin-top:8px;">Import selection (CSV / GeoJSON / consistent-GSLC JSON)</label>
          <input type="file" id="import-file" accept=".csv,.json,.geojson" style="font-size:11px;color:var(--text-dim);">
          <div class="stat-line" id="import-status">CSV needs <code>track,frame</code> columns (optional <code>color</code>). GeoJSON matches on <code>track/frame</code> or <code>id</code>. A consistent-GSLC JSON selects every frame in its <code>data</code> block.</div>
        </div>
      </div>


      <div class="section collapsed">
        <div class="section-head" data-target="sec-sel"><span>Selected List <span id="count-badge">0</span></span><span class="chev">&#9660;</span></div>
        <div class="section-body" id="sec-sel">
          <ul id="selected-list"></ul>
          <div class="stat-line" id="empty-sel-hint">No frames selected yet.</div>
        </div>
      </div>

    </div>
    <div class="footer-actions">
      <button class="btn" id="btn-clear-filters">Reset filters</button>
      <button class="btn danger" id="btn-clear-sel">Clear selection</button>
      <button class="btn primary" id="btn-export-csv">Export CSV</button>
      <button class="btn primary" id="btn-export-geojson">Export GeoJSON</button>
    </div>
  </div>
  <div id="sidebar-backdrop"></div>
  <div id="map">
    <button id="menu-btn" title="Filters and settings" aria-label="Open filters and settings">&#9776;</button>
    <div id="product-ctrl" role="group" aria-label="Product shown on the map" hidden>
      <button type="button" data-product="gslc" class="active">GSLC</button>
      <button type="button" data-product="gunw">GUNW</button>
    </div>
    <div id="pass-ctrl">
      <label><input type="radio" name="pass" value="all" checked> All</label>
      <label><input type="radio" name="pass" value="Ascending"> Asc</label>
      <label><input type="radio" name="pass" value="Descending"> Desc</label>
    </div>
    <div id="click-ctrl">
      <label><input type="checkbox" id="f-frame-popup" checked> Frame popup</label>
    </div>
    <div id="search">
      <input id="search-q" type="search" autocomplete="off" spellcheck="false"
             placeholder="Place, lat lon, frame id or T12_F34">
      <div id="search-results" hidden></div>
    </div>
    <div id="top-hint">Click a frame to list granules &amp; select &middot; the (i) button toggles hover summaries</div>
    <div id="basemap-ctrl">
      <label><input type="radio" name="basemap" value="light" checked> Light</label>
      <label><input type="radio" name="basemap" value="dark"> Dark</label>
      <label><input type="radio" name="basemap" value="sat"> Satellite</label>
      <label><input type="radio" name="basemap" value="sat2"> Satellite-H</label>
    </div>
    <div class="overlay-panel" id="colorby-panel" hidden>
      <div class="overlay-head">
        <div class="cb-tabs" role="tablist">
          <button type="button" data-tab="1" class="active">Color by</button>
          <button type="button" data-tab="2">Style</button>
        </div>
        <button class="li-x" data-close="colorby" title="Close the panel (the legend stays on the map)">&times;</button>
      </div>
      <div class="cb-note" id="cb-overlay-note" hidden>Frame colours are hidden under the rainy / snow layer.
        <button class="btn small" id="cb-overlay-off">Show them</button></div>
      <div id="cb-options"></div>
      <div id="cb-style" hidden>
        <div class="pop-title" id="cb-style-title"></div>
        <div class="cb-slider">Fill <input type="range" id="cb-fill" min="0" max="100"><b id="cb-fill-val"></b></div>
        <div class="cb-presets" id="cb-presets"></div>
        <div class="cb-slider">Outline <input type="range" id="cb-outline" min="0" max="100"><b id="cb-outline-val"></b></div>
        <div id="cb-month-row" hidden><div class="month-chips" id="cb-months"></div></div>
        <div id="cb-style-controls" style="margin-top:6px;"></div>
        <button class="btn small" id="cb-reset" style="margin-top:8px;">Reset this option</button>
      </div>
      <div class="cb-legend" id="cb-legend"></div>
    </div>
    <div id="map-legend" hidden title="Click to change the colouring">
      <div class="ml-head" title="Drag to move"><span><span class="ml-grip">&#8942;&#8942;</span><span id="ml-title"></span></span><button class="li-x" id="ml-close" title="Hide the legend">&times;</button></div>
      <div id="ml-body"></div>
    </div>
    <div class="overlay-panel" id="snow-panel" hidden>
      <div class="overlay-head"><span>Rainy / snow season blackouts</span><button class="li-x" data-close="snow" title="Close">&times;</button></div>
      <div class="pop-row">Dates DISP drops for seasonal decorrelation: winter snow cover, or the peak rainy season (Aug-Nov) over Central America, as in DISP-S1. <b>YR</b> shades each frame by the share of the year blacked out; a month, by the share of that month.</div>
      <div class="month-chips" id="snow-months"></div>
      <div class="snow-ramp"></div>
      <div class="cmap-labels"><span id="snow-lo">0%</span><span id="snow-unit">share of the year blacked out</span><span id="snow-hi">100%</span></div>
      <div class="stat-line" id="snow-stat"></div>
    </div>
    <div class="overlay-panel" id="quake-panel" hidden>
      <div class="overlay-head"><span>Earthquakes (USGS)</span><button class="li-x" data-close="quake" title="Close">&times;</button></div>
      <div class="eq-form">
        <label for="eq-mag">Min magnitude</label>
        <select id="eq-mag"><option>2.5</option><option>3</option><option>4</option><option selected>4.5</option>
          <option>5</option><option>6</option><option>7</option></select>
        <label for="eq-period">Period</label>
        <select id="eq-period"><option value="7">last 7 days</option><option value="30">last 30 days</option>
          <option value="365" selected>last year</option><option value="1825">last 5 years</option>
          <option value="custom">dates...</option></select>
        <div class="eq-dates" id="eq-dates" hidden><input type="date" id="eq-start"><span>to</span><input type="date" id="eq-end"></div>
        <label for="eq-area">Area</label>
        <select id="eq-area"><option value="page" selected>this page's area</option><option value="view">current map view</option>
          <option value="world">whole world</option></select>
      </div>
      <div class="bc-ctl"><button type="button" class="btn small primary" id="eq-apply">Show</button><span id="eq-status" class="tdim"></span></div>
      <div class="eq-legend" id="eq-legend"></div>
      <div class="stat-line">USGS ComCat via its FDSN event service; at most 20,000 events per request.
        Click an event for its details.</div>
    </div>
    <div class="overlay-panel" id="quakeleg-panel" hidden>
      <div class="overlay-head"><span>Earthquakes (USGS)</span><button class="li-x" data-close="quakeleg" title="Close">&times;</button></div>
      <div class="tdim" id="eq-leg-what"></div>
      <div class="eq-legend" id="eq-legend-2"></div>
    </div>
    <div class="overlay-panel" id="rollout-panel" hidden>
      <div class="overlay-head"><span>Rollout regions</span><button class="li-x" data-close="rollout" title="Close">&times;</button></div>
      <div class="pop-row" id="rollout-panel-note"></div>
      <div id="rollout-panel-list"></div>
    </div>
    <div id="build-status" hidden role="status" aria-live="polite"><span class="spin"></span><span id="build-text"></span>
      <button class="li-x" id="build-x" title="Hide" hidden>&times;</button></div>
    <div id="browse-card" hidden>
      <div class="overlay-head"><span id="browse-title">Browse</span><button class="li-x" id="browse-close" title="Close">&times;</button></div>
      <div class="bc-sub" id="browse-sub"></div>
      <div class="cb-tabs" id="browse-tabs" role="tablist"></div>
      <div class="bc-img"><img id="browse-img" alt="" hidden><div id="browse-msg"></div></div>
      <div class="bc-ctl">
        <label title="Place this image on the map"><input type="checkbox" id="browse-map"> on map</label>
        <input type="range" id="browse-opacity" min="10" max="100" value="85" title="Opacity on the map">
        <a id="browse-full" target="_blank" rel="noopener">full size</a>
        <a id="browse-report" target="_blank" rel="noopener" title="Needs your Earthdata login">QA report</a>
      </div>
      <div class="stat-line bc-helper" id="browse-helper"></div>
    </div>
    <div id="chart-modal" hidden>
      <div class="chart-card">
        <div class="chart-head">
          <div>
            <div class="pop-title" id="chart-title"></div>
            <div class="chart-sub" id="chart-sub"></div>
          </div>
          <div class="chart-ctls">
            <label class="chart-flag-ctl" id="chart-flags-ctl" hidden><input type="checkbox" id="chart-flags"> Show flags</label>
            <label class="chart-flag-ctl" id="chart-qa-ctl" hidden><input type="checkbox" id="chart-qa"> Show QA</label>
            <label class="chart-flag-ctl" id="chart-qa-color-ctl" hidden>Colour pairs
              <select id="chart-qa-color"><option value="">by mode</option></select></label>
          </div>
          <button class="li-x" id="chart-expand" title="Expand">&#10530;</button>
          <button class="li-x" id="chart-close" title="Close">&times;</button>
        </div>
        <div id="chart-body"></div>
        <div class="chart-tip" id="chart-tip" hidden></div>
      </div>
    </div>
  </div>
</div>
"""


APP_JS = r"""
(function(){
  const PALETTE = ["#ff5d5d","#ff8a4d","#ffd24d","#7ee787","#4dd2c9","#4da3ff","#a389ff","#ff6fc7","#ffffff"];
  let currentColor = PALETTE[0];
  let applyColorBy = function(){};   // reassigned after map layers exist
  let paintFrames = function(){};    // repaints the map only, no legend redraw
  const frameStyle = {fill: 32, outline: 70};   // percent
  let refreshSummary = function(){}; // reassigned after DOM ready
  const selected = new Map();        // id -> {feature, color}
  let product = "gslc";              // "gslc" or "gunw", flipped by the product switch
  const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  const DAY_MS = 86400000;

  // ---------- derive filter option lists ----------
  function uniqSorted(arr){ return Array.from(new Set(arr)).sort(); }
  // Frames observed in several modes are labelled by the mode they were actually
  // acquired in most often, which is what the consistent-mode vote also sees.
  function mostCommon(values){
    const counts = new Map();
    values.forEach(v=>{ if (v != null) counts.set(v, (counts.get(v)||0)+1); });
    let best = null, bestN = 0;
    counts.forEach((n, v)=>{ if (n > bestN || (n === bestN && best !== null && v < best)) { best = v; bestN = n; } });
    return best;
  }

  const allModesGslc = uniqSorted(FRAME_DATA.features.flatMap(f => f.properties.gslc_modes));
  const allPolsGslc  = uniqSorted(FRAME_DATA.features.flatMap(f => f.properties.gslc_pols));
  const allModesGunw = uniqSorted(FRAME_DATA.features.flatMap(f => asArray(f.properties.gunw_modes)));
  const allPolsGunw  = uniqSorted(FRAME_DATA.features.flatMap(f => asArray(f.properties.gunw_pols)));
  // Each product keeps its own chip selection across the GSLC / GUNW switch.
  const activeChips = { gslcMode:new Set(), gslcPol:new Set(), gunwMode:new Set(), gunwPol:new Set(),
                        gslcCrid:new Set(), gunwCrid:new Set() };

  // The CRID (composite release ID, e.g. P05023) names the processing release a
  // granule came from; it sits at a fixed field of the GSLC and GUNW names, the
  // same ones ``nisar_db.filenames`` parses.
  function cridOf(gid, field){
    const parts = String(gid || "").split("_");
    return parts.length > field ? parts[field] : "";
  }
  FRAME_DATA.features.forEach(f=>{
    const p = f.properties;
    const granules = Array.isArray(p.granules) ? p.granules : [];
    // Pages built from the CMR catalog stored the cycle as text ("024").
    granules.forEach(g=>{ g.crid = cridOf(g.gid, 13); g.cycle = parseInt(g.cycle, 10); });
    p.gslc_crids = uniqSorted(granules.map(g=>g.crid).filter(Boolean));
    p.gslc_cycles = Array.from(new Set(granules.map(g=>g.cycle).filter(Number.isFinite)));
    const ifgs = asArray(p.gunw_ifgs);
    // A GUNW name carries the reference and secondary cycles (fields 4 and 8).
    ifgs.forEach(g=>{
      g.crid = cridOf(g.gid, 15);
      g.cyc = [4, 8].map(i=>parseInt(String(g.gid || "").split("_")[i], 10)).filter(Number.isFinite);
    });
    if (typeof p.gunw_ifgs === "string") p.gunw_ifgs = ifgs;
    p.gunw_crids = uniqSorted(ifgs.map(g=>g.crid).filter(Boolean));
    p.gunw_cycles = Array.from(new Set(ifgs.flatMap(g=>g.cyc)));
  });
  // A pair's RFI likelihood is the larger of its two acquisitions', read from
  // the frame's GSLCs; the GUNW's own QA has none.
  function attachPairRfi(p){
    const byDate = new Map();
    (Array.isArray(p.granules) ? p.granules : []).forEach(g=>{
      if (g.qa && Number.isFinite(g.qa.rl)) byDate.set(g.date, Math.max(byDate.get(g.date) ?? -Infinity, g.qa.rl));
    });
    if (!byDate.size) return;
    asArray(p.gunw_ifgs).forEach(g=>{
      const vals = [byDate.get(g.ref), byDate.get(g.sec)].filter(v=>v != null);
      if (vals.length) g.rl = Math.max(...vals);
    });
  }
  FRAME_DATA.features.forEach(f=> attachPairRfi(f.properties));
  const allCridsGslc = uniqSorted(FRAME_DATA.features.flatMap(f => f.properties.gslc_crids));
  const allCridsGunw = uniqSorted(FRAME_DATA.features.flatMap(f => f.properties.gunw_crids));

  // Single-value derived keys for the array-valued mode/pol properties.
  FRAME_DATA.features.forEach(f=>{
    const p = f.properties;
    const granules = Array.isArray(p.granules) ? p.granules : [];
    p._gslcMode = mostCommon(granules.map(g=>g.mode)) || (p.gslc_modes[0] || "none");
    p._gslcPol  = mostCommon(granules.map(g=>g.pol))  || (p.gslc_pols[0]  || "none");
  });

  // ---------- color-by support ----------
  const CAT_PALETTE = ["#4da3ff","#ff8a4d","#7ee787","#ff5d5d","#a389ff","#ffd24d","#4dd2c9","#ff6fc7",
                       "#f0b429","#6ee7b7","#93c5fd","#fca5a5","#c4b5fd","#fda4af","#86efac","#fcd34d"];
  // Ramps for numeric color-by fields, 9 evenly spaced stops each, shown in the
  // picker under the group headings of COLORMAP_GROUPS. "OPERA" is the
  // viewer's original ramp. The perceptual and diverging maps are the
  // matplotlib, cmocean (Thyng et al., 2016) and Scientific colour maps
  // (Crameri, 2018, doi:10.5281/zenodo.1243862) samples the geepers grid
  // browser offers; the single- and multi-hue ones are ColorBrewer's 9-class
  // schemes (Harrower & Brewer, 2003, doi:10.1179/000870403235002042).
  const COLORMAPS = {
    OPERA:   ["#2c7bb6","#00a6ca","#00ccbc","#90eb9d","#ffff8c","#f9d057","#f29e2e","#e76818","#d7191c"],
    Viridis: ["#440154","#472d7b","#3b528b","#2c728e","#21918c","#28ae80","#5ec962","#addc30","#fde725"],
    Turbo:   ["#30123b","#4458cb","#3e9bfe","#18d6cb","#46f884","#a2fc3c","#e1dd37","#fea130","#ef5a11"],
    Magma:   ["#000004","#1d1147","#51127c","#822681","#b73779","#e75263","#fc8961","#fec488","#fcfdbf"],
    Plasma:  ["#0d0887","#4c02a1","#7e03a8","#aa2395","#cc4778","#e66c5c","#f89540","#fdc527","#f0f921"],
    Inferno: ["#000004","#210c4a","#57106e","#8a226a","#bc3754","#e45a31","#f98e09","#f9cb35","#fcffa4"],
    Cividis: ["#00224e","#1a386f","#434e6c","#61656f","#7d7c78","#9b9476","#bcae6c","#dec958","#fee838"],
    Batlow:  ["#011959","#114360","#226061","#4d734d","#828231","#c09036","#f29d6d","#fdb4b6","#faccfa"],
    Thermal: ["#042333","#19337c","#563b9c","#83508f","#b15f82","#df7064","#f99341","#f9c641","#e8fa5b"],
    Spectral:["#5e4fa2","#3d95b8","#86cfa5","#d6ee9b","#ffffbe","#fed481","#f98e52","#dd4a4c","#9e0142"],
    RdYlBu:  ["#313695","#4f81ba","#8ec2dc","#d1ecf4","#feffc0","#fed485","#f98e52","#de402e","#a50026"],
    Greys:   ["#ffffff","#f0f0f0","#d9d9d9","#bdbdbd","#959595","#727272","#515151","#242424","#000000"],
    Blues:   ["#f7fbff","#deebf7","#c6dbef","#9ecae1","#6baed6","#4292c6","#2171b5","#08519c","#08306b"],
    Greens:  ["#f7fcf5","#e5f5e0","#c7e9c0","#a1d99b","#74c476","#41ab5d","#238b45","#006d2c","#00441b"],
    Oranges: ["#fff5eb","#fee6ce","#fdd0a2","#fdae6b","#fd8d3c","#f16913","#d94801","#a63603","#7f2704"],
    Reds:    ["#fff5f0","#fee0d2","#fcbba1","#fc9272","#fb6a4a","#ef3b2c","#cb181d","#a50f15","#67000d"],
    Purples: ["#fcfbfd","#efedf5","#dadaeb","#bcbddc","#9e9ac8","#807dba","#6a51a3","#54278f","#3f007d"],
    YlOrRd:  ["#ffffcc","#ffeda0","#fed976","#feb24c","#fd8d3c","#fc4e2a","#e31a1c","#bd0026","#800026"],
    YlOrBr:  ["#ffffe5","#fff7bc","#fee391","#fec44f","#fe9929","#ec7014","#cc4c02","#993404","#662506"],
    YlGnBu:  ["#ffffd9","#edf8b1","#c7e9b4","#7fcdbb","#41b6c4","#1d91c0","#225ea8","#253494","#081d58"],
    GnBu:    ["#f7fcf0","#e0f3db","#ccebc5","#a8ddb5","#7bccc4","#4eb3d3","#2b8cbe","#0868ac","#084081"],
    PuBuGn:  ["#fff7fb","#ece2f0","#d0d1e6","#a6bddb","#67a9cf","#3690c0","#02818a","#016c59","#014636"],
    RdPu:    ["#fff7f3","#fde0dd","#fcc5c0","#fa9fb5","#f768a1","#dd3497","#ae017e","#7a0177","#49006a"],
    RdBu:    ["#b2182b","#d6604d","#f4a582","#fddbc7","#f7f7f7","#d1e5f0","#92c5de","#4393c3","#2166ac"],
    BrBG:    ["#8c510a","#bf812d","#dfc27d","#f6e8c3","#f5f5f5","#c7eae5","#80cdc1","#35978f","#01665e"],
    PuOr:    ["#7f3b08","#be630a","#ef9e3c","#fed7a2","#f6f6f7","#cecde4","#988dbe","#5d3790","#2d004b"],
    PiYG:    ["#8e0152","#cb3289","#e897c4","#fad6ea","#f7f7f6","#d9f0bc","#9acd61","#589b28","#276419"],
    Coolwarm:["#3b4cc0","#6282ea","#8db0fe","#b9d0f9","#dddcdc","#f5c4ac","#f4987a","#dd5f4b","#b40426"],
    Vik:     ["#001261","#034481","#307da6","#94bed2","#ece5e0","#dbaa8d","#c27041","#912d06","#590008"],
    Roma:    ["#7e1700","#9d5818","#b68c32","#d0ca72","#c0eac3","#76d1d7","#389cc6","#2269b0","#033198"],
    Balance: ["#181c43","#2548b0","#3888ba","#98bac5","#f1eceb","#d7a290","#bf573a","#8d1029","#3c0912"]
  };
  const COLORMAP_GROUPS = [
    ["Perceptual", ["OPERA","Viridis","Turbo","Magma","Plasma","Inferno","Cividis","Batlow","Thermal"]],
    ["Single hue", ["Blues","Greens","Oranges","Reds","Purples","Greys"]],
    ["Multi hue", ["YlOrRd","YlOrBr","YlGnBu","GnBu","PuBuGn","RdPu"]],
    ["Diverging", ["Spectral","RdYlBu","RdBu","BrBG","PuOr","PiYG","Coolwarm","Vik","Roma","Balance"]]
  ];
  const DEFAULT_CMAP = "OPERA";

  const COLOR_BY_FIELDS = {
    passDirection: { label:"Pass Direction",      key:"passDirection", kind:"cat" },
    gslc_count:    { label:"GSLC acquisitions in CMR", key:"gslc_count_sel", kind:"num" },
    n_duplicate:   { label:"Duplicate granules",   key:"n_duplicate_sel", kind:"num" },
    cons_mode:     { label:"Consistent mode",      key:"cons_mode",     kind:"cat" },
    cons_cov:      { label:"Consistent coverage",  key:"cons_cov",      kind:"cat" },
    n_modes:       { label:"Distinct modes",       key:"n_modes_sel",   kind:"num" },
    rollout:       { label:"Rollout option",       key:"_rollout",      kind:"cat" },
    blackout_months:{ label:"Blackout months",     key:"blackout_months", kind:"num" },
    blackout_month:{ label:"Blacked out (%)",      key:"_bo_sel",       kind:"num" },
    gslc_modes:    { label:"GSLC mode",            key:"_gslcMode",     kind:"cat" },
    gslc_pols:     { label:"GSLC polarization",    key:"_gslcPol",      kind:"cat" },
    gunw_count:    { label:"GUNW interferograms",  key:"gunw_count_sel", kind:"num" },
    gunw_net:      { label:"GUNW network",         key:"_gunwNet",      kind:"cat" },
    flag_j:        { label:"Joint observation",    key:"_flag_j",       kind:"cat" },
    flag_f:        { label:"Full frame",           key:"_flag_f",       kind:"cat" },
    flag_o:        { label:"Orbit type",           key:"_flag_o",       kind:"cat" },
    flag_r:        { label:"RFI mitigation applied", key:"_flag_r",     kind:"cat" },
    flag_m:        { label:"Mixed mode",           key:"_flag_m",       kind:"cat" },
    flag_d:        { label:"Dithered",             key:"_flag_d",       kind:"cat" }
  };
  // ---------- per-granule QA metrics ----------
  // Read from each product's QA_STATS.h5 (collect_granule_qa.py); an entry
  // without ``qa`` has not been read. ``dir`` is the bad direction: -1 low
  // values are bad, 1 high ones, 0 a large magnitude (the ionosphere mean
  // carries its own offset per pair, so only its size compares). ``thr`` is the
  // default "bad" threshold and ``lo``/``hi`` a fixed scale for the plot lanes.
  // The RFI likelihood is documented as 0-1 but runs from 0.01 to 1e31 in the
  // archive, so it is coloured on a log scale and "bad" means past 1.
  const QA_FIELDS = {
    cm:  {label:"Coherence median",            lane:"coh. median", dir:-1, thr:0.3, digits:2, lo:0, hi:1},
    ca:  {label:"Coherence mean",              lane:"coh. mean",   dir:-1, thr:0.3, digits:2, lo:0, hi:1},
    v:   {label:"Valid unwrapped (%)",         lane:"valid %",     dir:-1, thr:50,  digits:0, lo:0, hi:100},
    l:   {label:"Largest region (%)",          lane:"largest %",   dir:-1, thr:50,  digits:0, lo:0, hi:100},
    n:   {label:"Connected components",        lane:"# regions",   dir:1,  thr:1,   digits:0},
    im:  {label:"Ionosphere mean (rad)",       lane:"iono mean",   dir:0,  thr:20,  digits:1},
    imd: {label:"Ionosphere median (rad)",     lane:"iono median", dir:0,  thr:20,  digits:1},
    is:  {label:"Ionosphere spread (rad)",     lane:"iono spread", dir:1,  thr:10,  digits:1},
    iu:  {label:"Ionosphere uncertainty (rad)",lane:"iono unc.",   dir:1,  thr:3,   digits:2},
    rl:  {label:"RFI likelihood (log10)",      lane:"RFI likel.",  dir:1,  thr:1,   digits:2, log:true}
  };
  const QA_GUNW = ["cm","ca","v","l","n","im","imd","is","iu","rl"];
  const QA_LANES = {gunw: ["cm","v","l","n","im","is","iu","rl"], gslc: ["rl"]};
  const QA_MISSING = "#3a3f45";
  QA_GUNW.forEach(k=>{
    COLOR_BY_FIELDS[`gunw_qa_${k}`] = {label: QA_FIELDS[k].label, key: `_qa_gunw_${k}`, kind:"num", qa:k, product:"gunw"};
  });
  COLOR_BY_FIELDS.gslc_qa_rl = {label: QA_FIELDS.rl.label, key: "_qa_gslc_rl", kind:"num", qa:"rl", product:"gslc"};
  // These counts change with the chips and the date range; the scope note
  // under the colour-by select is shown only for them.
  const SELECTION_FIELDS = new Set(["gslc_count","n_duplicate","n_modes","gunw_count",
    ...Object.keys(COLOR_BY_FIELDS).filter(k=>COLOR_BY_FIELDS[k].qa)]);

  // Rollout options keep a fixed colour each, earliest first, so the map reads
  // the same whatever subset is shown.
  const ROLLOUT_PALETTE = ["#e5484d","#ff8a4d","#ffd24d","#7ee787","#4dd2c9","#4da3ff","#a389ff","#ff6fc7"];
  const ROLLOUT_OPTIONS = Array.isArray(META.rollout_options) ? META.rollout_options : [];

  const baseColorMapsCache = {};
  function baseColorMap(propKey){
    if (baseColorMapsCache[propKey]) return baseColorMapsCache[propKey];
    let m;
    if (propKey === "passDirection") {
      m = new Map([["Ascending","#4da3ff"],["Descending","#ff8a4d"]]);
    } else if (propKey === "cons_cov") {
      m = new Map([["F","#4da3ff"],["P","#ff8a4d"],["none","#555a61"]]);
    } else if (propKey === "_gunwNet") {
      m = new Map([["connected","#7ee787"],["disconnected","#e5484d"],["no GUNW","#6b6b6b"]]);
    } else if (propKey === "_rollout") {
      m = new Map(ROLLOUT_OPTIONS.map((o,i)=>[o, ROLLOUT_PALETTE[i % ROLLOUT_PALETTE.length]]));
      m.set("none", "#555a61");
    } else if (propKey === "_flag_o") {
      m = orbitColorMap();
    } else if (propKey.startsWith("_flag_")) {
      m = new Map([["all",FLAG_YES],["some","#ffd24d"],["none","#555a61"],["not collected","#2b2b2b"]]);
    } else {
      const vals = uniqSorted(FRAME_DATA.features.map(f=>String(f.properties[propKey])));
      m = new Map();
      vals.forEach((v,i)=> m.set(v, v==="none" ? "#6b6b6b" : CAT_PALETTE[i % CAT_PALETTE.length]));
    }
    baseColorMapsCache[propKey] = m;
    return m;
  }

  // Numeric ramps auto-fit the frames currently shown, so narrowing a filter
  // stretches the ramp over what is left. A viewer built before a field existed
  // still has to render: an all-missing field collapses to a flat colour rather
  // than NaN stops MapLibre rejects.
  let shownFeatures = FRAME_DATA.features;
  function numericStops(propKey){
    const vals = shownFeatures.map(f=>Number(f.properties[propKey])).filter(Number.isFinite);
    if (!vals.length) return {lo:0, hi:0};
    let lo = Infinity, hi = -Infinity;
    vals.forEach(v=>{ if (v < lo) lo = v; if (v > hi) hi = v; });
    return {lo, hi};
  }

  // Per colour-by field: colormap, invert, and a fixed range (null = auto).
  const numStyle = {};
  function styleOf(fieldName){
    if (!numStyle[fieldName]) numStyle[fieldName] = {cmap:DEFAULT_CMAP, invert:false, vmin:null, vmax:null};
    return numStyle[fieldName];
  }
  function activeStops(fieldName){
    const st = styleOf(fieldName);
    const stops = COLORMAPS[st.cmap] || COLORMAPS[DEFAULT_CMAP];
    return st.invert ? [...stops].reverse() : stops;
  }
  function fieldRange(fieldName){
    const st = styleOf(fieldName);
    const auto = numericStops(COLOR_BY_FIELDS[fieldName].key);
    return {
      lo: st.vmin == null ? auto.lo : st.vmin,
      hi: st.vmax == null ? auto.hi : st.vmax,
      fixed: st.vmin != null || st.vmax != null
    };
  }

  function colorExpression(fieldName){
    const info = COLOR_BY_FIELDS[fieldName];
    if (info.kind === "num") {
      const {lo, hi} = fieldRange(fieldName);
      const stops = activeStops(fieldName);
      let expr = stops[Math.floor(stops.length/2)];
      if (hi > lo) {
        expr = ["interpolate", ["linear"], ["to-number", ["get", info.key]]];
        stops.forEach((col,i)=> expr.push(lo + (hi - lo) * (i / (stops.length - 1)), col));
      }
      // A frame none of whose granules has been read has no value, which
      // to-number would paint as the bottom of the ramp.
      return info.qa ? ["case", ["==", ["typeof", ["get", info.key]], "number"], expr, QA_MISSING] : expr;
    }
    const cmap = baseColorMap(info.key);
    const expr = ["match", ["to-string", ["get", info.key]]];
    cmap.forEach((color, val)=>{ expr.push(val, color); });
    expr.push("#9a9a9a");
    return expr;
  }

  function fmtNum(v){ return Number.isInteger(v) ? String(v) : v.toFixed(1); }

  // The colormap panel survives the legend being redrawn on every filter change.
  let cmapPopOpen = false;
  let cmapGroupsOpen = null;   // colormap groups left open, shared by sidebar and panel
  let cbGroupsOpen = null;     // colour-by groups left open, likewise

  // Style controls for the current colour-by field, drawn into ``el``. The
  // sidebar and the map's colour panel both draw them, so every element is
  // found by its role inside ``el`` rather than by a page-wide id. In the
  // sidebar the colormap choices fold under the colorbar (``collapsible``); in
  // the map panel they are always open.
  function renderStyleControls(el, fieldName, collapsible){
    const info = COLOR_BY_FIELDS[fieldName];
    const q = role => el.querySelector(`[data-role="${role}"]`);
    el.innerHTML = "";
    if (info.kind === "num") {
      const st = styleOf(fieldName);
      const {lo, hi, fixed} = fieldRange(fieldName);
      const grad = activeStops(fieldName).join(",");
      const auto = numericStops(info.key);
      // Each group folds and starts closed (its header names the current
      // colormap); whatever the user opens or closes stays so across redraws.
      if (!cmapGroupsOpen) cmapGroupsOpen = new Set();
      const swatches = COLORMAP_GROUPS.map(([title, names])=>
        `<details class="cmap-group" data-group="${title}"${cmapGroupsOpen.has(title) ? " open" : ""}>`+
        `<summary>${title} <span class="cmap-count">${names.length}</span>`+
        `${names.includes(st.cmap) ? ` <span class="cmap-cur">&middot; ${st.cmap}</span>` : ""}</summary>`+
        `<div class="cmap-grid">`+names.map(name=>
          `<button type="button" data-cmap="${name}" class="${name === st.cmap ? "active" : ""}">${name}`+
          `<span class="cmap-swatch" style="background:linear-gradient(90deg,${COLORMAPS[name].join(",")})"></span></button>`).join("")+
        `</div></details>`).join("");
      const open = !collapsible || cmapPopOpen;
      el.innerHTML = qaStatHtml(fieldName) +
        `<div class="${collapsible ? "cmap-bar-click" : ""}" data-role="bar"${collapsible ? ` title="Click to change the colormap and range"` : ""}>`+
        `<div class="cmap-ramp" style="background:linear-gradient(90deg,${grad});"></div>`+
        `<div class="cmap-labels"><span>${fmtNum(lo)}</span><span>${info.label}${fixed ? " (fixed)" : ""}</span><span>${fmtNum(hi)}</span></div></div>`+
        `<div class="cmap-pop" data-role="pop"${open ? "" : " hidden"}>`+
        `<div class="cmap-groups">${swatches}</div>`+
        `<div class="cmap-range">min <input type="number" data-role="vmin" step="any" value="${st.vmin == null ? "" : st.vmin}" placeholder="${fmtNum(auto.lo)}">`+
        `max <input type="number" data-role="vmax" step="any" value="${st.vmax == null ? "" : st.vmax}" placeholder="${fmtNum(auto.hi)}">`+
        `<button class="btn small" data-role="auto" title="Fit the frames shown">Auto</button></div>`+
        `<div class="cmap-range"><label><input type="checkbox" data-role="invert"${st.invert ? " checked" : ""}> invert</label>`+
        `<span style="margin-left:auto">empty range = fit the frames shown</span></div>`+
        `</div>`;
      if (collapsible) q("bar").addEventListener("click", ()=>{
        cmapPopOpen = !cmapPopOpen;
        q("pop").hidden = !cmapPopOpen;
      });
      el.querySelectorAll("[data-cmap]").forEach(b=> b.addEventListener("click", ()=>{
        st.cmap = b.dataset.cmap; applyColorBy();
      }));
      el.querySelectorAll("details.cmap-group").forEach(d=> d.addEventListener("toggle", ()=>{
        if (d.open) cmapGroupsOpen.add(d.dataset.group); else cmapGroupsOpen.delete(d.dataset.group);
      }));
      q("invert").addEventListener("change", e=>{ st.invert = e.target.checked; applyColorBy(); });
      const readBound = role=>{
        const v = q(role).value.trim();
        return v === "" || !Number.isFinite(Number(v)) ? null : Number(v);
      };
      ["vmin","vmax"].forEach(role=> q(role).addEventListener("change", ()=>{
        st.vmin = readBound("vmin"); st.vmax = readBound("vmax"); applyColorBy();
      }));
      q("auto").addEventListener("click", ()=>{ st.vmin = null; st.vmax = null; applyColorBy(); });
      if (info.qa) wireQaStat(el, fieldName);
      return;
    }
    const colorMap = baseColorMap(info.key);
    const grid = document.createElement("div");
    grid.className = "cat-grid";
    colorMap.forEach((color, val)=>{
      const row = document.createElement("label");
      row.className = "cat-row";
      const picker = document.createElement("input");
      picker.type = "color"; picker.className = "legend-color"; picker.value = color;
      picker.title = `Recolour ${val}`;
      // The map follows the picker live; the legends are redrawn only once a
      // colour is settled, so the open picker is not torn down under the cursor.
      picker.addEventListener("input", ()=>{ colorMap.set(val, picker.value); paintFrames(); });
      picker.addEventListener("change", ()=>{ colorMap.set(val, picker.value); applyColorBy(); });
      const label = document.createElement("span");
      label.textContent = val;
      row.appendChild(picker);
      row.appendChild(label);
      grid.appendChild(row);
    });
    el.appendChild(grid);
  }

  // A compact, read-only legend: the ramp with its range, or the categories.
  function legendHtml(fieldName){
    const info = COLOR_BY_FIELDS[fieldName];
    if (info.kind === "num") {
      const {lo, hi} = fieldRange(fieldName);
      return `<div class="cmap-ramp" style="background:linear-gradient(90deg,${activeStops(fieldName).join(",")});"></div>`+
             `<div class="cmap-labels"><span>${fmtNum(lo)}</span><span>${fmtNum(hi)}</span></div>`+
             (info.qa ? `<div class="mini-cats"><span><i style="background:${QA_MISSING}"></i>QA not read</span></div>` : "");
    }
    return `<div class="mini-cats">`+Array.from(baseColorMap(info.key)).map(([v,c])=>
      `<span><i style="background:${c}"></i>${v}</span>`).join("")+`</div>`;
  }

  // A row's preview in the option list: its ramp, or its first few colours.
  function previewHtml(fieldName){
    const info = COLOR_BY_FIELDS[fieldName];
    if (info.kind === "num") return `<span class="cb-prev" style="background:linear-gradient(90deg,${activeStops(fieldName).join(",")})"></span>`;
    return `<span class="cb-prev cb-prev-cat">`+Array.from(baseColorMap(info.key).values()).slice(0,5)
      .map(c=>`<i style="background:${c}"></i>`).join("")+`</span>`;
  }

  // ---------- filter chips ----------
  // Science modes / polarizations the DISP time series is built from: preselected
  // so the map opens on the acquisitions that matter, not the whole archive.
  const DEFAULT_GSLC_MODES = ["2005", "4005"];
  const DEFAULT_GSLC_POLS = ["DHDH", "QPDH"];

  function buildChips(containerId, values, chipSet, defaults){
    const el = document.getElementById(containerId);
    el.innerHTML = "";
    values.forEach(v=>{
      const c = document.createElement("div");
      c.className = "chip"; c.textContent = v;
      if (defaults && defaults.includes(v)) { chipSet.add(v); c.classList.add("active"); }
      c.onclick = ()=>{
        if (chipSet.has(v)) { chipSet.delete(v); c.classList.remove("active"); }
        else { chipSet.add(v); c.classList.add("active"); }
        applyFilters();
      };
      el.appendChild(c);
    });
  }
  // The chip grids show whichever product the map is on; GUNW starts unfiltered.
  function renderChips(){
    const gunw = product === "gunw";
    const modeSet = gunw ? activeChips.gunwMode : activeChips.gslcMode;
    const polSet = gunw ? activeChips.gunwPol : activeChips.gslcPol;
    buildChips("chips-gslc-mode", gunw ? allModesGunw : allModesGslc, modeSet, Array.from(modeSet));
    buildChips("chips-gslc-pol", gunw ? allPolsGunw : allPolsGslc, polSet, Array.from(polSet));
    const cridSet = gunw ? activeChips.gunwCrid : activeChips.gslcCrid;
    buildChips("chips-crid", gunw ? allCridsGunw : allCridsGslc, cridSet, Array.from(cridSet));
    document.getElementById("chips-title").textContent = `${gunw ? "GUNW" : "GSLC"} Mode / Polarization / CRID`;
  }
  function buildGslcChips(){
    Object.values(activeChips).forEach(s=>s.clear());
    DEFAULT_GSLC_MODES.filter(v=>allModesGslc.includes(v)).forEach(v=>activeChips.gslcMode.add(v));
    DEFAULT_GSLC_POLS.filter(v=>allPolsGslc.includes(v)).forEach(v=>activeChips.gslcPol.add(v));
    renderChips();
  }
  buildGslcChips();

  // When CMR (or the bucket scan) was last queried for the catalog behind this
  // page. The refresh workflow builds the catalog in the same run, so this
  // tracks the cron schedule -- or a manual run -- on its own.
  const queriedAt = META.catalog_queried_at || META.generated_at;
  if (queriedAt) {
    // A bucket scan and a CMR query are different sources with different
    // coverage, so the stamp names the one this page was actually built from.
    const source = {cmr:"CMR queried", "bucket-scan":"Bucket scanned"}[META.catalog_kind] || "Catalog built";
    document.getElementById("hdr-queried").textContent =
      `${source}: ${new Date(queriedAt).toISOString().slice(0,16).replace("T"," ")} UTC`;
  }

  // Blackout windows show in the frame popup and as bands in Show plot; the
  // colour option is revealed only when the viewer was built with blackout data.
  // The GSLC / GUNW switch only exists when the viewer was built with a GUNW catalog.
  if (META.has_gunw) {
    document.getElementById("product-ctrl").hidden = false;
    document.getElementById("map").classList.add("has-product");
    document.getElementById("opt-gunw").hidden = false;
    document.getElementById("opt-gunw-net").hidden = false;
  }
  if (META.has_flags) {
    document.querySelectorAll(".opt-flag").forEach(o=>{ o.hidden = false; });
    document.getElementById("chart-flags-ctl").hidden = false;
  }
  if (META.has_qa) {
    // GUNW colourings need the GUNW catalog as well as the QA cache.
    document.querySelectorAll(".opt-qa").forEach(o=>{ o.hidden = o.dataset.product === "gunw" && !META.has_gunw; });
    document.getElementById("chart-qa-ctl").hidden = false;
  }

  if (META.has_blackout) document.getElementById("opt-blackout").hidden = false;

  // ---------- blackout / reference helpers ----------
  function asArray(v){ return typeof v === "string" ? JSON.parse(v) : (v || []); }

  function blackoutHoverLine(p){
    if (!p.has_blackout) return META.has_blackout ? `<div class="pop-row">Blackout: none</div>` : "";
    return `<div class="pop-row">Blackout: <b>${p.blackout_label}</b> `+
           `(${p.blackout_months} mo, ${p.blackout_windows} yr)</div>`;
  }
  function referenceHoverLine(p){
    if (!META.has_reference) return "";
    const refs = asArray(p.reference_dates);
    return `<div class="pop-row">Ref resets: ${refs.length ? refs.join(", ") : "default"}</div>`;
  }
  // The popup keeps blackout to one line and a month strip: each month is shaded
  // by the share its window excludes and counts the frame's acquisitions (GSLC)
  // or pairs (GUNW) in it; tapping a month lists them underneath. The per-year
  // windows, all alike, are in the strip's tooltip rather than a list.
  function blackoutDetailBlock(p){
    const counts = new Array(12).fill(0);
    monthEntries(p).forEach(en=> new Set(en.months).forEach(m=>{ if (m >= 0) counts[m]++; }));
    const what = product === "gunw" ? "pairs" : "acquisitions";
    let head = "By month";
    if (META.has_blackout) head = p.has_blackout ? `Blackout <b>${p.blackout_label}</b> &middot; ~${p._boDays} d/yr` : "Blackout: none";
    let html = `<div class="pop-row" style="margin-top:6px;">${head} <span class="tdim">&middot; tap a month for its ${what}</span></div>`+
      monthStripHtml(p._boShares || new Array(12).fill(0), counts, p.id, asArray(p.blackout_ranges))+
      `<div class="month-list" hidden></div>`;
    if (META.has_reference) {
      const refs = asArray(p.reference_dates);
      html += `<div class="pop-row" style="margin-top:6px;">Reference resets: ${refs.length ? refs.join(", ") : "default (first acquisition)"}</div>`;
    }
    return html;
  }

  // A GSLC acquisition belongs to the month of its date; a GUNW pair to the
  // months of both its dates.
  const monthOf = d => d ? Number(String(d).slice(5, 7)) - 1 : -1;
  function monthEntries(p){
    if (product === "gunw") return asArray(p.gunw_ifgs).map(g=>({months: [monthOf(g.ref), monthOf(g.sec)], g}));
    return parseGranules(p).map(g=>({months: [monthOf(g.date)], g}));
  }

  // ---------- rollout regions ----------
  // Each frame carries the rollout options it belongs to, earliest first; the
  // map colours it by the earliest. An empty set shows every frame.
  const activeRollout = new Set();
  FRAME_DATA.features.forEach(f=>{
    const opts = asArray(f.properties.rollout);
    f.properties._rollout = opts.length ? opts[0] : "none";
  });
  if (ROLLOUT_OPTIONS.length) {
    document.getElementById("sec-rollout-wrap").hidden = false;
    document.getElementById("opt-rollout").hidden = false;
    const src = META.rollout_source || "";
    document.getElementById("rollout-note").textContent = src.endsWith(".geojson")
      ? `NISAR frames tagged with the DISP-S1 rollout options (${src}) whose S1 frames cover at least a quarter of them. A frame can sit in more than one option.`
      : `Rollout options from ${src}.`;
  }

  function rolloutLine(p){
    if (!ROLLOUT_OPTIONS.length) return "";
    const opts = asArray(p.rollout), regions = asArray(p.rollout_regions);
    return `<div class="pop-row">Rollout: ${opts.length ? `<b>${opts.join(", ")}</b>` : "none"}`+
           `${regions.length ? ` &middot; ${regions.join(", ")}` : ""}</div>`;
  }

  function refreshRolloutList(){
    if (!ROLLOUT_OPTIONS.length) return;
    const base = currentFiltered({ignoreRollout:true});
    const cmap = baseColorMap("_rollout");
    const rows = [...ROLLOUT_OPTIONS, "none"].map(opt=>{
      const has = f=> opt === "none" ? !asArray(f.properties.rollout).length
                                     : asArray(f.properties.rollout).includes(opt);
      return {opt, shown: base.filter(has).length, total: FRAME_DATA.features.filter(has).length};
    });
    const maxN = Math.max(1, ...rows.map(r=>r.shown));
    const el = document.getElementById("rollout-list");
    el.innerHTML = rows.map(r=>{
      const off = activeRollout.size && !activeRollout.has(r.opt);
      return `<div class="rollout-row${off ? " off" : ""}" data-opt="${r.opt}" title="Show only ${r.opt} frames (click again to clear)">`+
        `<span class="rl">${r.opt}</span>`+
        `<span class="bar-track"><span class="bar-fill" style="width:${r.shown/maxN*100}%;background:${cmap.get(r.opt)};"></span></span>`+
        `<span class="bn">${r.shown}${r.shown !== r.total ? ` / ${r.total}` : ""}</span></div>`;
    }).join("");
  }
  document.getElementById("rollout-list").addEventListener("click", e=>{
    const row = e.target.closest("[data-opt]");
    if (!row) return;
    const opt = row.dataset.opt;
    if (activeRollout.has(opt)) activeRollout.delete(opt); else activeRollout.add(opt);
    applyFilters();
  });

  // ---------- blackout by calendar month ----------
  // Share of each calendar month a frame's windows black out, averaged over the
  // years they span. Windows wrap the new year (Oct -> May), so each one is
  // walked month by month rather than read from its start and end months.
  function blackoutMonthShares(ranges){
    const covered = new Array(12).fill(0);
    if (!ranges.length) return covered;
    ranges.forEach(r=>{
      const [a, b] = r.split("->").map(x=>x.trim());
      let t = Date.parse(`${a}T00:00:00Z`);
      const t1 = Date.parse(`${b}T00:00:00Z`);
      while (t <= t1) {
        const d = new Date(t);
        const y = d.getUTCFullYear(), m = d.getUTCMonth();
        const next = Date.UTC(y, m + 1, 1);
        const end = Math.min(next - DAY_MS, t1);
        covered[m] += (Math.round((end - t) / DAY_MS) + 1) / Math.round((next - Date.UTC(y, m, 1)) / DAY_MS);
        t = end + DAY_MS;
      }
    });
    return covered.map(c=> Math.min(1, c / ranges.length));
  }

  let boMonth = new Date().getUTCMonth();
  const DAYS_IN_MONTH = [31,28,31,30,31,30,31,31,30,31,30,31];
  let ovMonth = "yr";
  if (META.has_blackout) {
    FRAME_DATA.features.forEach(f=>{
      f.properties._boShares = blackoutMonthShares(asArray(f.properties.blackout_ranges));
      f.properties._bo_sel = Math.round(f.properties._boShares[boMonth] * 100);
      f.properties._boDays = Math.round(f.properties._boShares.reduce((a, sh, i)=> a + sh * DAYS_IN_MONTH[i], 0));
      f.properties._bo_ov = Math.round(f.properties._boDays / 365 * 100);
    });
    document.getElementById("sec-bo-wrap").hidden = false;
    document.getElementById("opt-blackout-month").hidden = false;
    const sel = document.getElementById("bo-month");
    sel.innerHTML = MONTHS.map((m,i)=>`<option value="${i}">${m}</option>`).join("");
    sel.value = String(boMonth);
    sel.addEventListener("change", ()=> setBlackoutMonth(Number(sel.value)));
  }

  // The map overlay keeps its own month (or the whole year, the default), so
  // stepping through it leaves the sidebar's colour-by and month chart alone.
  function overlayShare(p){
    const shares = p._boShares;
    if (!shares) return 0;
    return ovMonth === "yr" ? Math.round(p._boDays / 365 * 100) : Math.round(shares[ovMonth] * 100);
  }
  function setOverlayMonth(m){
    ovMonth = m;
    FRAME_DATA.features.forEach(f=>{ f.properties._bo_ov = overlayShare(f.properties); });
    if (map.getSource("frames")) map.getSource("frames").setData({type:"FeatureCollection", features: shownFeatures});
    refreshSnowPanel();
  }

  function setBlackoutMonth(m){
    boMonth = m;
    document.getElementById("bo-month").value = String(m);
    FRAME_DATA.features.forEach(f=>{
      const shares = f.properties._boShares;
      f.properties._bo_sel = shares ? Math.round(shares[m] * 100) : 0;
    });
    if (map.getSource("frames")) map.getSource("frames").setData({type:"FeatureCollection", features: shownFeatures});
    applyColorBy();
    refreshBlackoutChart(shownFeatures);
  }

  function monthStripHtml(shares, counts, frameId, ranges){
    const tip = ranges && ranges.length ? ` title="Blackout windows:&#10;${ranges.join("&#10;")}"` : "";
    return `<div class="month-strip" data-frame="${frameId}"${tip}>`+MONTHS.map((m,i)=>{
      const a = shares[i];
      const bg = a > 0 ? `background:rgba(140,140,140,${(0.15 + 0.75 * a).toFixed(2)})` : "";
      return `<button type="button" data-m="${i}" style="${bg}"${counts[i] ? "" : ` class="none"`} `+
             `title="${m}: ${Math.round(a * 100)}% blacked out, ${counts[i]} in this month">`+
             `${m[0]}<small>${counts[i]}</small></button>`;
    }).join("")+`</div>`;
  }
  document.addEventListener("click", e=>{
    const cell = e.target.closest ? e.target.closest(".month-strip [data-m]") : null;
    if (!cell) return;
    const strip = cell.parentElement, list = strip.nextElementSibling;
    const was = cell.classList.contains("active");
    strip.querySelectorAll(".active").forEach(c=>c.classList.remove("active"));
    if (was) { list.hidden = true; return; }
    cell.classList.add("active");
    const m = Number(cell.dataset.m);
    const p = idToFeature(strip.dataset.frame).properties;
    const hits = monthEntries(p).filter(en=>en.months.includes(m)).map(en=>en.g);
    const gunw = product === "gunw";
    list.innerHTML = `<div class="pop-row">${MONTHS[m]}: ${hits.length} ${gunw ? "pair(s) with a date in it" : "acquisition(s)"}</div>`+
      (hits.length ? `<div class="granule-list">${gunw ? gunwRowsHtml(hits) : granuleRowsHtml(hits)}</div>` : "");
    list.hidden = false;
  });

  let boBins = [];
  function refreshBlackoutChart(features){
    if (!META.has_blackout) return;
    const el = document.getElementById("bo-chart");
    document.getElementById("bo-tip").hidden = true;
    boBins = MONTHS.map((m,i)=>({m, half:0, any:0}));
    let withBo = 0;
    features.forEach(f=>{
      const shares = f.properties._boShares;
      if (!shares || !f.properties.has_blackout) return;
      withBo++;
      shares.forEach((a,i)=>{ if (a > 0) boBins[i].any++; if (a >= 0.5) boBins[i].half++; });
    });
    const W = Math.max(el.clientWidth || 300, 200), H = 92, padT = 12, padB = 16;
    const maxN = Math.max(1, ...boBins.map(b=>b.any));
    const slot = W / 12, bw = slot - 3;
    const y = n => (n / maxN) * (H - padT - padB);
    const bars = boBins.map((b,i)=>{
      const x = (i * slot + 1.5).toFixed(1), sel = i === boMonth ? " sel" : "";
      return `<rect class="bo-bar-any${sel}" data-i="${i}" x="${x}" y="${(H-padB-y(b.any)).toFixed(1)}" width="${bw.toFixed(1)}" height="${y(b.any).toFixed(1)}" rx="1.5"/>`+
             `<rect class="bo-bar${sel}" data-i="${i}" x="${x}" y="${(H-padB-y(b.half)).toFixed(1)}" width="${bw.toFixed(1)}" height="${y(b.half).toFixed(1)}" rx="1.5"/>`+
             `<text class="day-axis" x="${(i*slot + slot/2).toFixed(1)}" y="${H-4}" text-anchor="middle">${MONTHS[i][0]}</text>`;
    }).join("");
    el.innerHTML =
      `<svg width="100%" height="${H}" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Frames blacked out per month">`+
      `<text class="day-axis" x="0" y="9">peak ${maxN}</text>${bars}`+
      `<line class="day-base" x1="0" x2="${W}" y1="${H-padB}" y2="${H-padB}"/></svg>`+
      `<div class="stat-line">${withBo} of ${features.length} frames shown have a blackout window</div>`;
  }
  document.getElementById("bo-chart").addEventListener("mousemove", e=>{
    const tip = document.getElementById("bo-tip");
    const bar = e.target.closest ? e.target.closest("rect[data-i]") : null;
    if (!bar) { tip.hidden = true; return; }
    const b = boBins[Number(bar.dataset.i)];
    tip.innerHTML = `<b>${b.m}</b>: ${b.half} frames at least half excluded<br><span class="tdim">${b.any} with any part excluded</span>`;
    const wrap = tip.parentElement.getBoundingClientRect();
    tip.hidden = false;
    tip.style.left = `${Math.max(0, Math.min(e.clientX - wrap.left + 10, wrap.width - tip.offsetWidth))}px`;
    tip.style.top = `${e.clientY - wrap.top - 40}px`;
  });
  document.getElementById("bo-chart").addEventListener("mouseleave", ()=>{ document.getElementById("bo-tip").hidden = true; });
  document.getElementById("bo-chart").addEventListener("click", e=>{
    const bar = e.target.closest ? e.target.closest("rect[data-i]") : null;
    if (!bar) return;
    document.getElementById("color-by").value = "blackout_month";
    setBlackoutMonth(Number(bar.dataset.i));
  });

  // ---------- palette ----------
  const paletteEl = document.getElementById("palette");
  function renderPalette(){
    paletteEl.innerHTML = "";
    PALETTE.forEach(col=>{
      const sw = document.createElement("div");
      sw.className = "swatch" + (col===currentColor ? " selected" : "");
      sw.style.background = col;
      sw.onclick = ()=>{ currentColor = col; renderPalette(); };
      paletteEl.appendChild(sw);
    });
    const custom = document.createElement("input");
    custom.type = "color"; custom.id = "custom-color"; custom.value = currentColor;
    custom.oninput = (e)=>{ currentColor = e.target.value; renderPalette(); };
    paletteEl.appendChild(custom);
  }
  renderPalette();

  // ---------- collapsible sections ----------
  document.querySelectorAll(".section-head").forEach(h=>{
    h.addEventListener("click", ()=>{ h.parentElement.classList.toggle("collapsed"); });
  });

  // ---------- track/frame text filters ----------
  function parseIntSet(text){
    text = text.trim();
    if (!text) return null;
    const out = new Set();
    text.split(",").forEach(part=>{
      part = part.trim();
      if (!part) return;
      if (part.includes("-")) {
        const [a,b] = part.split("-").map(s=>parseInt(s.trim(),10));
        if (!isNaN(a) && !isNaN(b)) { for(let i=Math.min(a,b); i<=Math.max(a,b); i++) out.add(i); }
      } else {
        const n = parseInt(part,10);
        if (!isNaN(n)) out.add(n);
      }
    });
    return out;
  }
  // The cycles typed in the Location section, or null for all.
  function cycleFilter(){ return parseIntSet(document.getElementById("f-cycle").value); }
  function inCycles(g, cycles){
    return !cycles || (g.cyc ? g.cyc.some(c=>cycles.has(c)) : cycles.has(g.cycle));
  }
  function matchesArrayFilter(propArr, chipSet){
    if (chipSet.size === 0) return true;
    return propArr.some(v => chipSet.has(v));
  }

  function currentFiltered(opts){
    const ignoreRollout = !!(opts && opts.ignoreRollout);
    const trackSet = parseIntSet(document.getElementById("f-track").value);
    const frameSet = parseIntSet(document.getElementById("f-frame").value);
    const cycleSet = cycleFilter();
    const idFilter = document.getElementById("f-id").value.trim().toLowerCase();
    const passVal = document.querySelector('input[name="pass"]:checked').value;
    const calval = document.getElementById("f-calval").checked;
    const selectedOnly = document.getElementById("f-selected-only").checked;
    return FRAME_DATA.features.filter(f=>{
      const p = f.properties;
      if (trackSet && !trackSet.has(p.track)) return false;
      if (frameSet && !frameSet.has(p.frame)) return false;
      if (cycleSet && !asArray(product === "gunw" ? p.gunw_cycles : p.gslc_cycles).some(c=>cycleSet.has(c))) return false;
      if (idFilter && !p.id.toLowerCase().includes(idFilter) &&
          !String(p.frame_idx).includes(idFilter)) return false;
      if (passVal !== "all" && p.passDirection !== passVal) return false;
      if (calval && !p.isCalVal) return false;
      if (selectedOnly && !selected.has(p.id)) return false;
      if (!ignoreRollout && activeRollout.size) {
        const ro = asArray(p.rollout);
        if (!(ro.length ? ro.some(o=>activeRollout.has(o)) : activeRollout.has("none"))) return false;
      }
      if (product === "gunw") {
        if (!matchesArrayFilter(asArray(p.gunw_modes), activeChips.gunwMode)) return false;
        if (!matchesArrayFilter(asArray(p.gunw_pols), activeChips.gunwPol)) return false;
        if (!matchesArrayFilter(p.gunw_crids || [], activeChips.gunwCrid)) return false;
      } else {
        if (!matchesArrayFilter(p.gslc_modes, activeChips.gslcMode)) return false;
        if (!matchesArrayFilter(p.gslc_pols, activeChips.gslcPol)) return false;
        if (!matchesArrayFilter(p.gslc_crids || [], activeChips.gslcCrid)) return false;
      }
      return true;
    });
  }

  function applyFilters(){
    updateSelectedCounts();
    updateFlagStatus();
    updateQaStats();
    const filtered = currentFiltered();
    shownFeatures = filtered;
    if (map.getSource("frames")) {
      map.getSource("frames").setData({type:"FeatureCollection", features: filtered});
    }
    document.getElementById("hdr-count").textContent = filtered.length;
    applyColorBy();          // the GSLC-count ramp follows the mode/pol chips
    refreshSummary(filtered);
    refreshDailyChart(filtered);
    refreshRolloutList();
    refreshBlackoutChart(filtered);
  }

  // ---------- per-frame counts under the current chips and date range ----------
  // Acquisitions are counted the way ``n_unique`` is on the Python side: one
  // acquisition split into several granules is one entry, so the ramp agrees
  // with the timeline chart and the popup's "Unique acquisitions" row. The
  // duplicates are the granules beyond that, and the distinct modes are the
  // modes left after the filter, so all three answer "of the kind I selected,
  // in the dates I picked" rather than "of any kind, ever".
  const GRANULE_KEYS_BY_FRAME = new Map(
    FRAME_DATA.features.map(f=>{
      const granules = Array.isArray(f.properties.granules) ? f.properties.granules : [];
      return [f.properties.id, granules.map(g=>[g.mode, g.pol, g.date || "", `${g.date}|${g.mode}|${g.cov}`, g.crid || "", g.cycle])];
    })
  );

  function dateRange(){
    return {
      from: document.getElementById("f-date-start").value,
      to: document.getElementById("f-date-end").value
    };
  }

  function selectedGslcStats(rows, modes, pols, from, to, crids, cycles){
    const seen = new Set(), modeSet = new Set();
    let n = 0;
    for (const [mode, pol, date, key, crid, cycle] of rows) {
      if (cycles && !cycles.has(cycle)) continue;
      if (modes.size && !modes.has(mode)) continue;
      if (pols.size && !pols.has(pol)) continue;
      if (crids && crids.size && !crids.has(crid)) continue;
      if (from && date < from) continue;
      if (to && date > to) continue;
      n++;
      seen.add(key);
      modeSet.add(mode);
    }
    return {acq: seen.size, dup: n - seen.size, modes: modeSet.size};
  }

  // In GUNW mode the date range applies to the secondary date, the same date the
  // over-time chart bins interferograms by.
  function selectedGunwCount(ifgs, modes, pols, from, to, crids, cycles){
    return ifgs.filter(g=> inCycles(g, cycles) &&
      (!modes.size || modes.has(g.mode)) && (!pols.size || pols.has(g.pol)) &&
      (!crids || !crids.size || crids.has(g.crid)) &&
      (!from || g.sec >= from) && (!to || g.sec <= to)).length;
  }

  function updateSelectedCounts(){
    const {from, to} = dateRange();
    const cycles = cycleFilter();
    FRAME_DATA.features.forEach(f=>{
      const p = f.properties;
      const st = selectedGslcStats(GRANULE_KEYS_BY_FRAME.get(p.id) || [],
                                   activeChips.gslcMode, activeChips.gslcPol, from, to, activeChips.gslcCrid, cycles);
      p.gslc_count_sel = st.acq;
      p.n_duplicate_sel = st.dup;
      p.n_modes_sel = st.modes;
      p.gunw_count_sel = selectedGunwCount(asArray(p.gunw_ifgs),
                                           activeChips.gunwMode, activeChips.gunwPol, from, to, activeChips.gunwCrid, cycles);
    });
  }
  updateSelectedCounts();

  function selectionRow(p){
    const {from, to} = dateRange();
    const span = from || to ? ` ${from || "start"} to ${to || "end"}` : "";
    const cyc = document.getElementById("f-cycle").value.trim();
    return `<div class="pop-row">Selected modes / pols / CRIDs${cyc ? ` / cycles ${cyc}` : ""}${span}: ${p.gslc_count_sel} acq. &middot; `+
           `${p.n_duplicate_sel} dup. &middot; ${p.n_modes_sel} mode(s)</div>`;
  }

  // ---------- per-granule flags ----------
  // Collected from each product's HDF5 metadata (collect_granule_flags.py); an
  // entry without ``fl`` has not been read yet.
  const FLAG_FIELDS = [
    {k:"j", lane:"joint obs"}, {k:"f", lane:"full frame"}, {k:"o", lane:"orbit"},
    {k:"r", lane:"RFI mitig."}, {k:"m", lane:"mixed mode"}, {k:"d", lane:"dithered"}
  ];
  // Green reads as "flag set" in the plot lanes and the flag colourings; the
  // orbit lane keeps green out of its own palette so the two never meet.
  const FLAG_YES = "#2fbf71";
  const ORBIT_COLORS = {MOE:"#4da3ff", POE:"#4dd2c9", NOE:"#ffd24d", FOE:"#ff5d5d"};

  function orbitColorMap(){
    const seen = new Set();
    FRAME_DATA.features.forEach(f=>[...asArray(f.properties.granules), ...asArray(f.properties.gunw_ifgs)]
      .forEach(g=>{ if (g.fl) seen.add(g.fl.o); }));
    const m = new Map();
    uniqSorted(Array.from(seen)).forEach((v,i)=> m.set(v, ORBIT_COLORS[v] || CAT_PALETTE[(i + 4) % CAT_PALETTE.length]));
    m.set("mixed", "#a389ff");
    m.set("not collected", "#2b2b2b");
    return m;
  }

  // "all" / "some" / "none" of the entries that have flags; orbit type reports
  // its value, or "mixed".
  function flagStatus(items, k){
    const known = items.filter(g=>g.fl);
    if (!known.length) return "not collected";
    if (k === "o") {
      const vals = new Set(known.map(g=>g.fl.o));
      return vals.size === 1 ? Array.from(vals)[0] : "mixed";
    }
    const n = known.filter(g=>g.fl[k]).length;
    return n === known.length ? "all" : (n ? "some" : "none");
  }

  // Follows the mode / polarization chips, as the count ramps do.
  function updateFlagStatus(){
    if (!META.has_flags) return;
    const gunw = product === "gunw";
    const modes = gunw ? activeChips.gunwMode : activeChips.gslcMode;
    const pols = gunw ? activeChips.gunwPol : activeChips.gslcPol;
    FRAME_DATA.features.forEach(f=>{
      const items = asArray(gunw ? f.properties.gunw_ifgs : f.properties.granules)
        .filter(g=>(!modes.size || modes.has(g.mode)) && (!pols.size || pols.has(g.pol)));
      FLAG_FIELDS.forEach(ff=>{ f.properties[`_flag_${ff.k}`] = flagStatus(items, ff.k); });
    });
  }

  function flagLine(entries){
    const e = entries.find(g=>g && g.fl);
    if (!e) return META.has_flags ? `<br><span class="tdim">flags not collected</span>` : "";
    return `<br><span class="tdim">`+FLAG_FIELDS.map(ff=>
      ff.k === "o" ? `orbit ${e.fl.o}` : `${ff.lane} ${e.fl[ff.k] ? "yes" : "no"}`).join(" &middot; ")+`</span>`;
  }

  // One lane per flag under a plot. ``entries`` carry the chart point index, a
  // start time, an optional end time (GUNW pairs) and the flag-carrying entry.
  function flagLanesSvg(entries, xOf, x0, x1, yTop){
    const rowH = 20;
    const orbit = baseColorMap("_flag_o");
    let svg = "";
    FLAG_FIELDS.forEach((ff, r)=>{
      const y = yTop + r * rowH + rowH / 2;
      svg += `<line class="chart-grid" x1="${x0}" x2="${x1}" y1="${y}" y2="${y}" opacity="0.6"/>`+
             `<text class="chart-row-label" x="${x0-10}" y="${y+3.5}" text-anchor="end">${ff.lane}</text>`;
      entries.forEach(en=>{
        const fl = en.g && en.g.fl;
        if (!fl) return;
        const on = ff.k === "o" ? true : Boolean(fl[ff.k]);
        const color = ff.k === "o" ? (orbit.get(fl.o) || "#9a9a9a") : (on ? FLAG_YES : "#6b6b6b");
        const xa = xOf(en.ta).toFixed(1);
        if (en.tb != null) {
          svg += `<line class="chart-ifg" data-i="${en.i}" x1="${xa}" x2="${xOf(en.tb).toFixed(1)}" y1="${y}" y2="${y}"`+
                 ` stroke="${color}" stroke-width="${on ? 4 : 1.5}"/>`;
        } else {
          svg += `<circle class="chart-dot" data-i="${en.i}" cx="${xa}" cy="${y}" r="${on ? 4 : 2}" fill="${color}"/>`;
        }
      });
    });
    const orbitKey = Array.from(orbit).filter(([v])=>entries.some(en=>en.g && en.g.fl && en.g.fl.o === v))
      .map(([v, c])=>`<span style="color:${c}">&#9679;</span> ${v}`).join(" ");
    const legend = `flags: <span style="color:${FLAG_YES}">&#9679;</span> yes &middot; `+
      `<span style="color:#6b6b6b">&middot;</span> no &middot; orbit ${orbitKey}`;
    return {svg, height: FLAG_FIELDS.length * rowH, legend};
  }

  // ---------- QA metrics per frame ----------
  // Every QA colouring summarises the frame's granules (or pairs) left by the
  // chips and the date range, like the counts: their median, their worst
  // (10th / 90th percentile, by the metric's bad direction), or the share past
  // a threshold.
  function qaValue(g, k){
    if (k === "rl" && g.rl != null) return g.rl;
    return g.qa ? g.qa[k] : undefined;
  }
  const qaStats = {};
  function qaStatOf(field){
    if (!qaStats[field]) qaStats[field] = {stat:"median", thr: QA_FIELDS[COLOR_BY_FIELDS[field].qa].thr};
    return qaStats[field];
  }
  function quantile(sorted, q){
    const pos = (sorted.length - 1) * q, i = Math.floor(pos);
    return i + 1 < sorted.length ? sorted[i] + (pos - i) * (sorted[i + 1] - sorted[i]) : sorted[i];
  }
  function qaIsBad(k, v, thr){
    const dir = QA_FIELDS[k].dir;
    return dir < 0 ? v < thr : dir > 0 ? v > thr : Math.abs(v) > thr;
  }
  function qaAggregate(k, values, stat, thr){
    if (!values.length) return undefined;
    if (stat === "bad") return 100 * values.filter(v=>qaIsBad(k, v, thr)).length / values.length;
    const dir = QA_FIELDS[k].dir;
    if (stat === "worst" && dir === 0) return quantile(values.map(Math.abs).sort((a,b)=>a-b), 0.9);
    const sorted = [...values].sort((a,b)=>a-b);
    if (stat === "worst") return quantile(sorted, dir < 0 ? 0.1 : 0.9);
    return quantile(sorted, 0.5);
  }
  function qaEntries(p, gunw){
    const {from, to} = dateRange();
    const modes = gunw ? activeChips.gunwMode : activeChips.gslcMode;
    const pols = gunw ? activeChips.gunwPol : activeChips.gslcPol;
    const crids = gunw ? activeChips.gunwCrid : activeChips.gslcCrid;
    const cycles = cycleFilter();
    return asArray(gunw ? p.gunw_ifgs : p.granules).filter(g=>{
      if (!inCycles(g, cycles)) return false;
      const date = gunw ? g.sec : g.date;
      return (!modes.size || modes.has(g.mode)) && (!pols.size || pols.has(g.pol)) &&
             (!crids || !crids.size || crids.has(g.crid)) && (!from || date >= from) && (!to || date <= to);
    });
  }
  function qaLabel(field){
    const info = COLOR_BY_FIELDS[field], st = qaStatOf(field), f = QA_FIELDS[info.qa];
    if (st.stat === "bad") {
      const op = f.dir < 0 ? "<" : f.dir > 0 ? ">" : "|x| >";
      return `% ${info.product === "gunw" ? "pairs" : "acq."} with ${f.lane} ${op} ${st.thr}`;
    }
    return st.stat === "worst" ? `${f.label}, worst` : f.label;
  }
  function updateQaStats(){
    if (!META.has_qa) return;
    const fields = Object.keys(COLOR_BY_FIELDS).filter(k=>COLOR_BY_FIELDS[k].qa);
    FRAME_DATA.features.forEach(feat=>{
      const p = feat.properties;
      const sel = {gunw: qaEntries(p, true), gslc: qaEntries(p, false)};
      fields.forEach(field=>{
        const info = COLOR_BY_FIELDS[field], st = qaStatOf(field);
        const values = sel[info.product].map(g=>qaValue(g, info.qa)).filter(Number.isFinite);
        let v = qaAggregate(info.qa, values, st.stat, st.thr);
        if (v !== undefined && QA_FIELDS[info.qa].log && st.stat !== "bad") v = Math.log10(Math.max(v, 1e-6));
        // Absent rather than null: MapLibre reads a missing property as null,
        // and the colour expression greys out anything that is not a number.
        if (v === undefined) delete p[info.key]; else p[info.key] = v;
      });
    });
  }
  function qaStatHtml(field){
    const info = COLOR_BY_FIELDS[field];
    if (!info.qa) return "";
    const st = qaStatOf(field), f = QA_FIELDS[info.qa];
    const chip = (v, t, title)=>`<div class="chip${st.stat === v ? " active" : ""}" data-qastat="${v}" title="${title}">${t}</div>`;
    const worst = f.dir === 0 ? "90th percentile of |value|" : f.dir < 0 ? "10th percentile" : "90th percentile";
    return `<div class="qa-stat">per frame:`+
      chip("median", "median", "Median over the frame's granules / pairs left by the filters")+
      chip("worst", "worst", `Worst: ${worst}`)+
      chip("bad", "% bad", "Share past the threshold")+
      (st.stat === "bad" ? `<span>${f.dir < 0 ? "&lt;" : f.dir > 0 ? "&gt;" : "|x| &gt;"}</span>`+
        `<input type="number" step="any" data-role="qa-thr" value="${st.thr}">` : "")+
      `</div>`;
  }
  function wireQaStat(el, field){
    const st = qaStatOf(field), styled = styleOf(field);
    el.querySelectorAll("[data-qastat]").forEach(c=> c.addEventListener("click", ()=>{
      if (st.stat === c.dataset.qastat) return;
      st.stat = c.dataset.qastat;
      // The two scales differ (a metric vs a percentage), so a fixed range
      // set for one would only mislead on the other.
      styled.vmin = null; styled.vmax = null;
      COLOR_BY_FIELDS[field].label = qaLabel(field);
      applyFilters();
    }));
    const thr = el.querySelector('[data-role="qa-thr"]');
    if (thr) thr.addEventListener("change", ()=>{
      const v = Number(thr.value);
      if (thr.value.trim() === "" || !Number.isFinite(v)) return;
      st.thr = v;
      COLOR_BY_FIELDS[field].label = qaLabel(field);
      applyFilters();
    });
  }
  // The ionosphere mean is signed around an arbitrary offset: a diverging
  // ramp centred on the middle of the range reads it best.
  ["gunw_qa_im","gunw_qa_imd"].forEach(f=>{ styleOf(f).cmap = "Vik"; });

  function fmtQa(k, v){
    if (!Number.isFinite(v)) return "-";
    const f = QA_FIELDS[k];
    if (f.log && Math.abs(v) >= 1000) return v.toExponential(1);
    return v.toFixed(f.digits) + (k === "v" || k === "l" ? "%" : "");
  }
  function qaLine(entries){
    if (!META.has_qa) return "";
    const e = entries.find(g=>g && (g.qa || g.rl != null));
    if (!e) return `<br><span class="tdim">QA not read</span>`;
    const q = e.qa || {};
    const parts = [];
    if (q.cm != null) parts.push(`coh. ${fmtQa("cm", q.cm)} (mean ${fmtQa("ca", q.ca)})`);
    if (q.v != null) parts.push(`valid ${fmtQa("v", q.v)} &middot; largest ${fmtQa("l", q.l)} &middot; ${q.n} region${q.n === 1 ? "" : "s"}`);
    if (q.is != null) parts.push(`iono mean ${fmtQa("im", q.im)} / median ${fmtQa("imd", q.imd)} / spread ${fmtQa("is", q.is)} / unc. ${fmtQa("iu", q.iu)} rad`);
    const rl = qaValue(e, "rl");
    if (Number.isFinite(rl)) parts.push(`RFI likelihood ${fmtQa("rl", rl)}${rl > 1 ? " (past the documented 0-1)" : ""}`);
    return `<br><span class="tdim">QA: ${parts.join(" &middot; ") || "no metrics"}</span>`;
  }
  // One line in the frame popup, over the granules / pairs the filters leave.
  function qaSummaryRow(p, gunw){
    if (!META.has_qa) return "";
    const items = qaEntries(p, gunw);
    const read = items.filter(g=>g.qa || g.rl != null);
    if (!items.length) return "";
    const noun = gunw ? "pairs" : "acq.";
    if (!read.length) return `<div class="pop-row"><span class="tdim">QA: none of the ${items.length} ${noun} read yet</span></div>`;
    const med = k=>{
      const vals = read.map(g=>qaValue(g, k)).filter(Number.isFinite);
      return vals.length ? qaAggregate(k, vals, "median") : NaN;
    };
    const parts = [];
    if (gunw) {
      parts.push(`coh. ${fmtQa("cm", med("cm"))}`, `valid ${fmtQa("v", med("v"))}`, `largest ${fmtQa("l", med("l"))}`);
      const multi = read.filter(g=>g.qa && g.qa.n > 1).length;
      parts.push(`${multi} with &gt;1 region`, `iono spread ${fmtQa("is", med("is"))} rad`);
    }
    const rls = read.map(g=>qaValue(g, "rl")).filter(Number.isFinite);
    if (rls.length) parts.push(`RFI ${fmtQa("rl", med("rl"))} (max ${fmtQa("rl", Math.max(...rls))})`);
    return `<div class="pop-row">QA median of ${read.length}/${items.length} ${noun}: ${parts.join(" &middot; ")}</div>`;
  }
  function qaCsvCols(g, keys){ return keys.map(k=>{ const v = qaValue(g, k); return Number.isFinite(v) ? v : ""; }); }

  function rampColor(stops, t){
    const x = Math.max(0, Math.min(1, t)) * (stops.length - 1), i = Math.min(stops.length - 2, Math.floor(x));
    const a = stops[i], b = stops[i + 1], u = x - i;
    const ch = (h, o)=> parseInt(h.slice(o, o + 2), 16);
    const mix = o=> Math.round(ch(a, o) + (ch(b, o) - ch(a, o)) * u).toString(16).padStart(2, "0");
    return `#${mix(1)}${mix(3)}${mix(5)}`;
  }
  // A metric's colour in the plots: bright is good and dark is bad whichever
  // way the metric runs, on its fixed scale or the range of the frame's values.
  function qaScale(k, values){
    const f = QA_FIELDS[k];
    const tf = v=> f.dir === 0 ? Math.abs(v) : f.log ? Math.log10(Math.max(v, 1e-6)) : v;
    const vals = values.filter(Number.isFinite).map(tf);
    let lo = f.lo ?? Math.min(...vals), hi = f.hi ?? Math.max(...vals);
    if (!(hi > lo)) hi = lo + 1;
    return v=>{
      if (!Number.isFinite(v)) return "#2b2b2b";
      const t = (tf(v) - lo) / (hi - lo);
      return rampColor(COLORMAPS.Viridis, f.dir < 0 ? t : 1 - t);
    };
  }
  // One lane per metric under a plot, beside the flag lanes; a red ring or
  // underline marks a value past its default threshold.
  function qaLanesSvg(entries, keys, xOf, x0, x1, yTop){
    const rowH = 20;
    let svg = "";
    keys.forEach((k, r)=>{
      const y = yTop + r * rowH + rowH / 2;
      const color = qaScale(k, entries.map(en=>en.g ? qaValue(en.g, k) : NaN));
      svg += `<line class="chart-grid" x1="${x0}" x2="${x1}" y1="${y}" y2="${y}" opacity="0.6"/>`+
             `<text class="chart-row-label" x="${x0-10}" y="${y+3.5}" text-anchor="end">${QA_FIELDS[k].lane}</text>`;
      entries.forEach(en=>{
        const v = en.g ? qaValue(en.g, k) : NaN;
        if (!Number.isFinite(v)) return;
        const c = color(v), bad = qaIsBad(k, v, QA_FIELDS[k].thr);
        const xa = xOf(en.ta).toFixed(1);
        if (en.tb != null) {
          const xb = xOf(en.tb).toFixed(1);
          if (bad) svg += `<line x1="${xa}" x2="${xb}" y1="${y + 4.5}" y2="${y + 4.5}" stroke="#e5484d" stroke-width="1" opacity="0.8"/>`;
          svg += `<line class="chart-ifg" data-i="${en.i}" x1="${xa}" x2="${xb}" y1="${y}" y2="${y}" stroke="${c}" stroke-width="5"/>`;
        } else {
          svg += `<circle class="chart-dot" data-i="${en.i}" cx="${xa}" cy="${y}" r="4.5" fill="${c}"`+
                 `${bad ? ` stroke="#e5484d" stroke-width="1.5"` : ""}/>`;
        }
      });
    });
    const legend = `QA: worse ${qaRampHtml()} better &middot; <span style="color:#e5484d">&#9644;</span> past threshold`;
    return {svg, height: keys.length * rowH, legend};
  }
  function qaRampHtml(){
    return `<span style="display:inline-block;width:46px;height:8px;vertical-align:middle;border-radius:2px;`+
      `background:linear-gradient(90deg,${COLORMAPS.Viridis.join(",")})"></span>`;
  }
  let showQa = false;
  let qaPairColor = "";

  // ---------- browse and QA images ----------
  // Every product's browse PNG is public and the browser may fetch it, so the
  // card always has one image (a GUNW's unwrapped phase, a GSLC's backscatter).
  // The wrapped phase, coherence, connected components and ionosphere screen
  // are only drawn in the QA report, behind the Earthdata login; the local
  // helper (scripts/qa_browse_server.py) fetches and extracts them on request,
  // and reads the grid corners that place any of them on the map.
  const DATA_HOST = "https://nisar.asf.earthdatacloud.nasa.gov";
  const PRODUCT_COLLECTION = {GSLC:"NISAR_L2_GSLC_PROVISIONAL_V1", GUNW:"NISAR_L2_GUNW_PROVISIONAL_V1"};
  const QA_HELPER_DEFAULT = "http://127.0.0.1:8797";
  // A page the helper served itself carries a marker; it then calls the helper
  // on its own origin, which no browser rule blocks.
  const SERVED_BY_HELPER = Boolean(document.querySelector('meta[name="nisar-qa-helper"]'));
  let qaHelper = SERVED_BY_HELPER ? location.origin : QA_HELPER_DEFAULT;
  try { if (!SERVED_BY_HELPER) qaHelper = localStorage.getItem("nisar-qa-helper") || QA_HELPER_DEFAULT; } catch (e) {}
  function gidKind(gid){ return String(gid).split("_")[3]; }
  function browseUrl(gid, suffix){ return `${DATA_HOST}/BROWSE/${PRODUCT_COLLECTION[gidKind(gid)]}/${gid}/${gid}${suffix}`; }
  function productFileUrl(gid, suffix){ return `${DATA_HOST}/NISAR/${PRODUCT_COLLECTION[gidKind(gid)]}/${gid}/${gid}${suffix}`; }

  // A failed check is retried after a while, so starting the helper later
  // needs no reload.
  let helperCheck = null, helperCheckedAt = 0;
  let helperAuth = null;   // where the helper's Earthdata login comes from: netrc, page or none
  function helperAlive(force){
    if (force || !helperCheck || (Date.now() - helperCheckedAt > 15000 && helperCheck.failed)) {
      helperCheckedAt = Date.now();
      const ctl = new AbortController();
      const timer = setTimeout(()=>ctl.abort(), 1500);
      const check = fetch(`${qaHelper}/health`, {signal: ctl.signal})
        .then(r=> r.ok ? r.json() : null).catch(()=>null)
        .then(j=>{ clearTimeout(timer); helperAuth = j ? (j.auth || "netrc") : null; check.failed = !j; return Boolean(j); });
      helperCheck = check;
    }
    return helperCheck;
  }
  async function helperJson(path){
    const r = await fetch(`${qaHelper}${path}`);
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
    return j;
  }
  // Images are read with fetch and shown from blob URLs: an <img> pointing an
  // https page at http://127.0.0.1 counts as mixed content.
  const helperBlobs = new Map();
  function helperImage(gid, layer, thumb){
    const key = `${gid}/${layer}${thumb ? "?thumb=1" : ""}`;
    if (!helperBlobs.has(key)) {
      helperBlobs.set(key, fetch(`${qaHelper}/qa/${key}`).then(r=>{
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.blob();
      }).then(b=>URL.createObjectURL(b)).catch(e=>{ helperBlobs.delete(key); throw e; }));
    }
    return helperBlobs.get(key);
  }
  // A GSLC browse is greyscale with black outside the swath; on the map that
  // black would hide the frame underneath, so it is made transparent first.
  const transparentBrowse = new Map();
  function mapReadyUrl(gid, url){
    if (gidKind(gid) !== "GSLC") return Promise.resolve(url);
    if (!transparentBrowse.has(url)) transparentBrowse.set(url, new Promise((resolve, reject)=>{
      const img = new Image();
      img.crossOrigin = "anonymous";
      img.onload = ()=>{
        const c = document.createElement("canvas");
        c.width = img.naturalWidth; c.height = img.naturalHeight;
        const ctx = c.getContext("2d");
        ctx.drawImage(img, 0, 0);
        const px = ctx.getImageData(0, 0, c.width, c.height);
        for (let i = 0; i < px.data.length; i += 4) {
          if (px.data[i] === 0 && px.data[i+1] === 0 && px.data[i+2] === 0) px.data[i+3] = 0;
        }
        ctx.putImageData(px, 0, 0);
        c.toBlob(b=> b ? resolve(URL.createObjectURL(b)) : reject(new Error("canvas")), "image/png");
      };
      img.onerror = ()=>reject(new Error("browse image did not load"));
      img.src = url;
    }));
    return transparentBrowse.get(url);
  }

  let browse = null;   // {gid, layers, cur, corners, onMap}
  function browseTitle(gid){
    const parts = String(gid).split("_");
    if (gidKind(gid) === "GUNW") {
      const d = s=> `${s.slice(0,4)}-${s.slice(4,6)}-${s.slice(6,8)}`;
      return `GUNW ${d(parts[11])} &rarr; ${d(parts[13])} &middot; T${parts[5]} F${parts[7]}`;
    }
    return `GSLC ${parts[11].slice(0,4)}-${parts[11].slice(4,6)}-${parts[11].slice(6,8)} &middot; T${parts[5]} F${parts[7]}`;
  }
  function renderBrowseTabs(){
    const tabs = browse.layers.map(l=>
      `<button type="button" data-layer="${l.name}" class="${l.name === browse.cur ? "active" : ""}">${l.label}</button>`).join("");
    document.getElementById("browse-tabs").innerHTML = tabs + (browse.loading ? `<span class="bc-wait">${browse.loading}</span>` : "");
  }
  function setHelperNote(html){ document.getElementById("browse-helper").innerHTML = html; }
  const HELPER_CMD = "python scripts/qa_browse_server.py";
  function helperOffNote(){
    return `Wrapped phase, coherence, connected components and ionosphere need the local QA helper `+
      `(it uses the Earthdata login in ~/.netrc, or the key icon's): <code>${HELPER_CMD}</code>`;
  }
  function openBrowse(gid){
    const kind = gidKind(gid);
    if (!PRODUCT_COLLECTION[kind]) return;
    const card = document.getElementById("browse-card");
    // Above the plot window when opened from it; otherwise under the search
    // results, which drop down over the same corner.
    card.style.zIndex = document.getElementById("chart-modal").hidden ? "" : "25";
    card.hidden = false;
    browse = {gid, cur:"public", corners:null, onMap: document.getElementById("browse-map").checked,
              layers:[{name:"public", label: kind === "GUNW" ? "Unwrapped (browse)" : "Backscatter (browse)",
                       url: browseUrl(gid, "_LATLON.png"), place:"bbox"}]};
    document.getElementById("browse-title").innerHTML = browseTitle(gid);
    document.getElementById("browse-sub").textContent = gid;
    document.getElementById("browse-report").href = productFileUrl(gid, "_QA_REPORT.pdf");
    setHelperNote("");
    browse.loading = "checking for the QA helper...";
    renderBrowseTabs();
    showBrowseLayer("public");
    const mine = browse;
    helperAlive().then(ok=>{
      if (browse !== mine) return;
      if (!ok) { mine.loading = ""; renderBrowseTabs(); setHelperNote(helperOffNote()); syncBrowseMapCtl(); return; }
      helperJson(`/corners/${gid}.json`).then(c=>{
        if (browse !== mine) return;
        mine.corners = c;
        syncBrowseMapCtl();
        if (mine.onMap) placeBrowseOnMap(true);
      }).catch(e=>{ if (browse === mine) setHelperNote(`Could not read the grid corners: ${e.message}`); });
      if (kind !== "GUNW") { mine.loading = ""; renderBrowseTabs(); return; }
      mine.loading = "fetching the QA report...";
      renderBrowseTabs();
      helperJson(`/qa/${gid}/index.json`).then(j=>{
        if (browse !== mine) return;
        mine.loading = j.layers.length ? "" : "the QA report has no images";
        j.layers.forEach(l=> mine.layers.push({name:l.name, label:l.label, place:"quad"}));
        renderBrowseTabs();
      }).catch(e=>{
        if (browse !== mine) return;
        mine.loading = "";
        renderBrowseTabs();
        setHelperNote(`The QA helper could not fetch this report: ${e.message}`+
          (/login/i.test(e.message) ? " &middot; log in with the key icon at the top of the sidebar" : ""));
        refreshEdl();
      });
    });
    syncBrowseMapCtl();
  }
  async function layerUrl(layer){
    return layer.url || helperImage(browse.gid, layer.name, false);
  }
  async function showBrowseLayer(name){
    const layer = browse.layers.find(l=>l.name === name);
    if (!layer) return;
    browse.cur = name;
    renderBrowseTabs();
    const img = document.getElementById("browse-img"), msg = document.getElementById("browse-msg");
    const mine = browse;
    img.hidden = true;
    msg.textContent = "loading...";
    try {
      const url = await layerUrl(layer);
      if (browse !== mine || mine.cur !== name) return;
      img.onload = ()=>{ msg.textContent = ""; img.hidden = false; };
      img.onerror = ()=>{ msg.textContent = "image not available"; };
      img.src = url;
      document.getElementById("browse-full").href = url;
      if (mine.onMap) placeBrowseOnMap(false);
    } catch (e) {
      if (browse === mine) msg.textContent = `could not load: ${e.message}`;
    }
  }
  function browseCoords(layer){
    const c = browse && browse.corners;
    if (!c) return null;
    if (layer.place === "quad") return c.quad;
    const [w, s, e, n] = c.bbox;
    return [[w, n], [e, n], [e, s], [w, s]];
  }
  function syncBrowseMapCtl(){
    const box = document.getElementById("browse-map");
    const ready = Boolean(browse && browse.corners);
    box.disabled = !ready;
    box.parentElement.title = ready ? "Place this image on the map"
      : "Placing an image needs its corners, which the local QA helper reads";
  }
  function clearBrowseMap(){
    if (map.getLayer("browse-img")) map.removeLayer("browse-img");
    if (map.getSource("browse-img")) map.removeSource("browse-img");
  }
  async function placeBrowseOnMap(fit){
    const layer = browse.layers.find(l=>l.name === browse.cur);
    const coords = browseCoords(layer);
    if (!coords) return;
    const mine = browse;
    const url = await mapReadyUrl(mine.gid, await layerUrl(layer));
    if (browse !== mine || !mine.onMap) return;
    const opacity = Number(document.getElementById("browse-opacity").value) / 100;
    const src = map.getSource("browse-img");
    if (src) src.updateImage({url, coordinates: coords});
    else {
      map.addSource("browse-img", {type:"image", url, coordinates: coords});
      // Under the frame outlines, so the frame it belongs to stays visible.
      map.addLayer({id:"browse-img", type:"raster", source:"browse-img",
                    paint:{"raster-opacity": opacity, "raster-fade-duration": 0}}, "frames-outline");
    }
    if (fit) {
      const lons = coords.map(c=>c[0]), lats = coords.map(c=>c[1]);
      map.fitBounds([[Math.min(...lons), Math.min(...lats)], [Math.max(...lons), Math.max(...lats)]],
                    {padding: 80, maxZoom: 9, duration: 600});
    }
  }
  document.getElementById("browse-tabs").addEventListener("click", e=>{
    const b = e.target.closest("[data-layer]");
    if (b && browse) showBrowseLayer(b.dataset.layer);
  });
  document.getElementById("browse-map").addEventListener("change", e=>{
    if (!browse) return;
    browse.onMap = e.target.checked;
    if (browse.onMap) {
      // The plot window would cover the map the image is going onto.
      hideModeTimeline();
      document.getElementById("browse-card").style.zIndex = "";
      placeBrowseOnMap(true);
    } else clearBrowseMap();
  });
  document.getElementById("browse-opacity").addEventListener("input", e=>{
    if (map.getLayer("browse-img")) map.setPaintProperty("browse-img", "raster-opacity", Number(e.target.value) / 100);
  });
  document.getElementById("browse-close").addEventListener("click", ()=>{
    document.getElementById("browse-card").hidden = true;
    clearBrowseMap();
    browse = null;
  });
  // The browse buttons live in popups that are rebuilt on every open.
  document.addEventListener("click", e=>{
    const b = e.target.closest ? e.target.closest("[data-browse]") : null;
    if (b) { e.stopPropagation(); openBrowse(b.dataset.browse); }
  });


  // ---------- Earthdata login panel ----------
  // A web page cannot send an Earthdata login to ASF (its download endpoint
  // refuses the cross-origin request), so the login lives with the local
  // helper: its netrc file, or a username / password handed over here, which
  // goes to 127.0.0.1 only. The dot on the key says whether that is set up.
  const edlBtn = document.getElementById("edl-btn"), edlPop = document.getElementById("edl-pop");
  function edlAction(label, action){
    return `<button type="button" class="btn small" data-edl="${action}">${label}</button>`;
  }
  async function refreshEdl(){
    const ok = await helperAlive(true);
    const status = document.getElementById("edl-status"), form = document.getElementById("edl-form");
    const actions = document.getElementById("edl-actions");
    edlBtn.classList.toggle("on", ok && helperAuth !== "none");
    edlBtn.classList.toggle("warn", ok && helperAuth === "none");
    actions.innerHTML = "";
    if (!ok) {
      status.innerHTML = `<b>Could not reach the QA helper</b> at <code>${qaHelper}</code>. Either it is not `+
        `running on the computer this browser runs on, or the browser blocked this page from reaching it `+
        `(Chrome asks a public site for "local network access").<br><br>`+
        `Start it, then open the viewer <b>it</b> serves, which nothing blocks:<br><code>${HELPER_CMD}</code><br>`+
        `<a href="${QA_HELPER_DEFAULT}/" target="_blank" rel="noopener">${QA_HELPER_DEFAULT}/</a><br><br>`+
        `Helper on another computer (e.g. a server)? Forward the port first: `+
        `<code>ssh -L 8797:127.0.0.1:8797 &lt;server&gt;</code>.`;
      form.hidden = true;
      actions.innerHTML = edlAction("Check again", "check") + edlAction("Helper address", "address");
      edlBtn.title = "QA helper not reachable";
      return;
    }
    if (helperAuth === "netrc") {
      status.innerHTML = `Connected to <code>${qaHelper}</code> &middot; found the Earthdata login in `+
        `<code>~/.netrc</code> on the helper's computer. QA images will load.`;
      form.hidden = true;
      actions.innerHTML = edlAction("Use another login", "show-form");
    } else if (helperAuth === "page") {
      status.innerHTML = `Connected to <code>${qaHelper}</code> &middot; logged in from this page `+
        `(kept by the helper until it stops). QA images will load.`;
      form.hidden = true;
      actions.innerHTML = edlAction("Log out", "logout");
    } else {
      status.innerHTML = `Connected to <code>${qaHelper}</code>, but the helper found <b>no Earthdata login</b>: `+
        `no <code>~/.netrc</code> entry for urs.earthdata.nasa.gov on its computer. Log in here, or add one `+
        `and check again.`;
      form.hidden = false;
      actions.innerHTML = edlAction("Check again", "check");
    }
    edlBtn.title = `QA helper connected (${helperAuth === "none" ? "no login" : `login from ${helperAuth === "page" ? "this page" : "~/.netrc"}`})`;
  }
  function setEdlOpen(open){
    edlPop.hidden = !open;
    edlBtn.setAttribute("aria-expanded", String(open));
    if (open) refreshEdl();
  }
  edlBtn.addEventListener("click", e=>{ e.stopPropagation(); setEdlOpen(edlPop.hidden); });
  document.getElementById("edl-close").addEventListener("click", ()=> setEdlOpen(false));
  document.addEventListener("pointerdown", e=>{
    if (!edlPop.hidden && !edlPop.contains(e.target) && !edlBtn.contains(e.target)) setEdlOpen(false);
  });
  // A new login can turn earlier failures into images.
  function afterLoginChange(){
    helperBlobs.clear();
    refreshEdl();
    if (browse) openBrowse(browse.gid);
  }
  document.getElementById("edl-actions").addEventListener("click", async e=>{
    const b = e.target.closest("[data-edl]");
    if (!b) return;
    if (b.dataset.edl === "show-form") { document.getElementById("edl-form").hidden = false; b.remove(); }
    else if (b.dataset.edl === "check") refreshEdl();
    else if (b.dataset.edl === "address") {
      const next = prompt("QA helper address", qaHelper);
      if (next && /^https?:\/\/[^\s]+$/.test(next.trim())) {
        qaHelper = next.trim().replace(/\/+$/, "");
        try { localStorage.setItem("nisar-qa-helper", qaHelper); } catch (err) {}
        helperBlobs.clear();
        refreshEdl();
      }
    }
    else if (b.dataset.edl === "logout") {
      await fetch(`${qaHelper}/logout`, {method:"POST"}).catch(()=>null);
      afterLoginChange();
    }
  });
  document.getElementById("edl-form").addEventListener("submit", async e=>{
    e.preventDefault();
    const user = document.getElementById("edl-user"), pass = document.getElementById("edl-pass");
    const msg = document.getElementById("edl-msg");
    msg.textContent = "";
    try {
      const r = await fetch(`${qaHelper}/login`, {method:"POST", headers:{"Content-Type":"application/json"},
                                                  body: JSON.stringify({username: user.value.trim(), password: pass.value})});
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
      pass.value = "";
      afterLoginChange();
    } catch (err) {
      msg.textContent = `Could not log in: ${err.message}`;
    }
  });
  refreshEdl();


  // ---------- local search and rebuild ----------
  // Only a page the QA helper served can ask it to search CMR and rebuild the
  // viewer for another scope; the published page has no server behind it.
  if (META.view_label) {
    document.getElementById("hdr-scope").textContent =
      `${META.view_label}${META.view_bbox ? ` [${META.view_bbox.map(v=>v.toFixed(1)).join(", ")}]` : ""}`;
  }
  const srchBtn = document.getElementById("srch-btn"), srchPop = document.getElementById("srch-pop");
  srchBtn.hidden = !SERVED_BY_HELPER;
  function viewBbox(){
    const b = map.getBounds();
    const clamp = (v, lo, hi)=> Math.max(lo, Math.min(hi, v));
    return [clamp(b.getWest(), -180, 180), clamp(b.getSouth(), -90, 90),
            clamp(b.getEast(), -180, 180), clamp(b.getNorth(), -90, 90)].map(v=>Number(v.toFixed(3)));
  }
  function setSrchOpen(open){
    srchPop.hidden = !open;
    srchBtn.classList.toggle("armed", open);
    srchBtn.setAttribute("aria-expanded", String(open));
    if (open) {
      const [w, s, e, n] = viewBbox();
      document.getElementById("srch-bbox").textContent = `the frames in the map's current view: ${w}, ${s} to ${e}, ${n}`;
    }
  }
  const buildBox = document.getElementById("build-status");
  function showBuild(text, error){
    buildBox.hidden = false;
    buildBox.classList.toggle("err", Boolean(error));
    document.getElementById("build-text").textContent = text;
    document.getElementById("build-x").hidden = !error;
  }
  let buildPoll = null;
  function pollBuild(){
    clearTimeout(buildPoll);
    fetch(`${qaHelper}/build`).then(r=>r.json()).then(j=>{
      const secs = j.started ? Math.round(Date.now() / 1000 - j.started) : 0;
      if (j.state === "running") {
        showBuild(`${j.step}... (${secs} s)`);
        buildPoll = setTimeout(pollBuild, 1500);
      } else if (j.state === "done") {
        showBuild(`Done: ${j.n_frames} frames. Opening the new view`+
          `${j.n_frames > 5000 ? " (a page this size takes 10-20 s to draw)" : ""}...`);
        location.href = j.view;
      } else if (j.state === "error") {
        showBuild(`Search failed: ${j.step}`, true);
      }
    }).catch(()=> showBuild("Lost the QA helper while searching.", true));
  }
  async function startBuild(){
    const scope = document.querySelector('input[name="srch-scope"]:checked').value;
    setSrchOpen(false);
    showBuild("Starting the search...");
    try {
      const r = await fetch(`${qaHelper}/build`, {method:"POST", headers:{"Content-Type":"application/json"},
                                                  body: JSON.stringify({scope, bbox: scope === "bbox" ? viewBbox() : null})});
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
      pollBuild();
    } catch (err) {
      showBuild(`Search failed: ${err.message}`, true);
    }
  }
  // First click opens the choices; a second click on the magnifier starts.
  srchBtn.addEventListener("click", e=>{
    e.stopPropagation();
    if (srchPop.hidden) setSrchOpen(true); else startBuild();
  });
  document.getElementById("srch-go").addEventListener("click", startBuild);
  document.getElementById("srch-close").addEventListener("click", ()=> setSrchOpen(false));
  document.getElementById("build-x").addEventListener("click", ()=>{ buildBox.hidden = true; });
  document.addEventListener("pointerdown", e=>{
    if (!srchPop.hidden && !srchPop.contains(e.target) && !srchBtn.contains(e.target)) setSrchOpen(false);
  });
  // A search started before a reload, or in another tab, shows here too.
  if (SERVED_BY_HELPER) fetch(`${qaHelper}/build`).then(r=>r.json()).then(j=>{ if (j.state === "running") pollBuild(); }).catch(()=>{});

  // ---------- browse strip in the frame popup ----------
  // One thumbnail per granule / pair left by the filters, newest last; the
  // public browse thumbnails need nothing, the QA layers the helper. Helper
  // thumbnails load as they scroll into view, a few at a time.
  const STRIP_LAYERS = [["public","browse"],["wrapped","wrapped phase"],["coherence","coherence"],
                        ["cc","conn. comp."],["iono","ionosphere"]];
  let stripLayer = "public";
  let stripQueue = [], stripActive = 0;
  function pumpStrip(){
    while (stripActive < 3 && stripQueue.length) {
      const img = stripQueue.shift();
      if (!img.isConnected) continue;
      stripActive++;
      helperImage(img.dataset.gid, img.dataset.layer, true)
        .then(url=>{ img.src = url; })
        .catch(()=>{ img.alt = "n/a"; })
        .finally(()=>{ stripActive--; pumpStrip(); });
    }
  }
  const stripObserver = "IntersectionObserver" in window ? new IntersectionObserver(entries=>{
    entries.forEach(en=>{
      if (!en.isIntersecting) return;
      stripObserver.unobserve(en.target);
      stripQueue.push(en.target);
    });
    pumpStrip();
  }) : null;
  function renderStrip(panel, p, gunw){
    const items = qaEntries(p, gunw).slice().sort((a,b)=> gunw ? (a.ref.localeCompare(b.ref) || a.sec.localeCompare(b.sec)) : a.date.localeCompare(b.date));
    const k = gunw ? (qaPairColor || "cm") : "rl";
    const color = META.has_qa ? qaScale(k, items.map(g=>qaValue(g, k))) : null;
    const helperLayers = gunw ? STRIP_LAYERS : STRIP_LAYERS.slice(0, 1);
    panel.innerHTML =
      `<div class="strip-ctl">${items.length} ${gunw ? "pairs" : "acquisitions"} (filters apply) &middot; show `+
      `<select data-role="strip-layer">${helperLayers.map(([v, t])=>`<option value="${v}"${v === stripLayer ? " selected" : ""}>${t}</option>`).join("")}</select>`+
      (color ? `<span>border: ${QA_FIELDS[k].lane}</span>` : "")+`</div>`+
      `<div class="strip">${items.map(g=>{
        const v = META.has_qa ? qaValue(g, k) : NaN;
        const border = color && Number.isFinite(v) ? ` style="border-color:${color(v)}"` : "";
        const label = gunw ? `${g.ref.slice(2)}<br>${g.sec.slice(2)}` : `${g.date}<br>${g.mode} ${g.pol}`;
        return `<button type="button" class="strip-th" data-browse="${g.gid}"${border} title="${Number.isFinite(v) ? `${QA_FIELDS[k].lane} ${fmtQa(k, v)}` : g.gid}">`+
               `<img alt="" data-gid="${g.gid}">${label}</button>`;
      }).join("") || `<span class="tdim">Nothing left by the filters.</span>`}</div>`+
      `<div class="stat-line" data-role="strip-note"></div>`;
    const layer = stripLayer === "public" || !gunw ? "public" : stripLayer;
    const imgs = Array.from(panel.querySelectorAll(".strip img"));
    if (layer === "public") {
      imgs.forEach(img=>{ img.loading = "lazy"; img.src = browseUrl(img.dataset.gid, "_LATLON_thumbnail.png"); });
    } else {
      helperAlive().then(ok=>{
        if (!ok) { panel.querySelector('[data-role="strip-note"]').innerHTML = helperOffNote(); return; }
        imgs.forEach(img=>{
          img.dataset.layer = layer;
          if (stripObserver) stripObserver.observe(img); else stripQueue.push(img);
        });
        pumpStrip();
      });
    }
    panel.querySelector('[data-role="strip-layer"]').addEventListener("change", e=>{
      stripLayer = e.target.value;
      renderStrip(panel, p, gunw);
    });
  }
  function wireStrip(p, gunw){
    const btn = document.getElementById("pop-strip"), panel = document.getElementById("pop-strip-panel");
    if (!btn || !panel) return;
    btn.addEventListener("click", ()=>{
      panel.hidden = !panel.hidden;
      btn.textContent = panel.hidden ? "Browse images" : "Hide images";
      if (!panel.hidden) renderStrip(panel, p, gunw);
    });
  }
  let showFlags = false;
  let redrawChart = null;

  // Charts are drawn to the card's width, so dragging the card's corner or
  // expanding it redraws them to fit. Before the card has a size (it is still
  // hidden on first draw) the old window-based width stands in.
  function chartWidth(){
    const w = document.getElementById("chart-body").clientWidth;
    return w > 0 ? Math.max(340, Math.floor(w)) : Math.min(720, Math.max(420, window.innerWidth - 140));
  }
  // Vertical room for a plot: only once the card has been given a height.
  function chartRoom(){
    const card = document.querySelector(".chart-card");
    if (!card.style.height && !card.classList.contains("big")) return 0;
    const head = card.querySelector(".chart-head").offsetHeight;
    return card.clientHeight - head - 70;
  }

  // Judged on all of a frame's interferograms, as the GUNW plot is, so a frame
  // coloured disconnected is one whose plot says so.
  function gunwNetworkStatus(ifgs){
    const pairs = ifgs.map(g=>({ta: Date.parse(`${g.ref}T00:00:00Z`), tb: Date.parse(`${g.sec}T00:00:00Z`)}))
      .filter(pt=>isFinite(pt.ta) && isFinite(pt.tb));
    if (!pairs.length) return "no GUNW";
    return gunwNetwork(pairs).components > 1 ? "disconnected" : "connected";
  }
  FRAME_DATA.features.forEach(f=>{ f.properties._gunwNet = gunwNetworkStatus(asArray(f.properties.gunw_ifgs)); });

  // ---------- GSLC acquisitions over time ----------
  // Per-frame date histograms are built once: the chart is redrawn on every
  // filter keystroke, and re-walking 60k+ granules each time is not free.
  const DAY_COUNTS_BY_FRAME = new Map(
    FRAME_DATA.features.map(f=>{
      const per = new Map();
      const granules = Array.isArray(f.properties.granules) ? f.properties.granules : [];
      granules.forEach(g=>{ if (g.date) per.set(g.date, (per.get(g.date)||0)+1); });
      return [f.properties.id, Array.from(per.entries())];
    })
  );

  // In GUNW mode the chart counts interferograms by secondary date, the day a
  // pair became possible.
  const GUNW_DAY_COUNTS_BY_FRAME = new Map(
    FRAME_DATA.features.map(f=>{
      const per = new Map();
      asArray(f.properties.gunw_ifgs).forEach(g=>{ if (g.sec) per.set(g.sec, (per.get(g.sec)||0)+1); });
      return [f.properties.id, Array.from(per.entries())];
    })
  );

  const DAILY_TEXT = {
    gslc: {title:"GSLC Acquisitions Over Time", unit:"granule", empty:"GSLC acquisitions",
           note:"GSLC granules in CMR over North America, counted by acquisition date across the frames currently shown. Hover a bar for its count."},
    gunw: {title:"GUNW Interferograms Over Time", unit:"interferogram", empty:"GUNW interferograms",
           note:"GUNW interferograms in the catalog, counted by secondary date across the frames currently shown. Hover a bar for its count."},
  };

  const BIN_LABEL = {1:"per day", 7:"per week", 30:"per month"};
  let dailyBins = [];

  function refreshDailyChart(features){
    const el = document.getElementById("daily-chart");
    const tip = document.getElementById("daily-tip");
    tip.hidden = true;

    const from = document.getElementById("f-date-start").value;
    const to = document.getElementById("f-date-end").value;
    const counts = new Map();
    const text = DAILY_TEXT[product];
    const byFrame = product === "gunw" ? GUNW_DAY_COUNTS_BY_FRAME : DAY_COUNTS_BY_FRAME;
    document.getElementById("daily-title").textContent = text.title;
    document.getElementById("daily-note").textContent = text.note;
    features.forEach(f=> (byFrame.get(f.properties.id)||[]).forEach(([d,n])=>{
      if (from && d < from) return;
      if (to && d > to) return;
      counts.set(d, (counts.get(d)||0) + n);
    }));
    const days = Array.from(counts.keys()).sort();
    dailyBins = [];
    if (!days.length) {
      el.innerHTML = `<div class="stat-line">No ${text.empty} in the frames shown${from || to ? " for this date range" : ""}.</div>`;
      return;
    }

    const t0 = Date.parse(`${days[0]}T00:00:00Z`);
    const t1 = Date.parse(`${days[days.length-1]}T00:00:00Z`);
    const spanDays = Math.round((t1 - t0) / DAY_MS) + 1;
    // Keep bars wide enough to hit: a daily bar over a multi-year archive is
    // narrower than a pixel in a 300px sidebar.
    const binDays = spanDays > 900 ? 30 : spanDays > 220 ? 7 : 1;
    const nBins = Math.ceil(spanDays / binDays);
    dailyBins = Array.from({length:nBins}, (_,i)=>({t: t0 + i*binDays*DAY_MS, n: 0, binDays}));
    let total = 0;
    counts.forEach((n, d)=>{
      const i = Math.floor((Date.parse(`${d}T00:00:00Z`) - t0) / (binDays * DAY_MS));
      dailyBins[i].n += n;
      total += n;
    });

    const W = Math.max(el.clientWidth || 300, 200);
    const H = 86, padT = 12, padB = 16;
    const maxN = Math.max(...dailyBins.map(b=>b.n));
    const slot = W / nBins;
    const bw = Math.max(1, slot - (slot > 4 ? 1 : 0));
    const bars = dailyBins.map((b,i)=>{
      if (!b.n) return "";
      const h = Math.max(1.5, (b.n / maxN) * (H - padT - padB));
      return `<rect class="day-bar" data-i="${i}" x="${(i*slot).toFixed(2)}" y="${(H-padB-h).toFixed(2)}" `+
             `width="${bw.toFixed(2)}" height="${h.toFixed(2)}" rx="${bw > 3 ? 1.5 : 0}"/>`;
    }).join("");
    const fmt = t => new Date(t).toISOString().slice(0,10);

    el.innerHTML =
      `<svg width="100%" height="${H}" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img"`+
      ` aria-label="${text.empty} ${BIN_LABEL[binDays]}">`+
      `<text class="day-axis" x="0" y="9">peak ${maxN}</text>`+
      `${bars}`+
      `<line class="day-base" x1="0" x2="${W}" y1="${H-padB}" y2="${H-padB}"/>`+
      `<text class="day-axis" x="0" y="${H-4}">${fmt(t0)}</text>`+
      `<text class="day-axis" x="${W}" y="${H-4}" text-anchor="end">${fmt(t1)}</text>`+
      `</svg>`+
      `<div class="stat-line">${total.toLocaleString()} ${text.unit}s &middot; counted ${BIN_LABEL[binDays]}</div>`;
  }

  document.getElementById("daily-chart").addEventListener("mousemove", (e)=>{
    const tip = document.getElementById("daily-tip");
    const bar = e.target.closest ? e.target.closest("rect[data-i]") : null;
    if (!bar) { tip.hidden = true; return; }
    const b = dailyBins[Number(bar.dataset.i)];
    const start = new Date(b.t).toISOString().slice(0,10);
    const end = new Date(b.t + (b.binDays-1)*DAY_MS).toISOString().slice(0,10);
    tip.innerHTML = `<b>${b.n}</b> ${DAILY_TEXT[product].unit}${b.n === 1 ? "" : "s"}<br>`+
                    `<span class="tdim">${b.binDays === 1 ? start : `${start} to ${end}`}</span>`;
    const wrap = tip.parentElement.getBoundingClientRect();
    tip.hidden = false;
    tip.style.left = `${Math.max(0, Math.min(e.clientX - wrap.left + 10, wrap.width - tip.offsetWidth))}px`;
    tip.style.top = `${e.clientY - wrap.top - 34}px`;
  });
  document.getElementById("daily-chart").addEventListener("mouseleave", ()=>{
    document.getElementById("daily-tip").hidden = true;
  });

  ["f-date-start","f-date-end"].forEach(id=>
    document.getElementById(id).addEventListener("change", applyFilters));
  document.getElementById("btn-date-reset").addEventListener("click", ()=>{
    document.getElementById("f-date-start").value = "";
    document.getElementById("f-date-end").value = "";
    applyFilters();
  });

  // Drag across the bars to set the date range. The chart then redraws over
  // just that range, so a second drag narrows it further and "All" goes back.
  let brush = null;
  function binAt(e){
    const svg = document.querySelector("#daily-chart svg");
    if (!svg || !dailyBins.length) return null;
    const r = svg.getBoundingClientRect();
    const frac = Math.min(Math.max((e.clientX - r.left) / r.width, 0), 0.999999);
    return Math.floor(frac * dailyBins.length);
  }
  function drawBrush(i0, i1){
    const svg = document.querySelector("#daily-chart svg");
    let rect = svg.querySelector(".day-brush");
    if (!rect) {
      rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      rect.setAttribute("class", "day-brush");
      svg.appendChild(rect);
    }
    const W = svg.viewBox.baseVal.width, H = svg.viewBox.baseVal.height;
    const slot = W / dailyBins.length;
    const a = Math.min(i0, i1), b = Math.max(i0, i1);
    rect.setAttribute("x", a * slot); rect.setAttribute("width", (b - a + 1) * slot);
    rect.setAttribute("y", 0); rect.setAttribute("height", H - 16);
  }
  document.getElementById("daily-chart").addEventListener("pointerdown", e=>{
    const i = binAt(e);
    if (i == null) return;
    e.preventDefault();
    brush = {i0: i, i1: i};
  });
  window.addEventListener("pointermove", e=>{
    if (!brush) return;
    const i = binAt(e);
    if (i == null) return;
    brush.i1 = i;
    drawBrush(brush.i0, brush.i1);
  });
  window.addEventListener("pointerup", ()=>{
    if (!brush) return;
    const {i0, i1} = brush;
    brush = null;
    if (i0 === i1) { const r = document.querySelector("#daily-chart .day-brush"); if (r) r.remove(); return; }
    const a = dailyBins[Math.min(i0, i1)], b = dailyBins[Math.max(i0, i1)];
    const iso = t => new Date(t).toISOString().slice(0,10);
    document.getElementById("f-date-start").value = iso(a.t);
    document.getElementById("f-date-end").value = iso(b.t + (b.binDays - 1) * DAY_MS);
    applyFilters();
  });

  ["f-track","f-frame","f-cycle","f-id"].forEach(id=>document.getElementById(id).addEventListener("input", applyFilters));
  document.querySelectorAll('input[name="pass"]').forEach(r=>r.addEventListener("change", applyFilters));
  ["f-calval","f-selected-only"].forEach(id=>document.getElementById(id).addEventListener("change", applyFilters));

  document.getElementById("btn-clear-filters").addEventListener("click", ()=>{
    document.getElementById("f-track").value = "";
    document.getElementById("f-frame").value = "";
    document.getElementById("f-cycle").value = "";
    document.getElementById("f-id").value = "";
    document.querySelector('input[name="pass"][value="all"]').checked = true;
    document.getElementById("f-calval").checked = false;
    document.getElementById("f-selected-only").checked = false;
    document.getElementById("f-date-start").value = "";
    document.getElementById("f-date-end").value = "";
    activeRollout.clear();
    buildGslcChips();
    applyFilters();
  });

  // ---------- consistent-mode summary ----------
  refreshSummary = function(features){
    const el = document.getElementById("cons-summary");
    const n = features.length;
    if (!n) { el.innerHTML = `<div class="stat-line">No frames shown.</div>`; return; }
    let full=0, partial=0, mixed=0, withGslc=0;
    const modeCounts = {};
    features.forEach(f=>{
      const p = f.properties;
      if (p.gslc_count > 0) withGslc++;
      if (p.cons_cov === "F") full++;
      else if (p.cons_cov === "P") partial++;
      if (p.n_modes > 1) mixed++;
      const key = (p.cons_mode && p.cons_mode !== "none") ? p.cons_mode : "none";
      modeCounts[key] = (modeCounts[key] || 0) + 1;
    });
    const modeEntries = Object.entries(modeCounts).sort((a,b)=>b[1]-a[1]);
    const maxN = Math.max(...modeEntries.map(e=>e[1]));
    const cmap = baseColorMap("cons_mode");

    let html = `<div class="summary-grid">
      <div class="stat-tile"><div class="num">${n}</div><div class="cap">frames</div></div>
      <div class="stat-tile"><div class="num">${modeEntries.filter(e=>e[0]!=="none").length}</div><div class="cap">modes</div></div>
      <div class="stat-tile"><div class="num">${withGslc}</div><div class="cap">with GSLC</div></div>
    </div>
    <div class="summary-grid">
      <div class="stat-tile"><div class="num">${full}</div><div class="cap">full frame</div></div>
      <div class="stat-tile"><div class="num">${partial}</div><div class="cap">partial</div></div>
      <div class="stat-tile"><div class="num">${mixed}</div><div class="cap">multi-mode</div></div>
    </div>
    <label style="margin-top:8px;">Frames per consistent mode</label>`;
    modeEntries.forEach(([mode,cnt])=>{
      const col = cmap.get(mode) || "#9a9a9a";
      const pct = maxN ? (cnt/maxN*100) : 0;
      html += `<div class="bar-row"><span class="bl">${mode}</span>`+
              `<span class="bar-track"><span class="bar-fill" style="width:${pct}%;background:${col};"></span></span>`+
              `<span class="bn">${cnt}</span></div>`;
    });
    el.innerHTML = html;
  };

  // ---------- selection list ----------
  function idToFeature(id){ return FRAME_DATA.features.find(f=>f.properties.id===id); }

  function renderSelectedList(){
    const ul = document.getElementById("selected-list");
    ul.innerHTML = "";
    document.getElementById("count-badge").textContent = selected.size;
    document.getElementById("empty-sel-hint").style.display = selected.size ? "none" : "block";
    Array.from(selected.values()).forEach(entry=>{
      const p = entry.feature.properties;
      const li = document.createElement("li");
      const sw = document.createElement("div");
      sw.className = "li-swatch"; sw.style.background = entry.color;
      sw.title = "Repaint with current color";
      sw.onclick = ()=>{ entry.color = currentColor; refreshSelectedSource(); renderSelectedList(); };
      const lbl = document.createElement("div");
      lbl.className = "li-label";
      lbl.innerHTML = `${p.frame_idx} <span class="sub">T${p.track}_F${p.frame} &middot; ${p.passDirection[0]} &middot; ${p.cons_mode}${p.cons_cov!=="none"?"_"+p.cons_cov:""}</span>`;
      lbl.title = "Click to zoom to frame";
      lbl.onclick = ()=> zoomToFeature(entry.feature);
      const x = document.createElement("button");
      x.className = "li-x"; x.textContent = "✕";
      x.title = "Remove from selection";
      x.onclick = ()=>{ selected.delete(p.id); selectionChanged(); };
      li.appendChild(sw); li.appendChild(lbl); li.appendChild(x);
      ul.appendChild(li);
    });
  }

  function refreshSelectedSource(){
    const feats = Array.from(selected.values()).map(e=>{
      const clone = JSON.parse(JSON.stringify(e.feature));
      clone.properties.__color = e.color;
      return clone;
    });
    if (map.getSource("selected")) {
      map.getSource("selected").setData({type:"FeatureCollection", features: feats});
    }
  }

  // With "Show only selected frames" on, the selection is itself a filter.
  function selectionChanged(){
    refreshSelectedSource(); renderSelectedList();
    if (document.getElementById("f-selected-only").checked) applyFilters();
  }

  function zoomToFeature(feature){
    const coords = [];
    const geom = feature.geometry;
    const rings = geom.type === "MultiPolygon" ? geom.coordinates.flat() : geom.coordinates;
    rings.forEach(ring=>ring.forEach(c=>coords.push(c)));
    const lons = coords.map(c=>c[0]), lats = coords.map(c=>c[1]);
    map.fitBounds([[Math.min(...lons), Math.min(...lats)],[Math.max(...lons), Math.max(...lats)]], {padding:60, duration:600});
  }

  function toggleSelectFrame(feature){
    const id = feature.properties.id;
    if (selected.has(id)) selected.delete(id);
    else selected.set(id, {feature, color: currentColor});
    selectionChanged();
  }
  function selectFrame(feature, color){
    selected.set(feature.properties.id, {feature, color: color || currentColor});
  }

  document.getElementById("btn-clear-sel").addEventListener("click", ()=>{
    selected.clear(); selectionChanged();
  });

  // ---------- import selection (CSV / GeoJSON / consistent-GSLC JSON) ----------
  function featureByTrackFrame(track, frame){
    return FRAME_DATA.features.find(f=>f.properties.track===track && f.properties.frame===frame);
  }
  function featureByIdx(idx){
    return FRAME_DATA.features.find(f=>f.properties.frame_idx===idx);
  }

  function importCsv(text){
    const lines = text.split(/\r?\n/).filter(l=>l.trim().length);
    if (!lines.length) return 0;
    const header = lines[0].split(",").map(s=>s.trim().replace(/^"|"$/g,"").toLowerCase());
    const ti = header.indexOf("track"), fi = header.indexOf("frame"), ci = header.indexOf("color");
    if (ti < 0 || fi < 0) throw new Error("CSV needs 'track' and 'frame' columns");
    let added = 0;
    for (let i=1; i<lines.length; i++){
      const cells = lines[i].split(",").map(s=>s.trim().replace(/^"|"$/g,""));
      const feat = featureByTrackFrame(parseInt(cells[ti],10), parseInt(cells[fi],10));
      if (feat){ selectFrame(feat, ci>=0 && cells[ci] ? cells[ci] : currentColor); added++; }
    }
    return added;
  }

  function importGeojson(obj){
    let added = 0;
    (obj.features || []).forEach(f=>{
      const p = f.properties || {};
      let feat = null;
      if (p.track !== undefined && p.frame !== undefined) feat = featureByTrackFrame(Number(p.track), Number(p.frame));
      else if (p.id) feat = idToFeature(String(p.id));
      if (feat){ selectFrame(feat, p.color || currentColor); added++; }
    });
    return added;
  }

  function importConsistent(obj){
    // consistent-GSLC catalog: { data: { "<frame_idx>": {...} }, metadata: {...} }
    const data = obj.data || obj;
    let added = 0;
    Object.keys(data).forEach(k=>{
      const feat = featureByIdx(parseInt(k,10));
      if (feat){ selectFrame(feat, currentColor); added++; }
    });
    return added;
  }

  document.getElementById("import-file").addEventListener("change", (e)=>{
    const file = e.target.files[0];
    if (!file) return;
    const status = document.getElementById("import-status");
    const reader = new FileReader();
    reader.onload = ()=>{
      let added = 0;
      try {
        const text = reader.result;
        if (/\.csv$/i.test(file.name)) {
          added = importCsv(text);
        } else {
          const obj = JSON.parse(text);
          if (obj.type === "FeatureCollection") added = importGeojson(obj);
          else added = importConsistent(obj);   // consistent-GSLC catalog
        }
        selectionChanged();
        status.textContent = `Imported ${added} frame(s) from ${file.name}.`;
      } catch (err) {
        status.textContent = "Import failed: " + err.message;
      }
    };
    reader.readAsText(file);
    e.target.value = "";
  });

  // ---------- export ----------
  function toCsv(rows){
    return rows.map(r=>r.map(v=>`"${String(v).replace(/"/g,'""')}"`).join(",")).join("\n");
  }
  function downloadBlob(content, filename, mime){
    const blob = new Blob([content], {type:mime});
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = filename;
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    URL.revokeObjectURL(a.href);
  }
  document.getElementById("btn-export-csv").addEventListener("click", ()=>{
    const rows = [["frame_id","track","frame","passDirection","color","gslc_count","n_unique","n_duplicate","cons_mode","cons_cov","n_modes","n_full","n_partial","isCalVal","isSNWG","isDNC","rollout","rollout_regions"]];
    Array.from(selected.values()).forEach(e=>{
      const p = e.feature.properties;
      rows.push([p.frame_idx,p.track,p.frame,p.passDirection,e.color,p.gslc_count,p.n_unique,p.n_duplicate,p.cons_mode,p.cons_cov,
        p.n_modes,p.n_full,p.n_partial,p.isCalVal,p.isSNWG,p.isDNC,
        asArray(p.rollout).join(";"),asArray(p.rollout_regions).join(";")]);
    });
    downloadBlob(toCsv(rows), "nisar_selected_frames.csv", "text/csv");
  });
  document.getElementById("btn-export-geojson").addEventListener("click", ()=>{
    const feats = Array.from(selected.values()).map(e=>{
      const clone = JSON.parse(JSON.stringify(e.feature));
      clone.properties.color = e.color;
      delete clone.properties.granules;   // keep the export compact
      return clone;
    });
    downloadBlob(JSON.stringify({type:"FeatureCollection", features: feats}, null, 2), "nisar_selected_frames.geojson", "application/geo+json");
  });

  // ---------- map ----------
  const style = {
    version: 8,
    projection: {type: "globe"},   // read at style load; the GlobeControl toggles from here
    sources: {
      "esri-light": { type:"raster", tiles:["https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}"], tileSize:256, maxzoom:16, attribution:"Esri, HERE, Garmin, &copy; OpenStreetMap contributors" },
      "esri-light-ref": { type:"raster", tiles:["https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Light_Gray_Reference/MapServer/tile/{z}/{y}/{x}"], tileSize:256, maxzoom:16, attribution:"Esri, HERE, Garmin, &copy; OpenStreetMap contributors" },
      "esri-dark": { type:"raster", tiles:["https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}"], tileSize:256, maxzoom:16, attribution:"Esri, HERE, Garmin, &copy; OpenStreetMap contributors" },
      "esri-dark-ref": { type:"raster", tiles:["https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}"], tileSize:256, maxzoom:16, attribution:"Esri, HERE, Garmin, &copy; OpenStreetMap contributors" },
      "esri-sat": { type:"raster", tiles:["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"], tileSize:256, attribution:"Esri World Imagery" },
      "google-hybrid": { type:"raster", tiles:["https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}"], tileSize:256, attribution:"Google" }
    },
    layers: [
      { id:"bm-light", type:"raster", source:"esri-light", layout:{visibility:"visible"} },
      { id:"bm-light-ref", type:"raster", source:"esri-light-ref", layout:{visibility:"visible"} },
      { id:"bm-dark", type:"raster", source:"esri-dark", layout:{visibility:"none"} },
      { id:"bm-dark-ref", type:"raster", source:"esri-dark-ref", layout:{visibility:"none"} },
      { id:"bm-sat", type:"raster", source:"esri-sat", layout:{visibility:"none"} },
      { id:"bm-sat2", type:"raster", source:"google-hybrid", layout:{visibility:"none"} }
    ]
  };

  let openFramePopup = null;   // set once the frame layers exist; used by the search box
  const map = new maplibregl.Map({
    container: "map",
    style: style,
    center: META.view_scope === "globe" ? [0, 20] : [-100, 40],
    zoom: META.view_scope === "globe" ? 1 : 1.4,
    attributionControl: true
  });
  map.addControl(new maplibregl.NavigationControl(), "bottom-right");

  // Frame summaries on hover are opt-in; the switch sits above the globe toggle.
  let hoverEnabled = false;
  let onHoverChange = ()=>{};
  const hoverInfoControl = {
    onAdd(){
      this._wrap = document.createElement("div");
      this._wrap.className = "maplibregl-ctrl maplibregl-ctrl-group";
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "hover-info-btn";
      btn.innerHTML = `<svg width="17" height="17" viewBox="0 0 24 24" aria-hidden="true">`+
        `<path fill="currentColor" d="M12 2.4A9.6 9.6 0 1 0 21.6 12 9.61 9.61 0 0 0 12 2.4zm0 1.8a7.8 7.8 0 1 1-7.8 7.8A7.81 7.81 0 0 1 12 4.2zm0 2.1a1.35 1.35 0 1 0 1.35 1.35A1.35 1.35 0 0 0 12 6.3zm-1.15 4.2h2.3v6.6h-2.3z"/></svg>`;
      const sync = ()=>{
        btn.classList.toggle("active", hoverEnabled);
        btn.title = hoverEnabled ? "Hover info: on" : "Hover info: off";
        btn.setAttribute("aria-label", btn.title);
        btn.setAttribute("aria-pressed", String(hoverEnabled));
      };
      btn.addEventListener("click", ()=>{ hoverEnabled = !hoverEnabled; sync(); onHoverChange(); });
      sync();
      this._wrap.appendChild(btn);
      return this._wrap;
    },
    onRemove(){ this._wrap.remove(); }
  };
  map.addControl(hoverInfoControl, "bottom-right");
  map.addControl(new maplibregl.GlobeControl(), "bottom-right");

  // Overlay switches stack above the globe toggle. Each click steps through
  // off -> layer -> layer + panel -> off, so the map can carry the layer
  // without its panel in the way.
  const overlayState = {snow:0, rollout:0, colorby:0, quake:0};
  const overlayButtons = {};
  function overlayControl(key, title, svg){
    return {
      onAdd(){
        this._wrap = document.createElement("div");
        this._wrap.className = "maplibregl-ctrl maplibregl-ctrl-group";
        const btn = document.createElement("button");
        btn.type = "button"; btn.className = "overlay-btn"; btn.title = title;
        btn.setAttribute("aria-label", title);
        btn.innerHTML = svg;
        // Earthquakes step through events, options, legend, off; the others
        // through layer, panel, off.
        btn.addEventListener("click", ()=> setOverlay(key, (overlayState[key] + 1) % (key === "quake" ? 4 : 3)));
        overlayButtons[key] = btn;
        this._wrap.appendChild(btn);
        return this._wrap;
      },
      onRemove(){ this._wrap.remove(); }
    };
  }
  const HAS_ROLLOUT_DATA = typeof ROLLOUT_DATA !== "undefined" && ROLLOUT_DATA.features.length > 0;
  if (HAS_ROLLOUT_DATA) map.addControl(overlayControl("rollout", "Rollout regions overview",
    // A flag planted on an outlined region: a rollout area, not a map layer.
    `<svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true"><g fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" stroke-linecap="round">`+
    `<path d="M3.5 16.5 8 13l4.5 2 4-2.5 4 2.2-1.2 5.3-6 1-5.5-1.2z"/>`+
    `<path d="M12 15V3.2"/><path d="M12 3.5h6.5l-1.6 2.3 1.6 2.4H12" fill="currentColor"/></g></svg>`), "bottom-right");
  if (META.has_blackout) map.addControl(overlayControl("snow", "Rainy / snow season blackouts",
    // A cloud dropping rain on one side and snow on the other.
    `<svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true"><g fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">`+
    `<path d="M7 14.5a4 4 0 0 1-.4-7.98A5.5 5.5 0 0 1 17.2 7a3.75 3.75 0 0 1 .3 7.5z"/>`+
    `<path d="M8 17.2 7 20M11 17.2 10 20"/>`+
    `<path d="M16 16.6v4.8M13.9 17.8l4.2 2.4M13.9 20.2l4.2-2.4"/></g></svg>`), "bottom-right");

  map.addControl(overlayControl("quake", "Earthquakes (USGS)",
    // A seismogram trace.
    `<svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.8" `+
    `stroke-linecap="round" stroke-linejoin="round" d="M2 12h4l2-5 2.5 11L13 4l2.5 12 1.8-6 1.2 2H22"/></svg>`), "bottom-right");

  // ---------- colour panel ----------
  // Its button steps: panel -> legend on the map -> off. Inside the panel the
  // Color by / Style tabs switch views without touching that cycle.
  const CB_GROUPS = [
    ["Counts", ["gslc_count","n_duplicate","n_modes","gunw_count"]],
    ["Mode / coverage", ["cons_mode","cons_cov","gslc_modes","gslc_pols","gunw_net","passDirection"]],
    ["Planning", ["rollout","blackout_months","blackout_month"]],
    ["Flags", ["flag_j","flag_f","flag_o","flag_r","flag_m","flag_d"]],
    ["Quality (QA)", ["gslc_qa_rl", ...QA_GUNW.map(k=>`gunw_qa_${k}`)]]
  ];
  let cbTab = 1;   // 1: option list, 2: style

  function setFrameOpacity(which, value){
    frameStyle[which] = value;
    paintFrames();
    syncOpacityInputs();
  }
  function syncOpacityInputs(){
    const set = (id, v)=>{ const el = document.getElementById(id); if (el && Number(el.value) !== v) el.value = v; };
    set("fill-opacity", frameStyle.fill); set("cb-fill", frameStyle.fill);
    set("outline-opacity", frameStyle.outline); set("cb-outline", frameStyle.outline);
    document.getElementById("opacity-val").textContent = frameStyle.fill;
    document.getElementById("outline-val").textContent = frameStyle.outline;
    document.getElementById("cb-fill-val").textContent = `${frameStyle.fill}%`;
    document.getElementById("cb-outline-val").textContent = `${frameStyle.outline}%`;
    document.querySelectorAll("#cb-presets .chip, #sb-presets .chip").forEach(c=>
      c.classList.toggle("active", Number(c.dataset.v) === frameStyle.fill));
  }
  document.getElementById("cb-presets").innerHTML =
    [0,25,50,75,100].map(v=>`<div class="chip" data-v="${v}">${v}</div>`).join("");
  document.getElementById("cb-presets").addEventListener("click", e=>{
    const c = e.target.closest("[data-v]");
    if (c) setFrameOpacity("fill", Number(c.dataset.v));
  });
  document.getElementById("cb-fill").addEventListener("input", e=> setFrameOpacity("fill", Number(e.target.value)));
  document.getElementById("cb-outline").addEventListener("input", e=> setFrameOpacity("outline", Number(e.target.value)));

  function selectColorBy(field){
    const sel = document.getElementById("color-by");
    sel.value = field;
    applyColorBy();
  }

  // The grouped option list, drawn into both the sidebar and the map panel:
  // only the colourings the product (and the page's data) offers, each with a
  // preview of its colours. Clicks are handled on the containers.
  function offeredColorings(){
    return new Map(Array.from(document.getElementById("color-by").options).filter(o=>!o.hidden)
      .map(o=>[o.value, o.textContent.replace(" (default)","")]));
  }
  function colorOptionsHtml(field){
    const offered = offeredColorings();
    const rows = names => names.filter(n=>offered.has(n) && COLOR_BY_FIELDS[n]).map(n=>
      `<button type="button" class="cb-opt${n === field ? " active" : ""}" data-field="${n}">`+
      `<span class="cb-name">${offered.get(n)}</span>${previewHtml(n)}</button>`).join("");
    // Every group folds; the current colouring's group opens first and the
    // user's own opening and closing then sticks across redraws.
    if (!cbGroupsOpen) cbGroupsOpen = new Set(CB_GROUPS.filter(([, n])=>n.includes(field)).map(([t])=>t));
    return CB_GROUPS.map(([title, names])=>{
      const html = rows(names);
      if (!html) return "";
      const n = names.filter(x=>offered.has(x)).length;
      const cur = names.includes(field) ? ` <span class="cmap-cur">&middot; ${offered.get(field)}</span>` : "";
      return `<details class="cb-more" data-group="${title}"${cbGroupsOpen.has(title) ? " open" : ""}>`+
             `<summary>${title} <span class="cmap-count">${n}</span>${cur}</summary>${html}</details>`;
    }).join("");
  }

  function renderColorbyPanel(){
    const field = document.getElementById("color-by").value;
    const offered = offeredColorings();
    document.getElementById("cb-options").innerHTML = colorOptionsHtml(field);
    document.getElementById("sb-options").innerHTML = colorOptionsHtml(field);
    document.querySelectorAll("#cb-options details.cb-more, #sb-options details.cb-more").forEach(d=>
      d.addEventListener("toggle", ()=>{
        if (d.open) cbGroupsOpen.add(d.dataset.group); else cbGroupsOpen.delete(d.dataset.group);
      }));
    document.getElementById("sb-legend").innerHTML = legendHtml(field);
    document.getElementById("cb-style-title").textContent = offered.get(field) || COLOR_BY_FIELDS[field].label;
    document.getElementById("cb-month-row").hidden = field !== "blackout_month";
    if (field === "blackout_month") {
      document.getElementById("cb-months").innerHTML = MONTHS.map((m,i)=>
        `<div class="chip${i === boMonth ? " active" : ""}" data-m="${i}">${m}</div>`).join("");
    }
    renderStyleControls(document.getElementById("cb-style-controls"), field, false);
    document.getElementById("cb-legend").innerHTML = legendHtml(field);
    document.getElementById("cb-overlay-note").hidden = !overlayState.snow;
  }
  ["cb-options","sb-options"].forEach(id=> document.getElementById(id).addEventListener("click", e=>{
    const b = e.target.closest("[data-field]");
    if (b) selectColorBy(b.dataset.field);
  }));
  document.querySelectorAll("#sb-tabs [data-sbtab]").forEach(b=> b.addEventListener("click", ()=>{
    const tab = Number(b.dataset.sbtab);
    document.querySelectorAll("#sb-tabs [data-sbtab]").forEach(x=> x.classList.toggle("active", x === b));
    document.getElementById("sb-tab-options").hidden = tab !== 1;
    document.getElementById("sb-tab-style").hidden = tab !== 2;
  }));
  document.getElementById("sb-presets").innerHTML =
    [0,25,50,75,100].map(v=>`<div class="chip" data-v="${v}">${v}</div>`).join("");
  document.getElementById("sb-presets").addEventListener("click", e=>{
    const c = e.target.closest("[data-v]");
    if (c) setFrameOpacity("fill", Number(c.dataset.v));
  });
  document.getElementById("cb-months").addEventListener("click", e=>{
    const c = e.target.closest("[data-m]");
    if (c) setBlackoutMonth(Number(c.dataset.m));
  });
  document.getElementById("cb-reset").addEventListener("click", ()=>{
    const field = document.getElementById("color-by").value;
    delete numStyle[field];
    delete baseColorMapsCache[COLOR_BY_FIELDS[field].key];
    applyColorBy();
  });
  document.getElementById("cb-overlay-off").addEventListener("click", ()=> setOverlay("snow", 0));
  function showCbTab(tab){
    cbTab = tab;
    document.getElementById("cb-options").hidden = tab !== 1;
    document.getElementById("cb-style").hidden = tab !== 2;
    document.getElementById("cb-legend").hidden = tab !== 1;   // the style tab draws its own bar
    document.querySelectorAll("#colorby-panel [data-tab]").forEach(b=> b.classList.toggle("active", Number(b.dataset.tab) === tab));
    layoutPanels();
  }
  document.querySelectorAll("#colorby-panel [data-tab]").forEach(b=>
    b.addEventListener("click", ()=> showCbTab(Number(b.dataset.tab))));

  function renderMapLegend(){
    const field = document.getElementById("color-by").value;
    const el = document.getElementById("map-legend");
    const wasHidden = el.hidden;
    el.hidden = overlayState.colorby !== 2;
    if (el.hidden) return;
    if (wasHidden) requestAnimationFrame(restoreLegendPosition);
    const opt = document.querySelector(`#color-by option[value="${field}"]`);
    document.getElementById("ml-title").textContent = opt ? opt.textContent.replace(" (default)","") : field;
    document.getElementById("ml-body").innerHTML = legendHtml(field);
  }
  // The legend is dragged by its title bar and kept inside the map; where it
  // was left is remembered for the next visit. A drag is not a click, so
  // letting go does not reopen the panel.
  const legendEl = document.getElementById("map-legend");
  let legendDrag = null, legendMoved = false;
  function placeLegend(left, top){
    const mapBox = document.getElementById("map").getBoundingClientRect();
    left = Math.max(0, Math.min(left, mapBox.width - legendEl.offsetWidth));
    top = Math.max(0, Math.min(top, mapBox.height - legendEl.offsetHeight));
    Object.assign(legendEl.style, {left:`${left}px`, top:`${top}px`, bottom:"auto"});
    return {left, top};
  }
  legendEl.querySelector(".ml-head").addEventListener("pointerdown", e=>{
    if (e.target.closest("#ml-close")) return;
    const box = legendEl.getBoundingClientRect(), mapBox = document.getElementById("map").getBoundingClientRect();
    legendDrag = {dx: e.clientX - box.left, dy: e.clientY - box.top, x0: e.clientX, y0: e.clientY, mapBox};
    legendMoved = false;
    e.currentTarget.setPointerCapture(e.pointerId);
  });
  legendEl.querySelector(".ml-head").addEventListener("pointermove", e=>{
    if (!legendDrag) return;
    if (Math.abs(e.clientX - legendDrag.x0) + Math.abs(e.clientY - legendDrag.y0) > 4) legendMoved = true;
    if (legendMoved) placeLegend(e.clientX - legendDrag.mapBox.left - legendDrag.dx, e.clientY - legendDrag.mapBox.top - legendDrag.dy);
  });
  legendEl.querySelector(".ml-head").addEventListener("pointerup", ()=>{
    if (legendDrag && legendMoved) {
      try { localStorage.setItem("nisar-viewer-legend", JSON.stringify({left: parseFloat(legendEl.style.left), top: parseFloat(legendEl.style.top)})); } catch (err) { /* storage off */ }
    }
    legendDrag = null;
  });
  legendEl.addEventListener("click", e=>{
    if (legendMoved) { legendMoved = false; return; }
    setOverlay("colorby", e.target.closest("#ml-close") ? 0 : 1);
  });
  function restoreLegendPosition(){
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem("nisar-viewer-legend") || "null"); } catch (err) { saved = null; }
    if (saved && Number.isFinite(saved.left) && Number.isFinite(saved.top)) placeLegend(saved.left, saved.top);
  }

  // Bottom panels stack upwards in this order; on a phone only one is open.
  const PANEL_ORDER = ["colorby","snow","rollout","quake","quakeleg"];
  const PHONE = window.matchMedia("(max-width: 768px)");
  function layoutPanels(){
    let bottom = 30;
    PANEL_ORDER.forEach(k=>{
      const el = document.getElementById(`${k}-panel`);
      if (el.hidden) return;
      el.style.bottom = PHONE.matches ? "" : `${bottom}px`;
      bottom += el.offsetHeight + 8;
    });
  }

  map.addControl(overlayControl("colorby", "Color frames by",
    `<svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" `+
    `d="M12 3a9 9 0 0 0 0 18c1.1 0 1.8-.8 1.8-1.7 0-.5-.2-.9-.5-1.2-.3-.3-.5-.7-.5-1.2 0-1 .8-1.7 1.8-1.7H17a4 4 0 0 0 4-4C21 6.7 17 3 12 3z"/>`+
    `<circle cx="7.5" cy="11.5" r="1.4" fill="currentColor"/><circle cx="10" cy="7.4" r="1.4" fill="currentColor"/>`+
    `<circle cx="14.6" cy="7.4" r="1.4" fill="currentColor"/><circle cx="17.2" cy="11" r="1.4" fill="currentColor"/></svg>`), "bottom-right");

  function setOverlay(key, level){
    if (key === "colorby") {
      overlayState.colorby = level;
      const btn = overlayButtons.colorby;
      if (btn) {
        btn.classList.toggle("active", level > 0);
        btn.title = `${btn.getAttribute("aria-label")} - ${["off","panel open","legend shown"][level]}; click for ${["panel","legend","off"][level]}`;
      }
      if (level === 1 && PHONE.matches) ["snow","rollout"].forEach(k=>{ if (overlayState[k] === 2) setOverlay(k, 1); });
      document.getElementById("colorby-panel").hidden = level !== 1;
      renderColorbyPanel();
      renderMapLegend();
      showCbTab(cbTab);
      return;
    }
    if (level === 2 && PHONE.matches) {
      if (overlayState.colorby === 1) setOverlay("colorby", 2);
      ["snow","rollout","quake"].filter(k=>k !== key).forEach(k=>{ if (overlayState[k] === 2) setOverlay(k, 1); });
    }
    overlayState[key] = level;
    const on = level > 0;
    const btn = overlayButtons[key];
    if (btn) {
      btn.classList.toggle("active", on);
      btn.title = `${btn.getAttribute("aria-label")} - ${["off","layer shown","layer and panel shown"][level]}; click for ${["layer","panel","off"][level]}`;
    }
    document.getElementById(`${key}-panel`).hidden = level < 2;
    if (key === "quake") {
      showQuakes(on);
      if (btn) btn.title = `${btn.getAttribute("aria-label")} - ${["off","events shown","options open","legend shown"][level]}; `+
        `click for ${["events","options","legend","off"][level]}`;
      document.getElementById("quake-panel").hidden = level !== 2;
      document.getElementById("quakeleg-panel").hidden = level !== 3;
      layoutPanels();
      return;
    }
    const layers = key === "snow" ? ["snow-fill","snow-line"] : ["rollout-fill","rollout-line"];
    layers.forEach(id=>{ if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", on ? "visible" : "none"); });
    if (level === 2 && key === "snow") refreshSnowPanel();
    if (key === "snow") applyColorBy();   // hides or restores the frame colours under the overlay
    layoutPanels();
  }
  // A panel's x closes just the panel: the overlay stays on, and the colour
  // panel leaves its legend on the map.
  document.querySelectorAll(".overlay-panel [data-close]").forEach(b=>
    b.addEventListener("click", ()=>{
      const key = b.dataset.close === "quakeleg" ? "quake" : b.dataset.close;
      setOverlay(key, key === "colorby" ? 2 : 1);
    }));


  // ---------- USGS earthquakes ----------
  // Fetched straight from the USGS FDSN event service (it allows any origin),
  // so this works on the published page too. Circles grow with magnitude and
  // are coloured by depth, the way USGS maps draw them.
  const EQ_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query";
  const EQ_LIMIT = 20000;
  const NA_AREA = [-170, 14, -52, 75];
  const EQ_DEPTHS = [[0,"#e5484d"],[35,"#ff8a4d"],[70,"#ffd24d"],[150,"#7ee787"],[300,"#4da3ff"],[700,"#a389ff"]];
  let eqLoaded = false, eqLoading = null;
  const eqDay = d=> d.toISOString().slice(0, 10);
  function eqArea(){
    const area = document.getElementById("eq-area").value;
    if (area === "world" || (area === "page" && META.view_scope === "globe")) return null;
    if (area === "page") return Array.isArray(META.view_bbox) ? META.view_bbox : NA_AREA;
    const b = map.getBounds();
    // A view across the antimeridian, or wider than the world, asks for everything.
    if (b.getWest() < -180 || b.getEast() > 180 || b.getWest() >= b.getEast()) return null;
    return [b.getWest(), Math.max(-90, b.getSouth()), b.getEast(), Math.min(90, b.getNorth())];
  }
  function eqQuery(){
    const period = document.getElementById("eq-period").value;
    let start, end;
    if (period === "custom") {
      start = document.getElementById("eq-start").value;
      end = document.getElementById("eq-end").value;
    } else {
      const now = new Date();
      start = eqDay(new Date(now.getTime() - Number(period) * 86400000));
      end = "";
    }
    const q = new URLSearchParams({format:"geojson", orderby:"time", limit:String(EQ_LIMIT),
                                   minmagnitude: document.getElementById("eq-mag").value});
    if (start) q.set("starttime", start);
    if (end) q.set("endtime", `${end}T23:59:59`);
    const area = eqArea();
    if (area) {
      const [w, s, e, n] = area;
      q.set("minlongitude", w.toFixed(3)); q.set("minlatitude", s.toFixed(3));
      q.set("maxlongitude", e.toFixed(3)); q.set("maxlatitude", n.toFixed(3));
    }
    return q;
  }
  function eqLegend(){
    const sizes = [[3,"M3"],[5,"M5"],[7,"M7"]].map(([m, t])=>{
      // As drawn at zoom 5.
      const r = Math.round(eqRadius(m) * 2);
      return `<span><i style="width:${r}px;height:${r}px;background:#888"></i>${t}</span>`;
    }).join("");
    const depths = EQ_DEPTHS.map(([d, c], i)=>
      `<span><i style="width:9px;height:9px;background:${c}"></i>${i < EQ_DEPTHS.length - 1 ? `${d}-${EQ_DEPTHS[i+1][0]}` : `${d}+`} km</span>`).join("");
    document.getElementById("eq-legend").innerHTML = sizes + depths;
    document.getElementById("eq-legend-2").innerHTML = sizes + depths;
  }
  function eqDescribe(n){
    const period = document.getElementById("eq-period");
    const area = document.getElementById("eq-area");
    const when = period.value === "custom"
      ? `${document.getElementById("eq-start").value} to ${document.getElementById("eq-end").value}`
      : period.selectedOptions[0].textContent;
    document.getElementById("eq-leg-what").textContent =
      `M${document.getElementById("eq-mag").value}+ \u00b7 ${when} \u00b7 ${area.selectedOptions[0].textContent}`+
      (n == null ? "" : ` \u00b7 ${n.toLocaleString()} event${n === 1 ? "" : "s"}`);
  }
  // Circle radius (px, at zoom 5) for a magnitude: area roughly tracks energy
  // over the few magnitudes a map shows, without M7s swamping a continent.
  function eqRadius(m){ return Math.max(1.5, 1.2 * Math.pow(1.42, m)); }
  const EQ_RADIUS_EXPR = ["interpolate", ["exponential", 1.42], ["get", "mag"], 0, 1.2, 9, eqRadius(9)];
  function ensureQuakeLayer(){
    if (map.getSource("quakes")) return;
    map.addSource("quakes", {type:"geojson", data:{type:"FeatureCollection", features:[]}});
    const color = ["interpolate", ["linear"], ["get", "depth"]];
    EQ_DEPTHS.forEach(([d, c])=> color.push(d, c));
    map.addLayer({id:"quake-points", type:"circle", source:"quakes",
      layout:{"circle-sort-key": ["get", "mag"]},
      paint:{
        // Smaller over a whole continent, full size once zoomed in.
        "circle-radius": ["interpolate", ["linear"], ["zoom"],
          1, ["*", 0.55, EQ_RADIUS_EXPR], 5, EQ_RADIUS_EXPR, 9, ["*", 1.4, EQ_RADIUS_EXPR]],
        "circle-color": color, "circle-opacity": 0.8,
        "circle-stroke-color": "#ffffff", "circle-stroke-width": 0.6
      }});
    const tip = new maplibregl.Popup({closeButton:false, closeOnClick:false, offset:8});
    const html = f=>{
      const p = f.properties;
      const when = new Date(p.time).toISOString().replace("T", " ").slice(0, 19);
      return `<div class="pop-title">M${Number(p.mag).toFixed(1)} ${p.magType || ""} &middot; ${p.place || ""}</div>`+
        `<div class="pop-row">${when} UTC &middot; depth ${Number(p.depth).toFixed(1)} km`+
        `${p.tsunami ? " &middot; tsunami flag" : ""}${p.alert ? ` &middot; PAGER ${p.alert}` : ""}</div>`;
    };
    map.on("mousemove", "quake-points", e=>{
      map.getCanvas().style.cursor = "pointer";
      tip.setLngLat(e.lngLat).setHTML(html(e.features[0])).addTo(map);
    });
    map.on("mouseleave", "quake-points", ()=>{ map.getCanvas().style.cursor = ""; tip.remove(); });
    map.on("click", "quake-points", e=>{
      const f = e.features[0];
      tip.remove();
      new maplibregl.Popup({offset:8}).setLngLat(e.lngLat)
        .setHTML(html(f) + `<div class="pop-row"><a href="${f.properties.url}" target="_blank" rel="noopener">USGS event page</a></div>`)
        .addTo(map);
    });
  }
  async function loadQuakes(){
    ensureQuakeLayer();
    const status = document.getElementById("eq-status");
    status.textContent = "loading...";
    const mine = eqLoading = eqQuery().toString();
    try {
      const r = await fetch(`${EQ_URL}?${mine}`);
      if (!r.ok) throw new Error((await r.text()).split("\n").find(l=>/Error|exceed|limit/i.test(l)) || `HTTP ${r.status}`);
      const j = await r.json();
      if (mine !== eqLoading) return;
      j.features.forEach(f=>{ f.properties.depth = f.geometry.coordinates[2]; });
      map.getSource("quakes").setData(j);
      eqLoaded = true;
      const n = j.features.length;
      eqDescribe(n);
      status.textContent = `${n.toLocaleString()} event${n === 1 ? "" : "s"}`+
        (n >= EQ_LIMIT ? ` (the newest ${EQ_LIMIT.toLocaleString()}; raise the magnitude for all)` : "");
    } catch (err) {
      if (mine === eqLoading) status.textContent = `could not load: ${err.message}`;
    }
  }
  function showQuakes(on){
    if (on && !eqLoaded) loadQuakes();
    if (map.getLayer("quake-points")) map.setLayoutProperty("quake-points", "visibility", on ? "visible" : "none");
  }
  document.getElementById("eq-period").addEventListener("change", e=>{
    const custom = e.target.value === "custom";
    document.getElementById("eq-dates").hidden = !custom;
    if (custom && !document.getElementById("eq-start").value) {
      document.getElementById("eq-start").value = eqDay(new Date(Date.now() - 365 * 86400000));
      document.getElementById("eq-end").value = eqDay(new Date());
    }
  });
  document.getElementById("eq-apply").addEventListener("click", ()=>{ loadQuakes(); });
  eqLegend();

  function refreshSnowPanel(){
    if (!META.has_blackout) return;
    const yr = ovMonth === "yr";
    document.querySelectorAll("#snow-months .chip").forEach(c=> c.classList.toggle("active", c.dataset.m === String(ovMonth)));
    document.getElementById("snow-unit").textContent = yr ? "share of the year blacked out" : `share of ${MONTHS[ovMonth]} blacked out`;
    document.getElementById("snow-lo").textContent = "0%";
    document.getElementById("snow-hi").textContent = yr ? "100% (365 d)" : "100%";
    const shown = shownFeatures.filter(f=>f.properties.has_blackout);
    const half = shown.filter(f=>(f.properties._bo_ov||0) >= 50).length;
    if (yr) {
      const days = shown.map(f=>f.properties._boDays).sort((a,b)=>a-b);
      const med = days.length ? days[Math.floor(days.length / 2)] : 0;
      document.getElementById("snow-stat").textContent =
        `${shown.length} of ${shownFeatures.length} frames shown have a blackout; median ${med} days a year `+
        `(${days.length ? days[0] : 0}-${days.length ? days[days.length-1] : 0}), ${half} lose at least half the year`;
    } else {
      document.getElementById("snow-stat").textContent =
        `${MONTHS[ovMonth]}: ${half} of ${shownFeatures.length} frames shown at least half blacked out`;
    }
  }
  if (META.has_blackout) {
    const el = document.getElementById("snow-months");
    el.innerHTML = `<div class="chip" data-m="yr" title="Whole year">YR</div>`+
      MONTHS.map((m,i)=>`<div class="chip" data-m="${i}">${m}</div>`).join("");
    el.addEventListener("click", e=>{
      const c = e.target.closest("[data-m]");
      if (c) setOverlayMonth(c.dataset.m === "yr" ? "yr" : Number(c.dataset.m));
    });
  }

  function renderRolloutPanel(){
    if (!HAS_ROLLOUT_DATA) return;
    const cmap = baseColorMap("_rollout");
    const src = META.rollout_source || "";
    document.getElementById("rollout-panel-note").textContent = src.endsWith(".geojson")
      ? "DISP-S1 North America rollout, drawn from its Sentinel-1 frames. Click an option to zoom to it."
      : `Rollout from ${src}, drawn as the union of its frames. Click an option to zoom to it.`;
    document.getElementById("rollout-panel-list").innerHTML = ROLLOUT_DATA.features.map(f=>{
      const p = f.properties, regions = asArray(p.regions);
      const s1 = p.n_source ? ` &middot; ${p.n_source} S1 frames` : "";
      return `<div class="ov-row" data-opt="${p.rollout}"><span class="ov-sw" style="background:${cmap.get(p.rollout)}"></span>`+
        `<div><div class="ov-name">${p.rollout} <span class="ov-sub">${p.n_frames} NISAR frames${s1}</span></div>`+
        `<div class="ov-sub">${regions.join(", ")}</div></div></div>`;
    }).join("");
  }
  document.getElementById("rollout-panel-list").addEventListener("click", e=>{
    const row = e.target.closest("[data-opt]");
    if (!row) return;
    const f = ROLLOUT_DATA.features.find(x=>x.properties.rollout === row.dataset.opt);
    let w = 180, so = 90, ea = -180, no = -90;
    const walk = c => { if (typeof c[0] === "number") { w = Math.min(w, c[0]); ea = Math.max(ea, c[0]); so = Math.min(so, c[1]); no = Math.max(no, c[1]); } else c.forEach(walk); };
    walk(f.geometry.coordinates);
    map.fitBounds([[w, so], [ea, no]], {padding: 60, duration: 1000});
  });

  // Each basemap is one or more stacked layers (the Esri canvases keep labels separate).
  const BASEMAP_LAYERS = {light:["bm-light","bm-light-ref"], dark:["bm-dark","bm-dark-ref"], sat:["bm-sat"], sat2:["bm-sat2"]};
  document.querySelectorAll('input[name="basemap"]').forEach(r=>{
    r.addEventListener("change", ()=>{
      Object.entries(BASEMAP_LAYERS).forEach(([value, layers])=> layers.forEach(layer=>
        map.setLayoutProperty(layer, "visibility", value === r.value ? "visible" : "none")));
    });
  });

  // ---------- light / dark theme ----------
  document.getElementById("f-frame-popup").addEventListener("change", (e)=>{
    if (!e.target.checked) document.querySelectorAll(".maplibregl-popup").forEach(el=>el.remove());
  });

  // ---------- phone drawer ----------
  const sidebarEl = document.getElementById("sidebar");
  const setDrawer = open => sidebarEl.classList.toggle("open", open);
  document.getElementById("menu-btn").addEventListener("click", ()=> setDrawer(true));
  document.getElementById("sidebar-close").addEventListener("click", ()=> setDrawer(false));
  document.getElementById("sidebar-backdrop").addEventListener("click", ()=> setDrawer(false));
  // The drawer is narrower than the desktop sidebar, so its charts are redrawn
  // at the width they have once it has slid in.
  sidebarEl.addEventListener("transitionend", ()=>{ if (sidebarEl.classList.contains("open")) applyFilters(); });
  window.matchMedia("(max-width: 768px)").addEventListener("change", e=>{
    if (!e.matches) setDrawer(false);
    map.resize();
    applyFilters();
  });

  const themeBtn = document.getElementById("theme-toggle");
  themeBtn.addEventListener("click", ()=>{
    const light = document.body.classList.toggle("theme-light");
    themeBtn.innerHTML = light ? "&#9789;" : "&#9788;";
    themeBtn.title = light ? "Switch to the dark theme" : "Switch to the light theme";
    // The sidebar chart is drawn with the theme colours baked into its markup.
    applyFilters();
  });

  function parseGranules(p){
    const granules = JSON.parse(typeof p.granules === "string" ? p.granules : JSON.stringify(p.granules));
    // NISAR_L2_PR_GSLC_<cycle>_<track>_<D|A>_<frame>_... - the pass direction is
    // the seventh field of the granule id, and nowhere else in the record.
    granules.forEach(g=>{ g.dir = String(g.gid || "").split("_")[6] || "?"; });
    return granules;
  }

  const DIR_LABEL = {A:"Ascending", D:"Descending"};

  function granuleCsv(p, granules){
    const qa = META.has_qa ? QA_LANES.gslc : [];
    const rows = [["frame_id","track","frame","pass","direction","date","mode","coverage","polarization","cycle","granule_id",
                   ...qa.map(k=>`qa_${k}`)]];
    granules.forEach(g=> rows.push([p.frame_idx,p.track,p.frame,p.passDirection,g.dir,g.date,g.mode,g.cov,g.pol,g.cycle,g.gid,
                                    ...qaCsvCols(g, qa)]));
    return toCsv(rows);
  }

  // Same date and mode means one acquisition delivered as several granules, so
  // the unique count -- not the granule count -- is what the timeline plots.
  function duplicateRow(p){
    if (!p.n_duplicate) return "";
    return `<div class="pop-row">Unique acquisitions: ${p.n_unique} `+
           `&middot; ${p.n_duplicate} duplicate granule(s) on the same date &amp; mode</div>`;
  }

  // Granules that share a date, mode and coverage -- the same key n_unique counts
  // -- grouped so each repeated acquisition lists every granule delivered for it.
  function duplicateGroups(granules){
    const groups = new Map();
    granules.forEach(g=>{
      const key = `${g.date}|${g.mode}|${g.cov}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(g);
    });
    return Array.from(groups.values()).filter(gs=>gs.length > 1);
  }

  function duplicateRowsHtml(groups){
    return groups.map(gs=>
      `<div class="granule-row"><span class="gdate">${gs[0].date}</span> `+
      `<span class="gmode">${gs[0].mode}_${gs[0].cov}</span> &middot; ${gs.length} granules</div>`+
      gs.map(g=>`<div class="granule-row dup-gid">${g.pol} ${g.dir} c${g.cycle}<br>${g.gid}</div>`).join("")
    ).join("");
  }

  function granulePopupHtml(p){
    const granules = parseGranules(p);
    const dupGroups = duplicateGroups(granules);
    const nDup = dupGroups.reduce((n, gs)=>n + gs.length - 1, 0);
    const isSel = selected.has(p.id);
    const dirs = uniqSorted(granules.map(g=>g.dir));
    const dirChips = dirs.length > 1
      ? `<div class="seg" id="pop-dir">`+
        `<div class="chip active" data-dir="all">All</div>`+
        dirs.map(d=>`<div class="chip" data-dir="${d}">${DIR_LABEL[d] || d}</div>`).join("")+
        `</div>`
      : "";
    let rows = granuleRowsHtml(granules);
    if (!rows) rows = `<div class="granule-row">No GSLC granules in CMR for this frame.</div>`;
    return `
      <div class="pop-title">Frame ${p.frame_idx} &middot; Track ${p.track} / Frame ${p.frame}</div>
      <div class="pop-row">Pass: ${p.passDirection} &middot; consistent: ${p.cons_mode}${p.cons_cov!=="none"?"_"+p.cons_cov:""}</div>
      <div class="pop-row">GSLC granules in CMR: ${p.gslc_count} &middot; ${p.n_modes} mode(s) &middot; ${p.n_full}F / ${p.n_partial}P</div>
      ${duplicateRow(p)}
      ${selectionRow(p)}
      ${qaSummaryRow(p, false)}
      ${rolloutLine(p)}
      ${blackoutDetailBlock(p)}
      <div class="pop-actions">
        <button class="btn small primary" id="pop-select">${isSel ? "Remove from selection" : "Add to selection"}</button>
        <button class="btn small" id="pop-granules"${granules.length ? "" : " disabled"}>Show granules (${granules.length})</button>
        <button class="btn small" id="pop-csv"${granules.length ? "" : " disabled"}>Export CSV</button>
        <button class="btn small" id="pop-plot"${granules.length ? "" : " disabled"}>Show plot</button>
        <button class="btn small" id="pop-strip"${granules.length ? "" : " disabled"}>Browse images</button>
        ${nDup ? `<button class="btn small" id="pop-dups">Show duplicates (${nDup})</button>` : ""}
      </div>
      <div id="pop-strip-panel" hidden></div>
      <div id="pop-granule-panel" hidden>${dirChips}<div class="granule-list" id="pop-granule-list">${rows}</div></div>
      ${nDup ? `<div id="pop-dup-panel" hidden><div class="granule-list">${duplicateRowsHtml(dupGroups)}</div></div>` : ""}`;
  }

  function granuleRowsHtml(granules){
    return granules.map(g=>
      `<div class="granule-row"><span class="gdate">${g.date}</span> `+
      `<span class="gmode">${g.mode}_${g.cov}</span> ${g.pol} ${g.dir} c${g.cycle}${g.crid ? ` ${g.crid}` : ""}`+
      `<button class="btn tiny" data-browse="${g.gid}" title="Show this granule's browse image">browse</button><br>${g.gid}</div>`
    ).join("");
  }

  function wireGranulePopup(feature){
    const p = feature.properties;
    const granules = parseGranules(p);
    const selBtn = document.getElementById("pop-select");
    if (selBtn) selBtn.addEventListener("click", ()=>{
      toggleSelectFrame(feature);
      selBtn.textContent = selected.has(p.id) ? "Remove from selection" : "Add to selection";
    });
    const listBtn = document.getElementById("pop-granules");
    const panel = document.getElementById("pop-granule-panel");
    const list = document.getElementById("pop-granule-list");
    if (listBtn && panel) listBtn.addEventListener("click", ()=>{
      panel.hidden = !panel.hidden;
      listBtn.textContent = `${panel.hidden ? "Show" : "Hide"} granules (${granules.length})`;
    });
    const dirBar = document.getElementById("pop-dir");
    if (dirBar && list) dirBar.addEventListener("click", (e)=>{
      const chip = e.target.closest("[data-dir]");
      if (!chip) return;
      dirBar.querySelectorAll(".chip").forEach(c=>c.classList.toggle("active", c === chip));
      const dir = chip.dataset.dir;
      const shown = dir === "all" ? granules : granules.filter(g=>g.dir === dir);
      list.innerHTML = granuleRowsHtml(shown) ||
        `<div class="granule-row">No ${DIR_LABEL[dir] || dir} granules for this frame.</div>`;
    });
    const dupBtn = document.getElementById("pop-dups");
    const dupPanel = document.getElementById("pop-dup-panel");
    if (dupBtn && dupPanel) {
      const label = dupBtn.textContent.replace(/^Show /, "");
      dupBtn.addEventListener("click", ()=>{
        dupPanel.hidden = !dupPanel.hidden;
        dupBtn.textContent = `${dupPanel.hidden ? "Show" : "Hide"} ${label}`;
      });
    }
    const csvBtn = document.getElementById("pop-csv");
    if (csvBtn) csvBtn.addEventListener("click", ()=>
      downloadBlob(granuleCsv(p, granules), `nisar_granules_${p.id}.csv`, "text/csv"));
    const plotBtn = document.getElementById("pop-plot");
    if (plotBtn) plotBtn.addEventListener("click", ()=> showModeTimeline(p, granules));
    wireStrip(p, false);
  }

  // ---------- GUNW view ----------
  function gunwSummaryRows(p){
    if (!p.gunw_count) return `<div class="pop-row">No GUNW interferograms in the catalog for this frame.</div>`;
    const span = p.gunw_dt_max !== p.gunw_dt_min ? `${p.gunw_dt_min}&ndash;${p.gunw_dt_max}` : `${p.gunw_dt_min}`;
    return `<div class="pop-row">GUNW interferograms: ${p.gunw_count} &middot; ${p.gunw_pairs} pair(s) &middot; mode ${asArray(p.gunw_modes).join(", ")}</div>`+
           `<div class="pop-row">Temporal baselines: ${span} days &middot; ${asArray(p.gunw_pols).join(", ")}</div>`;
  }

  function gunwHoverHtml(p){
    return `
      <div class="pop-title">Frame ${p.frame_idx} &middot; Track ${p.track} / Frame ${p.frame}</div>
      <div class="pop-row">Pass: ${p.passDirection}</div>
      ${gunwSummaryRows(p)}
      ${rolloutLine(p)}
      ${blackoutHoverLine(p)}
      <div class="pop-row" style="color:var(--accent)">click to list interferograms &amp; select</div>`;
  }

  function gunwRowsHtml(ifgs){
    return ifgs.map(g=>
      `<div class="granule-row"><span class="gdate">${g.ref} &rarr; ${g.sec}</span> `+
      `<span class="gmode">${g.dt} d</span> ${g.mode}_${g.cov} ${g.pol}`+
      `<button class="btn tiny" data-browse="${g.gid}" title="Show this pair's browse and QA images">browse</button><br>${g.gid}</div>`
    ).join("");
  }

  function gunwPopupHtml(p){
    const ifgs = asArray(p.gunw_ifgs);
    const off = ifgs.length ? "" : " disabled";
    return `
      <div class="pop-title">Frame ${p.frame_idx} &middot; Track ${p.track} / Frame ${p.frame}</div>
      <div class="pop-row">Pass: ${p.passDirection}</div>
      ${gunwSummaryRows(p)}
      ${qaSummaryRow(p, true)}
      ${rolloutLine(p)}
      ${blackoutDetailBlock(p)}
      <div class="pop-actions">
        <button class="btn small primary" id="pop-select">${selected.has(p.id) ? "Remove from selection" : "Add to selection"}</button>
        <button class="btn small" id="pop-ifgs"${off}>Show interferograms (${ifgs.length})</button>
        <button class="btn small" id="pop-csv"${off}>Export CSV</button>
        <button class="btn small" id="pop-plot"${off}>Show plot</button>
        <button class="btn small" id="pop-strip"${off}>Browse images</button>
      </div>
      <div id="pop-strip-panel" hidden></div>
      <div id="pop-ifg-panel" hidden><div class="granule-list">${gunwRowsHtml(ifgs)}</div></div>`;
  }

  function gunwCsv(p, ifgs){
    const qa = META.has_qa ? QA_GUNW : [];
    const rows = [["frame_id","track","frame","pass","ref_date","sec_date","temporal_baseline_days","mode","coverage","polarization","granule_id",
                   ...qa.map(k=>`qa_${k}`)]];
    ifgs.forEach(g=> rows.push([p.frame_idx,p.track,p.frame,p.passDirection,g.ref,g.sec,g.dt,g.mode,g.cov,g.pol,g.gid,
                                ...qaCsvCols(g, qa)]));
    return toCsv(rows);
  }

  function wireGunwPopup(feature){
    const p = feature.properties;
    const ifgs = asArray(p.gunw_ifgs);
    const selBtn = document.getElementById("pop-select");
    if (selBtn) selBtn.addEventListener("click", ()=>{
      toggleSelectFrame(feature);
      selBtn.textContent = selected.has(p.id) ? "Remove from selection" : "Add to selection";
    });
    const listBtn = document.getElementById("pop-ifgs");
    const panel = document.getElementById("pop-ifg-panel");
    if (listBtn && panel) listBtn.addEventListener("click", ()=>{
      panel.hidden = !panel.hidden;
      listBtn.textContent = `${panel.hidden ? "Show" : "Hide"} interferograms (${ifgs.length})`;
    });
    const csvBtn = document.getElementById("pop-csv");
    if (csvBtn) csvBtn.addEventListener("click", ()=>
      downloadBlob(gunwCsv(p, ifgs), `nisar_gunw_${p.id}.csv`, "text/csv"));
    const plotBtn = document.getElementById("pop-plot");
    if (plotBtn) plotBtn.addEventListener("click", ()=> showGunwPlot(p, ifgs));
    wireStrip(p, true);
  }

  // A frame's interferogram network: acquisition dates are nodes, pairs are
  // edges. ``components`` counts the pieces it falls into; ``breaks`` are the
  // spans between consecutive dates that no pair bridges, where a time series
  // built from these interferograms would come apart; ``offMain(t)`` says whether
  // a date lies outside the largest piece.
  function gunwNetwork(pairs){
    const parent = new Map();
    const find = x=>{ while (parent.get(x) !== x) { parent.set(x, parent.get(parent.get(x))); x = parent.get(x); } return x; };
    pairs.forEach(pt=>[pt.ta, pt.tb].forEach(t=>{ if (!parent.has(t)) parent.set(t, t); }));
    pairs.forEach(pt=>{ const a = find(pt.ta), b = find(pt.tb); if (a !== b) parent.set(a, b); });
    const dates = Array.from(parent.keys()).sort((a,b)=>a - b);
    const breaks = [];
    for (let i = 0; i < dates.length - 1; i++) {
      const lo = dates[i], hi = dates[i+1];
      if (!pairs.some(pt=>pt.ta <= lo && pt.tb >= hi)) breaks.push([lo, hi]);
    }
    const size = new Map();
    dates.forEach(d=>{ const r = find(d); size.set(r, (size.get(r) || 0) + 1); });
    let main = null;
    size.forEach((n, r)=>{ if (main === null || n > size.get(main)) main = r; });
    return {components: size.size, breaks, nDates: dates.length, offMain: t=>parent.has(t) && find(t) !== main};
  }

  // Each pair is a segment from reference to secondary date, raised by its
  // temporal baseline, so short and long pairs separate and gaps in the network
  // show as dates no segment spans. Polarizations of one pair share a segment.
  function gunwPlotSvg(ifgs, p){
    const byPair = new Map();
    ifgs.forEach(g=>{
      const k = `${g.ref}|${g.sec}|${g.mode}_${g.cov}`;
      if (!byPair.has(k)) byPair.set(k, {ta: Date.parse(`${g.ref}T00:00:00Z`), tb: Date.parse(`${g.sec}T00:00:00Z`),
                                         key: `${g.mode}_${g.cov}`, ifg: g, group: []});
      byPair.get(k).group.push(g);
    });
    chartPoints = Array.from(byPair.values()).filter(pt=>isFinite(pt.ta) && isFinite(pt.tb))
      .sort((a,b)=>a.ta - b.ta || a.tb - b.tb);
    if (!chartPoints.length) return `<div class="stat-line">No interferograms to plot.</div>`;

    const withFlags = showFlags && META.has_flags;
    const qaKeys = showQa && META.has_qa ? QA_LANES.gunw : [];
    // The flag and QA lanes are labelled in the left margin, like the GSLC
    // plot's, so it widens to fit "RFI mitig." when they are shown; a label
    // inside the plot area would sit under the earliest interferograms.
    const padL = withFlags || qaKeys.length ? 96 : 52, padR = 24, padT = 12, padB = 30;
    const W = chartWidth();
    // A card the user has made taller gives the baseline axis the extra room.
    const H = Math.max(280, chartRoom() - (withFlags ? FLAG_FIELDS.length * 20 + 12 : 0) - (qaKeys.length ? qaKeys.length * 20 + 12 : 0));
    let [t0, t1] = spanWithBlackouts(p, Math.min(...chartPoints.map(pt=>pt.ta)), Math.max(...chartPoints.map(pt=>pt.tb)));
    if (t1 === t0) { t0 -= 15 * DAY_MS; t1 += 15 * DAY_MS; }
    const pad = (t1 - t0) * 0.03;
    t0 -= pad; t1 += pad;
    const dMax = Math.max(12, ...chartPoints.map(pt=>pt.ifg.dt)) * 1.1;
    const xOf = t => padL + (t - t0) / (t1 - t0) * (W - padL - padR);
    const yOf = d => H - padB - d / dMax * (H - padT - padB);

    const {bands, blackoutWindows} = blackoutBands(p, t0, t1, xOf, padT, H - padB);
    const xTicks = timeTicks(t0, t1).map(tk=>
      `<line class="chart-grid" x1="${xOf(tk.t).toFixed(1)}" x2="${xOf(tk.t).toFixed(1)}" y1="${padT}" y2="${H-padB}" opacity="0.55"/>`+
      `<text class="chart-tick" x="${xOf(tk.t).toFixed(1)}" y="${H-padB+14}" text-anchor="middle">${tk.label}</text>`
    ).join("");
    // Baseline ticks in multiples of NISAR's 12-day repeat.
    const step = [12, 24, 48, 96, 192, 384].find(s=>dMax / s <= 6) || 768;
    let yTicks = "";
    for (let d = 0; d <= dMax; d += step) {
      yTicks += `<line class="chart-grid" x1="${padL}" x2="${W-padR}" y1="${yOf(d).toFixed(1)}" y2="${yOf(d).toFixed(1)}"/>`+
                `<text class="chart-tick" x="${padL-6}" y="${(yOf(d)+3.5).toFixed(1)}" text-anchor="end">${d}</text>`;
    }
    const axis = `<text class="chart-tick" transform="translate(12 ${(padT + H - padB) / 2}) rotate(-90)" text-anchor="middle">temporal baseline (days)</text>`;

    const keys = uniqSorted(chartPoints.map(pt=>pt.key));
    const colorOf = key => CHART_PALETTE[keys.indexOf(key) % CHART_PALETTE.length];
    // Pairs can instead be coloured by one QA metric, so the poor ones stand
    // out in the network itself.
    const qaG = pt => pt.group.find(g=>g.qa) || pt.ifg;
    const byQa = qaPairColor && META.has_qa ? qaScale(qaPairColor, chartPoints.map(pt=>qaValue(qaG(pt), qaPairColor))) : null;
    const net = gunwNetwork(chartPoints);
    const segs = chartPoints.map((pt,i)=>{
      const y = yOf(pt.ifg.dt).toFixed(1);
      const xa = xOf(pt.ta).toFixed(1), xb = xOf(pt.tb).toFixed(1);
      const c = byQa ? byQa(qaValue(qaG(pt), qaPairColor)) : colorOf(pt.key);
      // End dots keep back-to-back pairs of one baseline from reading as one line;
      // a red halo marks pairs cut off from the main network.
      const halo = net.offMain(pt.ta) ? `<line class="chart-ifg-off" x1="${xa}" x2="${xb}" y1="${y}" y2="${y}"/>` : "";
      return halo + `<line class="chart-ifg" data-i="${i}" x1="${xa}" x2="${xb}" y1="${y}" y2="${y}" stroke="${c}"/>`+
             `<circle class="chart-ifg-end" cx="${xa}" cy="${y}" r="3" fill="${c}"/>`+
             `<circle class="chart-ifg-end" cx="${xb}" cy="${y}" r="3" fill="${c}"/>`;
    }).join("");
    const fmt = t => new Date(t).toISOString().slice(0,10);
    const gaps = net.breaks.map(([lo, hi])=>{
      const x0 = xOf(lo), x1 = xOf(hi);
      return `<rect class="chart-gap" x="${x0.toFixed(1)}" y="${padT}" width="${(x1 - x0).toFixed(1)}" height="${H - padT - padB}">`+
             `<title>Network gap: no interferogram connects ${fmt(lo)} to ${fmt(hi)}</title></rect>`;
    }).join("");
    const legend = (byQa ? [`pairs by ${QA_FIELDS[qaPairColor].lane}: worse ${qaRampHtml()} better`]
                         : keys.map(k=>`<span style="color:${colorOf(k)}">&#9644;</span> ${k}`))
      .concat(blackoutWindows.length ? [`<span style="color:#8c8c8c">&#9632;</span> blackout (${p.blackout_label})`] : [])
      .concat(net.breaks.length ? [`<span style="color:#e5484d">&#9632;</span> network gap`] : [])
      .concat(net.components > 1 ? [`<span style="color:#e5484d">&#9644;</span> cut off from the main network`] : [])
      .join(" &middot; ");

    // Flag lanes sit under the date axis and share its x scale.
    const flags = withFlags
      ? flagLanesSvg(chartPoints.map((pt,i)=>({i, ta: pt.ta, tb: pt.tb, g: pt.group.find(g=>g.fl) || pt.ifg})),
                     xOf, padL, W - padR, H + 4)
      : null;
    const qaTop = flags ? H + 8 + flags.height + 4 : H + 4;
    const qaLanes = qaKeys.length
      ? qaLanesSvg(chartPoints.map((pt,i)=>({i, ta: pt.ta, tb: pt.tb, g: qaG(pt)})), qaKeys, xOf, padL, W - padR, qaTop)
      : null;
    const HH = qaLanes ? qaTop + 4 + qaLanes.height : (flags ? H + 8 + flags.height : H);
    return `<svg id="chart-svg" width="${W}" height="${HH}" viewBox="0 0 ${W} ${HH}" role="img"
      aria-label="GUNW interferograms by temporal baseline">${bands}${gaps}${xTicks}${yTicks}${axis}${segs}${flags ? flags.svg : ""}${qaLanes ? qaLanes.svg : ""}</svg>`+
      `<div class="chart-sub">${legend}${flags ? " &middot; " + flags.legend : ""}${qaLanes ? " &middot; " + qaLanes.legend : ""}</div>`;
  }

  function showGunwPlot(p, ifgs){
    document.getElementById("chart-title").textContent =
      `Frame ${p.frame_idx} (Track ${p.track} / Frame ${p.frame}) - interferograms by temporal baseline`;
    const refs = ifgs.map(g=>g.ref).sort(), secs = ifgs.map(g=>g.sec).sort();
    document.getElementById("chart-qa-color-ctl").hidden = !META.has_qa;
    redrawChart = ()=>{ document.getElementById("chart-body").innerHTML = gunwPlotSvg(ifgs, p); };
    redrawChart();
    const net = gunwNetwork(chartPoints);
    // Interleaved pieces can overlap in time and leave no gap to shade, so the
    // piece count is stated even when there is no break.
    const status = net.components > 1
      ? ` &middot; <span class="chart-warn">network disconnected: ${net.components} pieces`+
        `${net.breaks.length ? `, ${net.breaks.length} gap(s)` : ""}</span>`
      : (ifgs.length ? " &middot; network connected" : "");
    document.getElementById("chart-sub").innerHTML = ifgs.length
      ? `${p.gunw_pairs} pair(s), ${ifgs.length} GUNW granules - ${refs[0]} to ${secs[secs.length-1]}${status}`
      : "No GUNW interferograms";
    document.getElementById("chart-modal").hidden = false;
  }

  // ---------- acquisition timeline: mode (y) vs. acquisition date (x) ----------
  // Categorical steps chosen for the dark panel surface; adjacent pairs clear the
  // colour-vision-deficiency separation floor. Every row is also directly
  // labelled, so identity never rests on colour alone.
  const CHART_PALETTE = ["#3987e5","#d95926","#199e70","#c98500","#d55181","#008300","#9085e9","#e66767"];
  let modeKeyOrder = null;
  let chartPoints = [];

  function modeKeys(){
    if (!modeKeyOrder) {
      const seen = new Set();
      FRAME_DATA.features.forEach(f=> parseGranules(f.properties).forEach(g=> seen.add(`${g.mode}_${g.cov}`)));
      modeKeyOrder = Array.from(seen).sort();
    }
    return modeKeyOrder;
  }

  // Fixed hue per mode across every frame, so a mode keeps its colour when the popup changes.
  function modeColor(key){
    const i = modeKeys().indexOf(key);
    return i < 0 ? "#9db4c6" : CHART_PALETTE[i % CHART_PALETTE.length];
  }

  function timeTicks(t0, t1){
    const months = (t1 - t0) / DAY_MS / 30.4;
    const step = months > 30 ? 12 : months > 10 ? 3 : 1;
    const start = new Date(t0);
    let m = start.getUTCMonth();
    if (step > 1) m = Math.floor(m / step) * step;
    let d = new Date(Date.UTC(start.getUTCFullYear(), m, 1));
    const ticks = [];
    while (d.getTime() <= t1 && ticks.length < 14) {
      if (d.getTime() >= t0) ticks.push({t: d.getTime(),
        label: step === 12 ? String(d.getUTCFullYear())
                           : `${MONTHS[d.getUTCMonth()]} ${String(d.getUTCFullYear()).slice(2)}`});
      d = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + step, 1));
    }
    return ticks;
  }

  const DUP_ROW = "duplicates";

  function blackoutWindowsOf(p){
    return (p && p.has_blackout ? asArray(p.blackout_ranges) : []).map(r=>{
      const [a, b] = String(r).split("->").map(s=>s.trim());
      return {a, b, ta: Date.parse(`${a}T00:00:00Z`), tb: Date.parse(`${b}T23:59:59Z`)};
    }).filter(w=>isFinite(w.ta) && isFinite(w.tb));
  }

  // First and last date anywhere in the archive the page carries.
  let archiveSpan = null;
  function archiveBounds(){
    if (!archiveSpan) {
      let lo = Infinity, hi = -Infinity;
      const see = d=>{ const t = Date.parse(`${d}T00:00:00Z`); if (isFinite(t)) { lo = Math.min(lo, t); hi = Math.max(hi, t); } };
      FRAME_DATA.features.forEach(f=>{
        asArray(f.properties.granules).forEach(g=>see(g.date));
        asArray(f.properties.gunw_ifgs).forEach(g=>{ see(g.ref); see(g.sec); });
      });
      archiveSpan = [lo, hi];
    }
    return archiveSpan;
  }

  // Widen a plot's span to take in the frame's blackout windows, so a season the
  // frame was never imaged in still shows -- but only within the archive's dates.
  function spanWithBlackouts(p, t0, t1){
    const [lo, hi] = archiveBounds();
    blackoutWindowsOf(p).forEach(w=>{
      const a = Math.max(w.ta, lo), b = Math.min(w.tb, hi);
      if (a < b) { t0 = Math.min(t0, a); t1 = Math.max(t1, b); }
    });
    return [t0, t1];
  }

  // A frame's blackout windows as gray bands behind a plot, clipped to its span.
  function blackoutBands(p, t0, t1, xOf, yTop, yBottom){
    const blackoutWindows = blackoutWindowsOf(p).filter(w=>w.tb > t0 && w.ta < t1);
    const bands = blackoutWindows.map(w=>{
      const x0 = xOf(Math.max(w.ta, t0)), x1 = xOf(Math.min(w.tb, t1));
      return `<rect class="chart-blackout" x="${x0.toFixed(1)}" y="${yTop}" width="${(x1 - x0).toFixed(1)}" `+
             `height="${yBottom - yTop}"><title>Blackout ${w.a} to ${w.b}</title></rect>`;
    }).join("");
    return {bands, blackoutWindows};
  }

  function modeTimelineSvg(granules, p){
    chartPoints = granules.filter(g=>g.date).map(g=>({
      t: Date.parse(`${g.date}T00:00:00Z`), key: `${g.mode}_${g.cov}`, dir: g.dir, g
    })).sort((a,b)=>a.t-b.t);
    if (!chartPoints.length) return `<div class="stat-line">No dated granules to plot.</div>`;

    // Same date and mode plot at the same pixel, so a dot can hide others; count
    // them and let the tooltip say so rather than silently under-reporting.
    const stacks = new Map();
    chartPoints.forEach(pt=> stacks.set(`${pt.t}|${pt.key}`, (stacks.get(`${pt.t}|${pt.key}`) || 0) + 1));
    chartPoints.forEach(pt=> pt.stack = stacks.get(`${pt.t}|${pt.key}`));

    // One extra lane repeats each stacked date & mode as a single dot, so the
    // duplicates are visible at a glance rather than only in a tooltip.
    const rows = uniqSorted(chartPoints.map(pt=>pt.key));
    const dupPoints = [];
    const dupSeen = new Map();
    chartPoints.filter(pt=>pt.stack > 1).forEach(pt=>{
      const k = `${pt.t}|${pt.key}`;
      if (!dupSeen.has(k)) {
        dupSeen.set(k, {t: pt.t, key: DUP_ROW, modeKey: pt.key, dir: pt.dir, g: pt.g, stack: pt.stack, group: []});
        dupPoints.push(dupSeen.get(k));
      }
      dupSeen.get(k).group.push(pt.g);
    });
    if (dupPoints.length) rows.push(DUP_ROW);
    chartPoints = chartPoints.concat(dupPoints);
    const padL = 96, padR = 24, padT = 10, padB = 30, rowH = 34;
    const W = chartWidth();
    const withFlags = showFlags && META.has_flags;
    const qaKeys = showQa && META.has_qa ? QA_LANES.gslc : [];
    const flagTop = padT + rows.length * rowH + 6;
    const flagH = withFlags ? FLAG_FIELDS.length * 20 + 6 : 0;
    const qaTop = flagTop + flagH;
    const qaH = qaKeys.length ? qaKeys.length * 20 + 6 : 0;
    const H = padT + rows.length * rowH + flagH + qaH + padB;
    let [t0, t1] = spanWithBlackouts(p, chartPoints[0].t, chartPoints[chartPoints.length-1].t);
    if (t1 === t0) { t0 -= 15 * DAY_MS; t1 += 15 * DAY_MS; }
    const pad = (t1 - t0) * 0.03;
    t0 -= pad; t1 += pad;
    const xOf = t => padL + (t - t0) / (t1 - t0) * (W - padL - padR);
    const yOf = key => padT + rows.indexOf(key) * rowH + rowH / 2;

    const {bands, blackoutWindows} = blackoutBands(p, t0, t1, xOf, padT, H - padB);

    const ticks = timeTicks(t0, t1).map(tk=>
      `<line class="chart-grid" x1="${xOf(tk.t).toFixed(1)}" x2="${xOf(tk.t).toFixed(1)}" y1="${padT}" y2="${H-padB}" opacity="0.55"/>`+
      `<text class="chart-tick" x="${xOf(tk.t).toFixed(1)}" y="${H-padB+14}" text-anchor="middle">${tk.label}</text>`
    ).join("");

    const lanes = rows.map(key=>
      `<line class="chart-grid" x1="${padL}" x2="${W-padR}" y1="${yOf(key)}" y2="${yOf(key)}"/>`+
      `<text class="chart-row-label" x="${padL-10}" y="${yOf(key)+3.5}" text-anchor="end">${key}</text>`
    ).join("");

    // Circle = ascending, diamond = descending; shape carries the direction so it
    // survives the mode colouring and colour-vision deficiency alike.
    const dots = chartPoints.map((pt,i)=>{
      const x = xOf(pt.t), y = yOf(pt.key), fill = modeColor(pt.modeKey || pt.key);
      if (pt.dir === "D") {
        const r = 5.2;
        const pts = [[x, y-r],[x+r, y],[x, y+r],[x-r, y]].map(c=>c.map(v=>v.toFixed(1)).join(",")).join(" ");
        return `<polygon class="chart-dot" data-i="${i}" points="${pts}" fill="${fill}"/>`;
      }
      return `<circle class="chart-dot" data-i="${i}" cx="${x.toFixed(1)}" cy="${y}" r="4.5" fill="${fill}"/>`;
    }).join("");
    const flags = withFlags
      ? flagLanesSvg(chartPoints.map((pt,i)=>({i, ta: pt.t, g: pt.key === DUP_ROW ? null : pt.g})), xOf, padL, W - padR, flagTop)
      : null;
    const qaLanes = qaKeys.length
      ? qaLanesSvg(chartPoints.map((pt,i)=>({i, ta: pt.t, g: pt.key === DUP_ROW ? null : pt.g})), qaKeys, xOf, padL, W - padR, qaTop)
      : null;
    const shapeKey = uniqSorted(chartPoints.map(pt=>pt.dir)).map(d=>
      d === "D" ? "&#9670; descending" : d === "A" ? "&#9679; ascending" : `? ${d}`)
      .concat(blackoutWindows.length ? [`<span style="color:#8c8c8c">&#9632;</span> blackout (${p.blackout_label})`] : [])
      .concat(flags ? [flags.legend] : [])
      .concat(qaLanes ? [qaLanes.legend] : [])
      .join(" &middot; ");

    return `<svg id="chart-svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img"
      aria-label="GSLC acquisitions by mode over time">${bands}${ticks}${lanes}${dots}${flags ? flags.svg : ""}${qaLanes ? qaLanes.svg : ""}</svg>`+
      `<div class="chart-sub">${shapeKey}</div>`;
  }

  function showModeTimeline(p, granules){
    const dated = granules.filter(g=>g.date).map(g=>g.date).sort();
    document.getElementById("chart-title").textContent =
      `Frame ${p.frame_idx} (Track ${p.track} / Frame ${p.frame}) - acquisitions by mode`;
    const dup = p.n_duplicate ? ` (${p.n_unique} unique, ${p.n_duplicate} stacked)` : "";
    document.getElementById("chart-sub").textContent = dated.length
      ? `${dated.length} GSLC granules${dup} - ${dated[0]} to ${dated[dated.length-1]}`
      : "No dated GSLC granules";
    document.getElementById("chart-qa-color-ctl").hidden = true;
    redrawChart = ()=>{ document.getElementById("chart-body").innerHTML = modeTimelineSvg(granules, p); };
    redrawChart();
    document.getElementById("chart-modal").hidden = false;
  }

  function hideModeTimeline(){
    document.getElementById("chart-modal").hidden = true;
    document.getElementById("chart-tip").hidden = true;
  }

  document.getElementById("chart-close").addEventListener("click", hideModeTimeline);
  document.getElementById("chart-expand").addEventListener("click", ()=>{
    const card = document.querySelector(".chart-card");
    const big = card.classList.toggle("big");
    if (big) { card.style.width = ""; card.style.height = ""; }
    document.getElementById("chart-expand").title = big ? "Restore" : "Expand";
  });
  {
    let drawn = "";
    new ResizeObserver(()=>{
      if (document.getElementById("chart-modal").hidden || !redrawChart) return;
      const card = document.querySelector(".chart-card");
      const size = `${document.getElementById("chart-body").clientWidth}x${card.style.height || card.classList.contains("big") ? card.clientHeight : 0}`;
      if (size === drawn) return;   // redrawing changes the content height; only a new card size counts
      drawn = size;
      requestAnimationFrame(()=> redrawChart());
    }).observe(document.querySelector(".chart-card"));
  }
  document.getElementById("chart-flags").addEventListener("change", (e)=>{
    showFlags = e.target.checked;
    if (redrawChart) redrawChart();
  });
  document.getElementById("chart-qa").addEventListener("change", (e)=>{
    showQa = e.target.checked;
    if (redrawChart) redrawChart();
  });
  document.getElementById("chart-qa-color").innerHTML = `<option value="">by mode</option>`+
    QA_LANES.gunw.map(k=>`<option value="${k}">by ${QA_FIELDS[k].lane}</option>`).join("");
  document.getElementById("chart-qa-color").addEventListener("change", (e)=>{
    qaPairColor = e.target.value;
    if (redrawChart) redrawChart();
  });
  // Only a click that starts and ends on the backdrop closes the plot: dragging
  // the card's resize corner past its edge ends on the backdrop too.
  let modalDownOnBackdrop = false;
  document.getElementById("chart-modal").addEventListener("pointerdown", (e)=>{
    modalDownOnBackdrop = e.target.id === "chart-modal";
  });
  document.getElementById("chart-modal").addEventListener("click", (e)=>{
    if (e.target.id === "chart-modal" && modalDownOnBackdrop) hideModeTimeline();
    modalDownOnBackdrop = false;
  });
  document.addEventListener("keydown", (e)=>{ if (e.key === "Escape") hideModeTimeline(); });

  const chartTip = document.getElementById("chart-tip");
  document.getElementById("chart-body").addEventListener("mousemove", (e)=>{
    // Ascending dots are circles and descending ones polygons, so match the class.
    const dot = e.target.closest ? e.target.closest(".chart-dot[data-i], .chart-ifg[data-i]") : null;
    if (!dot) { chartTip.hidden = true; return; }
    const pt = chartPoints[Number(dot.dataset.i)];
    if (pt.ifg) {
      chartTip.innerHTML = `<b>${pt.ifg.ref} &rarr; ${pt.ifg.sec}</b> &middot; ${pt.ifg.dt} days<br>`+
        `<span class="tdim">${pt.key} &middot; ${pt.group.map(g=>g.pol).join(", ")}</span><br>`+
        pt.group.map(g=>`<span class="tdim">${g.gid}</span>`).join("<br>") + flagLine(pt.group) + qaLine(pt.group);
    } else if (pt.group) {
      chartTip.innerHTML = `<b>${pt.g.date}</b> &middot; ${pt.modeKey} &middot; ${pt.group.length} granules<br>`+
        pt.group.map(g=>`<span class="tdim">${g.pol} &middot; ${DIR_LABEL[g.dir] || g.dir} &middot; ${g.gid}</span>`).join("<br>") + flagLine(pt.group) + qaLine(pt.group);
    } else {
      const stacked = pt.stack > 1
        ? `<br><span class="tdim">${pt.stack} granules here (${pt.stack - 1} duplicate) - showing the top one</span>`
        : "";
      chartTip.innerHTML = `<b>${pt.g.date}</b> &middot; ${pt.key}<br>`+
        `<span class="tdim">${pt.g.pol} &middot; ${DIR_LABEL[pt.dir] || pt.dir} &middot; cycle ${pt.g.cycle}</span><br>`+
        `<span class="tdim">${pt.g.gid}</span>${stacked}` + flagLine([pt.g]) + qaLine([pt.g]);
    }
    // Unhide first: a display:none tip measures 0 wide and would defeat the clamp.
    chartTip.hidden = false;
    const card = chartTip.parentElement.getBoundingClientRect();
    chartTip.style.left = `${Math.max(8, Math.min(e.clientX - card.left + 12, card.width - chartTip.offsetWidth - 8))}px`;
    chartTip.style.top = `${e.clientY - card.top + 14}px`;
  });
  document.getElementById("chart-body").addEventListener("mouseleave", ()=>{ chartTip.hidden = true; });
  document.getElementById("chart-body").addEventListener("click", (e)=>{
    const dot = e.target.closest ? e.target.closest(".chart-dot[data-i], .chart-ifg[data-i]") : null;
    if (!dot) return;
    const pt = chartPoints[Number(dot.dataset.i)];
    const g = pt.ifg ? pt.group[0] : (pt.group ? pt.group[0] : pt.g);
    if (g && g.gid) openBrowse(g.gid);
  });

  map.on("load", ()=>{
    if (Array.isArray(META.view_bbox)) {
      const [w, s, e, n] = META.view_bbox;
      map.fitBounds([[w, s], [e, n]], {padding: 30, duration: 0});
    }
    map.addSource("frames", { type:"geojson", data: FRAME_DATA });
    map.addLayer({
      id:"frames-fill", type:"fill", source:"frames",
      paint:{ "fill-color": colorExpression("gslc_count"), "fill-opacity": 0.32 }
    });
    map.addLayer({
      id:"frames-outline", type:"line", source:"frames",
      paint:{ "line-color": colorExpression("gslc_count"), "line-width": 1, "line-opacity":0.7 }
    });

    map.addSource("selected", { type:"geojson", data:{type:"FeatureCollection", features:[]} });
    map.addLayer({
      id:"selected-fill", type:"fill", source:"selected",
      paint:{ "fill-color": ["get","__color"], "fill-opacity": 0.45 }
    });
    map.addLayer({
      id:"selected-outline", type:"line", source:"selected",
      paint:{ "line-color": ["get","__color"], "line-width": 3 }
    });

    // Seasonal blackout overlay: every frame with a blackout window is filled
    // on one light-to-dark ramp by the share of the year (or month) it loses,
    // at a fixed opacity, and the frame colouring underneath is hidden while it
    // is on, so 0% and 100% read as different colours rather than as a wash of
    // varying strength over other colours. Most windows are snow; Central
    // America's Aug-Nov window is its rainy season, carried over from DISP-S1.
    const BO_RAMP = ["#f2f7fc","#c6dbef","#9ecae1","#6baed6","#3182bd","#08519c","#08306b"];
    const boColor = ["interpolate", ["linear"], ["to-number", ["get","_bo_ov"], 0]];
    BO_RAMP.forEach((c,i)=> boColor.push(100 * i / (BO_RAMP.length - 1), c));
    map.addLayer({
      id:"snow-fill", type:"fill", source:"frames", layout:{visibility:"none"},
      filter:["==", ["get","has_blackout"], true],
      paint:{ "fill-color": boColor, "fill-opacity": 0.8 }
    });
    map.addLayer({
      id:"snow-line", type:"line", source:"frames", layout:{visibility:"none"},
      filter:["==", ["get","has_blackout"], true],
      paint:{ "line-color":"#08306b", "line-width":0.6, "line-opacity":0.55 }
    });
    if (HAS_ROLLOUT_DATA) {
      const rcolor = ["match", ["get","rollout"]];
      baseColorMap("_rollout").forEach((c, v)=>{ if (v !== "none") rcolor.push(v, c); });
      rcolor.push("#9a9a9a");
      map.addSource("rollout-regions", { type:"geojson", data: ROLLOUT_DATA });
      map.addLayer({ id:"rollout-fill", type:"fill", source:"rollout-regions", layout:{visibility:"none"},
                     paint:{ "fill-color": rcolor, "fill-opacity": 0.35 } });
      map.addLayer({ id:"rollout-line", type:"line", source:"rollout-regions", layout:{visibility:"none"},
                     paint:{ "line-color": rcolor, "line-width": 2 } });
      renderRolloutPanel();
    }

    // ---------- color-by / opacity ----------
    // The sidebar and the map's colour panel edit the same state: the
    // colour-by select, the two opacities, and the per-field colour styles.
    paintFrames = function(){
      const field = document.getElementById("color-by").value;
      const colorExpr = colorExpression(field);
      map.setPaintProperty("frames-fill", "fill-color", colorExpr);
      map.setPaintProperty("frames-outline", "line-color", colorExpr);
      map.setPaintProperty("frames-fill", "fill-opacity", overlayState.snow ? 0 : frameStyle.fill / 100);
      map.setPaintProperty("frames-outline", "line-opacity",
        overlayState.snow ? Math.min(0.2, frameStyle.outline / 100) : frameStyle.outline / 100);
    };
    applyColorBy = function(){
      const field = document.getElementById("color-by").value;
      document.getElementById("bo-month-row").hidden = field !== "blackout_month";
      document.getElementById("colorby-scope").hidden = !SELECTION_FIELDS.has(field);
      paintFrames();
      syncOpacityInputs();
      renderStyleControls(document.getElementById("colorby-legend"), field, false);
      renderColorbyPanel();
      renderMapLegend();
    };
    document.getElementById("color-by").addEventListener("change", applyColorBy);
    document.getElementById("fill-opacity").addEventListener("input", e=> setFrameOpacity("fill", Number(e.target.value)));
    document.getElementById("outline-opacity").addEventListener("input", e=> setFrameOpacity("outline", Number(e.target.value)));
    document.getElementById("btn-reset-style").addEventListener("click", ()=>{
      Object.keys(baseColorMapsCache).forEach(k=>delete baseColorMapsCache[k]);
      Object.keys(numStyle).forEach(k=>delete numStyle[k]);
      cmapPopOpen = false;
      document.getElementById("color-by").value = "gslc_count";
      frameStyle.fill = 32; frameStyle.outline = 70;
      applyColorBy();
    });
    syncColorByOptions();
    applyColorBy();
    applyFilters();

    // hover summary
    const popup = new maplibregl.Popup({ closeButton:true, closeOnClick:false });
    let hoverCloseTimer = null;
    const cancelHoverClose = ()=>{ clearTimeout(hoverCloseTimer); hoverCloseTimer = null; };
    const scheduleHoverClose = ()=>{ cancelHoverClose(); hoverCloseTimer = setTimeout(()=>popup.remove(), 350); };
    onHoverChange = ()=>{ if (!hoverEnabled) popup.remove(); };
    map.on("mousemove", "frames-fill", (e)=>{
      map.getCanvas().style.cursor = "pointer";
      if (!hoverEnabled) return;
      if (map.getLayer("gps-points") &&
          map.queryRenderedFeatures(e.point, {layers:["gps-points"]}).length) { popup.remove(); return; }
      if (map.getLayer("quake-points") &&
          map.queryRenderedFeatures(e.point, {layers:["quake-points"]}).length) { popup.remove(); return; }
      cancelHoverClose();
      const p = e.features[0].properties;
      popup.setLngLat(e.lngLat).setHTML(product === "gunw" ? gunwHoverHtml(p) : `
        <div class="pop-title">Frame ${p.frame_idx} &middot; Track ${p.track} / Frame ${p.frame}</div>
        <div class="pop-row">Pass: ${p.passDirection} &middot; ${p.cons_mode}${p.cons_cov!=="none"?"_"+p.cons_cov:""}</div>
        <div class="pop-row">GSLC granules in CMR: ${p.gslc_count} &middot; ${p.n_modes} mode(s)</div>
        ${duplicateRow(p)}
        ${selectionRow(p)}
        ${rolloutLine(p)}
        ${blackoutHoverLine(p)}
        ${referenceHoverLine(p)}
        <div class="pop-row" style="color:var(--accent)">click to list granules &amp; select</div>
      `).addTo(map);
      const el = popup.getElement();
      if (el && !el.dataset.hoverBound) {
        el.dataset.hoverBound = "1";
        el.addEventListener("mouseenter", cancelHoverClose);
        el.addEventListener("mouseleave", scheduleHoverClose);
      }
    });
    // Close on a delay so the pointer can travel onto the popup (and its X) first.
    map.on("mouseleave", "frames-fill", ()=>{ map.getCanvas().style.cursor = ""; scheduleHoverClose(); });

    // ---------- UNR GPS sites ----------
    // Hidden until asked for: 10k markers over the frames is a lot of ink.
    if (UNR_GPS_DATA.features.length) {
      map.addSource("unr-gps", { type:"geojson", data: UNR_GPS_DATA });
      map.addLayer({
        id:"gps-points", type:"circle", source:"unr-gps",
        minzoom: 0,
        layout:{ visibility:"none" },
        paint:{
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 2, 1.5, 6, 3, 12, 6],
          "circle-color": "#ff6fc7",
          "circle-stroke-color": "#0f1418", "circle-stroke-width": 1
        }
      });
      document.getElementById("gps-count").textContent = UNR_GPS_DATA.features.length;
      document.getElementById("row-gps").hidden = false;
      document.getElementById("gps-hint").hidden = false;
      document.getElementById("f-gps-show").addEventListener("change", (e)=>{
        map.setLayoutProperty("gps-points", "visibility", e.target.checked ? "visible" : "none");
      });

      const gpsHoverPopup = new maplibregl.Popup({ closeButton:false, closeOnClick:false });
      map.on("mousemove", "gps-points", (e)=>{
        map.getCanvas().style.cursor = "pointer";
        const p = e.features[0].properties;
        gpsHoverPopup.setLngLat(e.lngLat).setHTML(
          `<div class="pop-title">${p.id}</div><div class="pop-row">${p.frame} &middot; click for time series</div>`
        ).addTo(map);
      });
      map.on("mouseleave", "gps-points", ()=>{ map.getCanvas().style.cursor = ""; gpsHoverPopup.remove(); });

      map.on("click", "gps-points", (e)=>{
        const p = e.features[0].properties;
        const imgUrl = `https://geodesy.unr.edu/gps_timeseries/${p.frame}/tsplots/${p.frame}/TimeSeries/${p.id}.png`;
        new maplibregl.Popup({ closeButton:true, closeOnClick:true, maxWidth:"320px" })
          .setLngLat(e.lngLat)
          .setHTML(
            `<div class="pop-title">${p.id}</div>
             <a href="${imgUrl}" target="_blank" rel="noopener">
               <img src="${imgUrl}" style="width:100%;max-width:300px;border-radius:4px;margin-top:4px;" alt="${p.id} time series">
             </a>
             <div class="pop-row" style="margin-top:4px;"><a href="${imgUrl}" target="_blank" rel="noopener" style="color:var(--accent)">Open full plot &#8594;</a></div>`
          )
          .addTo(map);
      });
    }

    // click -> granule list popup with a select toggle
    const clickPopup = new maplibregl.Popup({ closeButton:true, closeOnClick:false, maxWidth:"none", className:"frame-pop" });
    // Expanded or not, the next frame's popup opens the way the last one was left.
    let popBig = false;
    function addPopupExpand(){
      const el = clickPopup.getElement();
      if (!el) return;
      el.classList.toggle("pop-big", popBig);
      const content = el.querySelector(".maplibregl-popup-content");
      const btn = document.createElement("button");
      btn.type = "button"; btn.className = "pop-expand";
      btn.title = popBig ? "Restore" : "Expand"; btn.innerHTML = "&#10530;";
      btn.addEventListener("click", ()=>{
        popBig = !popBig;
        content.style.width = ""; content.style.height = "";
        el.classList.remove("pop-resized");
        el.classList.toggle("pop-big", popBig);
        btn.title = popBig ? "Restore" : "Expand";
      });
      content.appendChild(btn);
      // A corner drag sets an inline size; the lists then grow with it. Any new
      // size re-places the popup, so MapLibre picks the side of the click point
      // it fits on rather than letting it run off the top of the screen.
      new ResizeObserver(()=>{
        if (content.style.width || content.style.height) el.classList.add("pop-resized");
        if (!clickPopup.isOpen()) return;
        clickPopup.setLngLat(clickPopup.getLngLat());
        // Too tall for either side of the click point: pan the map just enough
        // to bring the whole popup into view.
        requestAnimationFrame(()=>{
          const r = el.getBoundingClientRect(), m = document.getElementById("map").getBoundingClientRect();
          const pad = 8;
          let dy = 0;
          if (r.bottom > m.bottom - pad) dy = r.bottom - (m.bottom - pad);
          if (r.top - dy < m.top + pad) dy = r.top - (m.top + pad);
          if (Math.abs(dy) > 2) map.panBy([0, dy], {duration: 250});
        });
      }).observe(content);
    }
    map.on("click", "frames-fill", (e)=>{
      if (!document.getElementById("f-frame-popup").checked) return;
      // A GPS marker always sits inside some frame; a click on one belongs to it.
      if (map.getLayer("gps-points") &&
          map.queryRenderedFeatures(e.point, {layers:["gps-points"]}).length) return;
      if (map.getLayer("quake-points") &&
          map.queryRenderedFeatures(e.point, {layers:["quake-points"]}).length) return;
      const feature = idToFeature(e.features[0].properties.id);
      if (!feature) return;
      cancelHoverClose();
      popup.remove();
      openFramePopup(feature, e.lngLat);
    });
    openFramePopup = (feature, lngLat)=>{
      if (product === "gunw") {
        clickPopup.setLngLat(lngLat).setHTML(gunwPopupHtml(feature.properties)).addTo(map);
        wireGunwPopup(feature);
        addPopupExpand();
        return;
      }
      clickPopup.setLngLat(lngLat).setHTML(granulePopupHtml(feature.properties)).addTo(map);
      wireGranulePopup(feature);
      addPopupExpand();
    };
  });

  // ---------- GSLC / GUNW switch ----------
  // GUNW mode recolours the frames by interferogram count, turns the Mode /
  // Polarization chips and the over-time chart to GUNW, and routes the hover,
  // popup, CSV and plot to the frame's interferograms. The GSLC colour-by is
  // restored on return.
  let gslcColorBy = "gslc_count";

  // The colour-by list offers only what fits the product shown: GSLC counts and
  // consistent-mode views under GSLC, interferogram views under GUNW; pass
  // direction, rollout, blackout and flags apply to both. Options the page has
  // no data for stay hidden either way, so availability is read once, after
  // every META check has unhidden what it supports.
  let colorByAvailable = null;
  function syncColorByOptions(){
    const opts = Array.from(document.querySelectorAll("#color-by option"));
    if (!colorByAvailable) colorByAvailable = new Set(opts.filter(o=>!o.hidden).map(o=>o.value));
    opts.forEach(o=>{
      o.hidden = !colorByAvailable.has(o.value) || (o.dataset.product && o.dataset.product !== product);
    });
  }
  document.getElementById("product-ctrl").addEventListener("click", (e)=>{
    const btn = e.target.closest("[data-product]");
    if (!btn || btn.dataset.product === product) return;
    product = btn.dataset.product;
    document.querySelectorAll("#product-ctrl [data-product]").forEach(b=>b.classList.toggle("active", b === btn));
    const sel = document.getElementById("color-by");
    if (product === "gunw") {
      gslcColorBy = sel.value;
      if (sel.selectedOptions[0].dataset.product === "gslc") sel.value = "gunw_count";
    } else if (sel.value.startsWith("gunw_")) sel.value = gslcColorBy;
    syncColorByOptions();
    renderChips();
    applyFilters();          // re-filters on this product's chips and recolours
    document.querySelectorAll(".maplibregl-popup").forEach(el=>el.remove());
    hideModeTimeline();
    document.getElementById("top-hint").innerHTML = product === "gunw"
      ? "Click a frame to list interferograms &amp; select &middot; the (i) button toggles hover summaries"
      : "Click a frame to list granules &amp; select &middot; the (i) button toggles hover summaries";
  });

  // ---------- search: place, coordinates, or frame ----------
  // Coordinates and frames are answered from the page; only a place name costs a
  // network call. Nominatim's usage policy is one request a second, so place
  // lookups fire on Enter, are spaced by a timer, and are cached for the page.
  const NOMINATIM = "https://nominatim.openstreetmap.org/search";
  const MIN_GAP_MS = 1100;
  const placeCache = new Map();
  let lastQueryAt = 0;
  const qEl = document.getElementById("search-q");
  const resEl = document.getElementById("search-results");
  const hideResults = ()=>{ resEl.hidden = true; resEl.innerHTML = ""; };
  const showResults = html=>{ resEl.innerHTML = html; resEl.hidden = false; };
  const escHtml = s=>String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;");
  const flyToPoint = (lon, lat, zoom)=> map.flyTo({center:[lon, lat], zoom: zoom == null ? 8 : zoom, duration:1200});

  function fitPlace(bbox, lon, lat){
    // Nominatim gives [south, north, west, east] as strings. A point result has a
    // zero-area box (fitBounds then silently does nothing), an antimeridian box
    // arrives as west > east, and a continent-sized one is no better than not
    // moving -- fly to the point in all three cases.
    const [s, n, w, e] = bbox.map(Number);
    const width = e - w, height = n - s;
    if (!(isFinite(width) && isFinite(height)) || width < 0.002 || height < 0.002) return flyToPoint(lon, lat, 11);
    if (w > e) return flyToPoint(lon, lat, 5);
    if (width > 120 || height > 90) return flyToPoint(lon, lat, 4);
    map.fitBounds([[w, s], [e, n]], {padding:80, duration:1200, maxZoom:11});
  }

  function parseCoords(text){
    const m = text.match(/^\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*$/);
    if (!m) return null;
    const a = parseFloat(m[1]), b = parseFloat(m[2]);
    // "lat lon" is how people write it; fall back to "lon lat" when only that fits.
    if (Math.abs(a) <= 90 && Math.abs(b) <= 180) return {lat:a, lon:b};
    if (Math.abs(b) <= 90 && Math.abs(a) <= 180) return {lat:b, lon:a};
    return null;
  }

  function matchFrames(text){
    const q = text.trim().toLowerCase();
    if (!q) return [];
    const tf = q.match(/^t?(\d+)\s*[_ /]\s*f?(\d+)$/);
    if (tf) {
      const track = Number(tf[1]), frame = Number(tf[2]);
      return FRAME_DATA.features.filter(f=>Number(f.properties.track)===track && Number(f.properties.frame)===frame).slice(0, 8);
    }
    if (!/^\d+$/.test(q)) return [];
    return FRAME_DATA.features.filter(f=>String(f.properties.frame_idx).startsWith(q)).slice(0, 8);
  }

  function featureBounds(f){
    let w = Infinity, s = Infinity, e = -Infinity, n = -Infinity;
    const walk = c=>{
      if (typeof c[0] === "number") { w = Math.min(w, c[0]); e = Math.max(e, c[0]); s = Math.min(s, c[1]); n = Math.max(n, c[1]); }
      else c.forEach(walk);
    };
    walk(f.geometry.coordinates);
    return [[w, s], [e, n]];
  }

  async function runSearch(text){
    const coords = parseCoords(text);
    if (coords) { hideResults(); flyToPoint(coords.lon, coords.lat, 9); return; }

    const frames = matchFrames(text);
    if (frames.length) {
      showResults(frames.map(f=>{
        const p = f.properties;
        return `<button data-frame="${p.id}">Frame ${p.frame_idx}`+
          `<small>Track ${p.track} / Frame ${p.frame} &middot; ${p.passDirection} &middot; ${p.gslc_count} GSLC granule(s)</small></button>`;
      }).join(""));
      return;
    }

    const key = text.trim().toLowerCase();
    if (!key) { hideResults(); return; }
    if (placeCache.has(key)) { renderPlaces(placeCache.get(key)); return; }
    const wait = MIN_GAP_MS - (Date.now() - lastQueryAt);
    if (wait > 0) await new Promise(r=>setTimeout(r, wait));
    lastQueryAt = Date.now();
    showResults(`<div class="msg">Searching&hellip;</div>`);
    try {
      const response = await fetch(`${NOMINATIM}?format=jsonv2&limit=6&q=${encodeURIComponent(text)}`,
                                   {headers:{Accept:"application/json"}});
      if (!response.ok) throw new Error(`Nominatim returned ${response.status}`);
      const places = await response.json();
      placeCache.set(key, places);
      renderPlaces(places);
    } catch (err) {
      showResults(`<div class="msg">Place lookup failed (${escHtml(err.message)}). Coordinates and frame ids still work offline.</div>`);
    }
  }

  function renderPlaces(places){
    if (!places.length) { showResults(`<div class="msg">Nothing found.</div>`); return; }
    showResults(places.map((pl, i)=>
      `<button data-place="${i}">${escHtml(pl.name || pl.display_name)}<small>${escHtml(pl.display_name)}</small></button>`
    ).join(""));
    resEl.__places = places;
  }

  resEl.addEventListener("click", (ev)=>{
    const b = ev.target.closest("button");
    if (!b) return;
    if (b.dataset.frame) {
      const f = idToFeature(b.dataset.frame);
      hideResults();
      if (!f) return;
      qEl.value = `Frame ${f.properties.frame_idx}`;
      const bounds = featureBounds(f);
      map.fitBounds(bounds, {padding:80, duration:1200, maxZoom:8});
      if (openFramePopup) openFramePopup(f, [(bounds[0][0]+bounds[1][0])/2, (bounds[0][1]+bounds[1][1])/2]);
      return;
    }
    const pl = (resEl.__places || [])[+b.dataset.place];
    if (!pl) return;
    const lon = parseFloat(pl.lon), lat = parseFloat(pl.lat);
    if (pl.boundingbox) fitPlace(pl.boundingbox, lon, lat); else flyToPoint(lon, lat);
    hideResults();
    qEl.value = pl.name || pl.display_name;
  });

  qEl.addEventListener("keydown", (ev)=>{
    if (ev.key === "Enter") { ev.preventDefault(); runSearch(qEl.value); }
    if (ev.key === "Escape") { hideResults(); qEl.blur(); }
  });
  // Frames and coordinates are local, so answer those while typing; a place name
  // waits for Enter so Nominatim is not hit on every keystroke.
  qEl.addEventListener("input", ()=>{
    const text = qEl.value;
    if (!text.trim() || parseCoords(text)) { hideResults(); return; }
    if (matchFrames(text).length) runSearch(text); else hideResults();
  });

  renderSelectedList();
})();
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> None:
    """Command-line entry point for building the scope viewer."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--frames-gpkg",
        type=Path,
        default=Path("notebooks/opera-nisar-disp-frames.gpkg"),
        help="OPERA NISAR-DISP frames GeoPackage.",
    )
    parser.add_argument(
        "--gslc-db",
        type=Path,
        default=Path("notebooks/gslc_catalog.duckdb"),
        help="GSLC catalog DuckDB store (table 'products'), from build-s3-catalog.",
    )
    parser.add_argument(
        "--gslc-catalog",
        type=Path,
        default=None,
        help="GSLC catalog CSV from 'nisar-db create-gslc-csv'; use instead of "
        "--gslc-db when the granules came from CMR rather than a bucket scan.",
    )
    parser.add_argument(
        "--consistent-json",
        type=Path,
        default=None,
        help="Optional consistent-GSLC catalog JSON/.json.zip to drive the "
        "consistent mode/coverage fields (from 'nisar-db create-consistent').",
    )
    parser.add_argument(
        "--blackout-json",
        type=Path,
        default=None,
        help="Optional per-frame blackout-dates JSON/.json.zip (from "
        "'nisar-db create-blackout-dates'); adds blackout duration coloring, "
        "filtering, and hover details.",
    )
    parser.add_argument(
        "--reference-json",
        type=Path,
        default=None,
        help="Optional per-frame reference-dates JSON/.json.zip; shows InSAR "
        "reference resets on hover/click.",
    )
    parser.add_argument(
        "--gunw-catalog",
        type=Path,
        default=None,
        help="Optional GUNW catalog (gunw_interferograms.json or .json.gz from "
        "'create_gunw_catalog'); adds a GSLC / GUNW switch to the viewer.",
    )
    parser.add_argument(
        "--granule-flags",
        type=Path,
        default=None,
        help="Optional per-granule flag cache (from collect_granule_flags.py); "
        "adds flag colouring and flag lanes in the per-frame plots.",
    )
    parser.add_argument(
        "--granule-qa",
        type=Path,
        default=None,
        help="Optional per-granule QA cache (from collect_granule_qa.py); adds "
        "the Quality colourings and QA lanes in the per-frame plots.",
    )
    parser.add_argument(
        "--calval-sites",
        type=Path,
        default=CALVAL_SITES,
        help="GeoJSON of CalVal site polygons; the ascending and descending "
        "frames covering them are the viewer's CalVal frames.",
    )
    parser.add_argument(
        "--rollout",
        type=Path,
        default=ROLLOUT_REGIONS,
        help="Rollout regions: a GeoJSON of polygons carrying 'rollout' (matched "
        "by overlap), or a JSON {option: [frame_idx, ...]} frame list.",
    )
    parser.add_argument(
        "--gps-source",
        default=NGL_STATION_MAP,
        help="UNR/NGL station map URL, a local copy of that page, or a GeoJSON "
        "of sites; drawn as an optional map layer.",
    )
    parser.add_argument(
        "--no-gps",
        action="store_true",
        help="Build without the UNR GPS layer.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("scripts/nisar_scope_viewer.html"),
        help="Output HTML path.",
    )
    parser.add_argument(
        "--title",
        default="OPERA NISAR-DB Viewer",
        help="Document title.",
    )
    args = parser.parse_args(argv)

    print(f"Loading frames from {args.frames_gpkg}")
    gdf = load_frames(args.frames_gpkg)
    print(f"  {len(gdf)} frames")
    gdf["isCalVal"] = flag_calval_frames(gdf, gpd.read_file(args.calval_sites))
    print(f"  {int(gdf['isCalVal'].sum())} CalVal frames from {args.calval_sites}")
    rollout_options, gdf["rollout"], gdf["rollout_regions"] = rollout_by_frame(
        gdf, args.rollout
    )
    rollout_regions = rollout_overview(
        gdf, args.rollout, rollout_options, list(gdf["rollout"])
    )
    n_rollout = sum(1 for r in gdf["rollout"] if r)
    print(f"  {n_rollout} frames in a rollout option from {args.rollout}")

    if args.gslc_catalog is not None:
        print(f"Loading GSLC catalog from {args.gslc_catalog}")
        catalog = load_gslc_catalog_csv(args.gslc_catalog)
    else:
        print(f"Loading GSLC catalog from {args.gslc_db}")
        catalog = load_gslc_catalog(args.gslc_db)
    print(f"  {len(catalog)} GSLC granules")

    consistent = None
    if args.consistent_json is not None:
        print(f"Loading consistent-GSLC catalog from {args.consistent_json}")
        consistent = load_consistent_json(args.consistent_json)
        print(f"  {len(consistent)} frames in consistent catalog")

    blackout = None
    if args.blackout_json is not None:
        print(f"Loading blackout dates from {args.blackout_json}")
        blackout = load_period_json(args.blackout_json, "blackout_dates", "data")
        print(f"  {len(blackout)} frames with blackout windows")

    reference = None
    if args.reference_json is not None:
        print(f"Loading reference dates from {args.reference_json}")
        reference = load_period_json(args.reference_json, "data", "reference_dates")
        print(f"  {len(reference)} frames with reference resets")

    gunw = None
    if args.gunw_catalog is not None:
        print(f"Loading GUNW catalog from {args.gunw_catalog}")
        gunw = load_gunw_catalog(args.gunw_catalog)
        print(f"  {len(gunw)} GUNW granules")
    frame_data = build_frame_data(
        gdf, catalog, consistent, blackout, reference, gunw=gunw
    )
    n_with = sum(1 for f in frame_data["features"] if f["properties"]["gslc_count"] > 0)

    n_flagged = 0
    if args.granule_flags is not None:
        print(f"Loading granule flags from {args.granule_flags}")
        n_flagged = attach_granule_flags(
            frame_data, load_granule_flags(args.granule_flags)
        )
        print(f"  flags for {n_flagged} granules / interferograms")
    n_qa = 0
    if args.granule_qa is not None:
        print(f"Loading granule QA metrics from {args.granule_qa}")
        n_qa = attach_granule_qa(frame_data, load_granule_flags(args.granule_qa))
        print(f"  QA metrics for {n_qa} granules / interferograms")

    catalog_path = args.gslc_catalog if args.gslc_catalog is not None else args.gslc_db
    meta = {
        "title": args.title,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        # When the catalog was written, i.e. when CMR (or the bucket) was last
        # queried. The refresh workflow builds the catalog in the same run, so
        # this tracks the cron schedule without extra plumbing.
        "catalog_queried_at": (
            datetime.fromtimestamp(
                catalog_path.stat().st_mtime, tz=timezone.utc
            ).isoformat(timespec="seconds")
        ),
        "catalog_source": catalog_path.name,
        "catalog_kind": "cmr" if args.gslc_catalog is not None else "bucket-scan",
        "n_frames": len(frame_data["features"]),
        "n_frames_with_gslc": n_with,
        # Granules actually drawn: the catalog can span the globe, the map does
        # not, and reporting the raw row count overstates the page's contents.
        "n_granules": sum(
            len(f["properties"]["granules"]) for f in frame_data["features"]
        ),
        "n_catalog_rows": int(len(catalog)),
        "consistent_source": str(args.consistent_json) if consistent else "computed",
        "has_blackout": blackout is not None,
        "has_reference": reference is not None,
        "has_gunw": gunw is not None,
        "has_flags": n_flagged > 0,
        "has_qa": n_qa > 0,
        "rollout_options": rollout_options,
        "rollout_source": args.rollout.name,
        "n_gunw": sum(
            f["properties"].get("gunw_count", 0) for f in frame_data["features"]
        ),
    }

    gps_sites = load_gps_sites(None if args.no_gps else args.gps_source)
    if gps_sites["features"]:
        print(f"  {len(gps_sites['features'])} UNR GPS sites")

    html = render_html(frame_data, meta, gps_sites, rollout_regions)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html)
    size_mb = args.output.stat().st_size / 1e6
    print(
        f"Wrote {args.output} ({size_mb:.1f} MB); {n_with}/{meta['n_frames']} frames have GSLC"
    )


if __name__ == "__main__":
    main()
