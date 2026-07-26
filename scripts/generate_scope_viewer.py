#!/usr/bin/env python
"""Generate a self-contained HTML viewer for the NISAR OPERA (North America) scope.

The viewer is a single offline HTML file (MapLibre GL is vendored under
``scripts/vendor/``) that draws every OPERA NISAR-DISP frame and joins in the
GSLC catalog so each frame carries:

* the number of GSLC granules present in CMR (``gslc_count``),
* the *consistent* observation mode / coverage chosen for DISP time series
  (``cons_mode`` / ``cons_cov``, following the same voting rule as
  :mod:`nisar_db.consistent_gslc`), and
* the full granule list, surfaced in a click-to-open popup.

It is intentionally a design sibling of ``scripts/nisar_frame_viewer_v1.html``
(same look and controls) with these deliberate differences:

* globe (global) projection is the default at start,
* frames can be colored by GSLC count or by consistent mode / coverage,
* hovering a frame shows a dismissable summary and clicking it opens the
  granule list, a per-frame granule CSV export, and a plot of observation mode
  against acquisition date,
* a live "Consistent Mode Summary" panel aggregates the shown frames,
* the solid-earth CalVal site boxes are dropped,
* selected frames can be imported from a CSV, a GeoJSON, or a consistent-GSLC
  catalog JSON (the output of ``nisar-db create-consistent``), and
* an optional blackout-dates JSON adds blackout-duration (months) coloring, a
  "blackout frames only" filter, a summary panel, and per-frame hover/click
  detail of the blacked-out ranges; an optional reference-dates JSON adds the
  InSAR reference resets to the same hover/click detail.

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
"""

from __future__ import annotations

import argparse
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import duckdb
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
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        df = con.execute(
            f"SELECT {', '.join(_CATALOG_COLUMNS)} FROM products"
        ).fetchdf()
    finally:
        con.close()
    return _add_date_column(df)


def load_gslc_catalog_csv(csv_path: Path) -> pd.DataFrame:
    """Read the GSLC catalog CSV written by ``nisar-db create-catalog``.

    The CSV route is what the CMR-sourced pipeline produces: a bucket scan
    (`build-s3-catalog`) yields the DuckDB store, but CMR gives granule names,
    which `create-catalog` parses into the same per-granule fields under
    slightly different names.
    """
    df = pd.read_csv(csv_path, dtype={"mode": str, "cycle": str, "crid": str})
    df = df.rename(
        columns={"pass_direction": "direction", "sensing_time": "start_datetime"}
    )
    missing = [c for c in _CATALOG_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"{csv_path} is missing catalog columns: {missing}")
    return _add_date_column(df[_CATALOG_COLUMNS])


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

    Priority, identical to :func:`nisar_db.consistent_gslc._common_mode_coverage`.
    Each mode settles its own coverage by majority (``F`` when ``n_F >= n_P``),
    then the modes compete on:

    1. coverage — full-frame (``F``) beats partial (``P``), reversed when more
       than :data:`PARTIAL_DOMINANCE_THRESHOLD` of the candidates are partial,
    2. the acquisition count of the selected ``(mode, coverage)`` combo, with
       non-standard modes competing only when the frame has no standard
       acquisitions, and
    3. mode — :data:`MODE_PRIORITY` (``4005`` then ``2005``) settles modes level
       on coverage and count.
    """
    counts = (
        group.groupby(["mode", "coverage"])
        .size()
        .unstack(fill_value=0)
        .reindex(columns=["F", "P"], fill_value=0)
    )
    standard = counts[counts.index.isin(STANDARD_MODES)]
    candidates = standard if not standard.empty else counts

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
    return {
        "gslc_count": int(len(group)),
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


# ---------------------------------------------------------------------------
# Feature building
# ---------------------------------------------------------------------------
def build_frame_data(
    gdf: gpd.GeoDataFrame,
    catalog: pd.DataFrame,
    consistent: dict[str, dict] | None,
    blackout: dict[str, list] | None = None,
    reference: dict[str, list] | None = None,
) -> dict:
    """Assemble the frames ``FeatureCollection`` embedded in the viewer."""
    stats = {
        key: summarize_frame(grp)
        for key, grp in catalog.groupby(["track", "frame", "direction"])
    }

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
            # Site flags come from the TrackFrame GeoPackage; a stale GeoPackage
            # means stale CalVal frames, so refresh it when the site list changes.
            "isCalVal": bool(row["isCalVal"]),
            "isSNWG": bool(row["isSNWG"]),
            "isDNC": bool(row["isDNC"]),
            "gslc_count": s["gslc_count"] if s else 0,
            "n_modes": s["n_modes"] if s else 0,
            "n_full": s["n_full"] if s else 0,
            "n_partial": s["n_partial"] if s else 0,
            "cons_mode": s["cons_mode"] if s else "none",
            "cons_cov": s["cons_cov"] if s else "none",
            "gslc_modes": s["gslc_modes"] if s else [],
            "gslc_pols": s["gslc_pols"] if s else [],
            "granules": s["granules"] if s else [],
        }

        # A published consistent-GSLC catalog wins over the recomputed choice.
        if consistent is not None:
            entry = consistent.get(str(frame_idx))
            if entry is not None:
                props["cons_mode"] = entry.get("common_mode", props["cons_mode"])
                props["cons_cov"] = entry.get("common_coverage", props["cons_cov"])
                props["n_consistent"] = len(entry.get("sensing_time_list", []))
                props["in_consistent"] = True
            else:
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


def render_html(frame_data: dict, meta: dict, gps_sites: dict | None = None) -> str:
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
  #pass-ctrl{position:absolute;top:10px;left:10px;background:var(--scrim);border:1px solid var(--border);
    border-radius:6px;padding:6px 8px;z-index:5;font-size:11.5px;display:flex;gap:8px;}
  #pass-ctrl label{display:flex;align-items:center;gap:4px;color:var(--text);margin:0;cursor:pointer;}
  #click-ctrl{position:absolute;top:48px;left:10px;background:var(--scrim);border:1px solid var(--border);
    border-radius:6px;padding:6px 8px;z-index:5;font-size:11.5px;}
  #click-ctrl label{display:flex;align-items:center;gap:4px;color:var(--text);margin:0;cursor:pointer;}
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
  .granule-row{font-size:10.5px;color:var(--text-dim);padding:2px 0;border-bottom:1px solid var(--hairline);font-family:ui-monospace,Menlo,Consolas,monospace;}
  .granule-row .gdate{color:var(--text);}
  .granule-row .gmode{color:var(--accent2);}
  .day-bar{fill:var(--accent);}
  .day-bar:hover{fill:#b2daf7;}
  .day-base{stroke:var(--border);stroke-width:1;}
  .day-axis{fill:var(--text-dim);font-size:9.5px;}
  .date-row{display:flex;gap:6px;align-items:center;margin-top:6px;}
  .date-row input[type=date]{flex:1;background:var(--inset);border:1px solid var(--border);color:var(--text);
    border-radius:5px;padding:3px 5px;font-size:11px;color-scheme:dark;}
  body.theme-light .date-row input[type=date]{color-scheme:light;}
  .legend-color{width:28px;height:20px;padding:0;border:1px solid var(--border);border-radius:4px;background:none;cursor:pointer;}
  #theme-toggle{position:absolute;top:11px;right:12px;background:none;border:none;color:var(--text-dim);cursor:pointer;font-size:15px;line-height:1;padding:2px 4px;}
  #theme-toggle:hover{color:var(--accent);}
  .seg{display:flex;gap:4px;margin:6px 0 2px 0;}
  .seg .chip{flex:1;text-align:center;}
  #chart-modal{position:fixed;inset:0;z-index:20;background:rgba(0,0,0,.66);display:flex;align-items:center;justify-content:center;}
  #chart-modal[hidden]{display:none;}
  .chart-card{position:relative;background:var(--panel);border:1px solid var(--border);border-radius:8px;
    padding:12px 14px;max-width:min(780px,94vw);max-height:88vh;overflow:auto;}
  .chart-head{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;}
  .chart-sub{color:var(--text-dim);font-size:11px;margin:2px 0 8px 0;}
  .chart-tick{fill:var(--text-dim);font-size:10px;}
  .chart-row-label{fill:var(--text);font-size:10.5px;font-family:ui-monospace,Menlo,Consolas,monospace;}
  .chart-grid{stroke:var(--border);stroke-width:1;}
  .chart-dot{stroke:var(--panel);stroke-width:2;cursor:pointer;}
  .chart-dot:hover{stroke:#f5f5f5;}
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
  .bar-fill{height:100%;border-radius:5px;}
  .bar-row .bn{width:34px;text-align:right;color:var(--text);flex-shrink:0;}
  ::-webkit-scrollbar{width:8px;height:8px;}
  ::-webkit-scrollbar-thumb{background:var(--border);border-radius:4px;}
  #count-badge{background:var(--accent);color:#1a1a1a;border-radius:10px;padding:0 6px;font-size:10px;font-weight:700;}
"""


BODY_HTML = r"""<body>
<div id="app">
  <div id="sidebar">
    <h1>OPERA NISAR-DB Viewer
      <button id="theme-toggle" title="Switch to the light theme">&#9788;</button>
      <small>North America &middot; <span id="hdr-count">0</span> frames shown</small>
      <small id="hdr-queried">CMR queried: unknown</small>
    </h1>
    <div id="sidebar-scroll">

      <div class="section">
        <div class="section-head" data-target="sec-daily"><span>GSLC Acquisitions Over Time</span><span class="chev">&#9660;</span></div>
        <div class="section-body" id="sec-daily">
          <div class="stat-line">GSLC granules in CMR over North America, counted by acquisition date across the frames currently shown. Hover a bar for its count.</div>
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
          <label>Color frames by</label>
          <select id="color-by">
            <option value="passDirection">Pass Direction</option>
            <option value="gslc_count" selected>GSLC count in CMR (default)</option>
            <option value="cons_mode">Consistent mode</option>
            <option value="cons_cov">Consistent coverage (full/partial)</option>
            <option value="n_modes">Distinct modes per frame</option>
            <option value="blackout_months" id="opt-blackout" hidden>Blackout duration (months)</option>
            <option value="gslc_modes">GSLC mode (most common)</option>
            <option value="gslc_pols">GSLC polarization (most common)</option>
          </select>
          <label>Fill opacity (<span id="opacity-val">32</span>%)</label>
          <input type="range" id="fill-opacity" min="0" max="100" value="32">
          <button class="btn small" id="btn-reset-style" style="margin-top:8px;">Reset to default</button>
          <div id="colorby-legend" style="margin-top:8px;"></div>
        </div>
      </div>


      <div class="section">
        <div class="section-head" data-target="sec-gslc"><span>GSLC Mode / Polarization</span><span class="chev">&#9660;</span></div>
        <div class="section-body" id="sec-gslc">
          <label>Mode (click to toggle)</label>
          <div class="chip-grid" id="chips-gslc-mode"></div>
          <label>Polarization</label>
          <div class="chip-grid" id="chips-gslc-pol"></div>
        </div>
      </div>


      <div class="section collapsed">
        <div class="section-head" data-target="sec-loc"><span>Location (Track / Frame)</span><span class="chev">&#9660;</span></div>
        <div class="section-body" id="sec-loc">
          <label>Track (e.g. "12" or "10-20" or "12,34,56")</label>
          <input type="text" id="f-track" placeholder="all tracks">
          <label>Frame</label>
          <input type="text" id="f-frame" placeholder="all frames">
          <label>Frame ID (track_frame, e.g. 34_19)</label>
          <input type="text" id="f-id" placeholder="e.g. 34_19">
        </div>
      </div>


      <div class="section">
        <div class="section-head" data-target="sec-flags"><span>Product / Site Flags</span><span class="chev">&#9660;</span></div>
        <div class="section-body" id="sec-flags">
          <div class="check-row"><input type="checkbox" id="f-calval"><label for="f-calval" style="margin:0;color:var(--text)">CalVal frames only</label></div>
          <div class="check-row" id="row-gps" hidden><input type="checkbox" id="f-gps-show"><label for="f-gps-show" style="margin:0;color:var(--text)">Show UNR GPS sites (<span id="gps-count">0</span>)</label></div>
          <div class="stat-line" id="gps-hint" hidden>Nevada Geodetic Laboratory sites; click one for its position time series.</div>
        </div>
      </div>


      <div class="section" id="section-blackout" hidden>
        <div class="section-head" data-target="sec-blackout"><span>Blackout &amp; Reference Dates</span><span class="chev">&#9660;</span></div>
        <div class="section-body" id="sec-blackout">
          <div class="check-row"><input type="checkbox" id="f-blackout"><label for="f-blackout" style="margin:0;color:var(--text)">Frames with a blackout window only</label></div>
          <div class="stat-line">Blackout windows repeat yearly (e.g. seasonal snow). Color frames by "Blackout duration (months)" above; hover or click a frame to see the exact blacked-out ranges and any InSAR reference resets.</div>
          <div id="blackout-summary"></div>
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
  <div id="map">
    <div id="pass-ctrl">
      <label><input type="radio" name="pass" value="all" checked> All</label>
      <label><input type="radio" name="pass" value="Ascending"> Asc</label>
      <label><input type="radio" name="pass" value="Descending"> Desc</label>
    </div>
    <div id="click-ctrl">
      <label><input type="checkbox" id="f-frame-popup" checked> Frame popup</label>
    </div>
    <div id="top-hint">Click a frame to list granules &amp; select &middot; the (i) button toggles hover summaries</div>
    <div id="basemap-ctrl">
      <label><input type="radio" name="basemap" value="light" checked> Light</label>
      <label><input type="radio" name="basemap" value="dark"> Dark</label>
      <label><input type="radio" name="basemap" value="sat"> Satellite</label>
      <label><input type="radio" name="basemap" value="sat2"> Satellite-H</label>
    </div>
    <div id="chart-modal" hidden>
      <div class="chart-card">
        <div class="chart-head">
          <div>
            <div class="pop-title" id="chart-title"></div>
            <div class="chart-sub" id="chart-sub"></div>
          </div>
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
  let refreshSummary = function(){}; // reassigned after DOM ready
  const selected = new Map();        // id -> {feature, color}

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
  const activeChips = { gslcMode:new Set(), gslcPol:new Set() };

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
  // Sequential ramp (low -> high) for numeric color-by fields.
  const SEQ_PALETTE = ["#2c7bb6","#00a6ca","#00ccbc","#90eb9d","#ffff8c","#f9d057","#f29e2e","#e76818","#d7191c"];

  const COLOR_BY_FIELDS = {
    passDirection: { label:"Pass Direction",      key:"passDirection", kind:"cat" },
    gslc_count:    { label:"GSLC count in CMR",    key:"gslc_count_sel", kind:"num" },
    cons_mode:     { label:"Consistent mode",      key:"cons_mode",     kind:"cat" },
    cons_cov:      { label:"Consistent coverage",  key:"cons_cov",      kind:"cat" },
    n_modes:       { label:"Distinct modes",       key:"n_modes",       kind:"num" },
    blackout_months:{ label:"Blackout months",     key:"blackout_months", kind:"num" },
    gslc_modes:    { label:"GSLC mode",            key:"_gslcMode",     kind:"cat" },
    gslc_pols:     { label:"GSLC polarization",    key:"_gslcPol",      kind:"cat" }
  };

  const baseColorMapsCache = {};
  function baseColorMap(propKey){
    if (baseColorMapsCache[propKey]) return baseColorMapsCache[propKey];
    let m;
    if (propKey === "passDirection") {
      m = new Map([["Ascending","#4da3ff"],["Descending","#ff8a4d"]]);
    } else if (propKey === "cons_cov") {
      m = new Map([["F","#4da3ff"],["P","#ff8a4d"],["none","#555a61"]]);
    } else {
      const vals = uniqSorted(FRAME_DATA.features.map(f=>String(f.properties[propKey])));
      m = new Map();
      vals.forEach((v,i)=> m.set(v, v==="none" ? "#6b6b6b" : CAT_PALETTE[i % CAT_PALETTE.length]));
    }
    baseColorMapsCache[propKey] = m;
    return m;
  }

  function numericStops(propKey){
    const vals = FRAME_DATA.features.map(f=>Number(f.properties[propKey]));
    const lo = Math.min(...vals), hi = Math.max(...vals);
    return {lo, hi};
  }

  function colorExpression(fieldName){
    const info = COLOR_BY_FIELDS[fieldName];
    if (info.kind === "num") {
      const {lo, hi} = numericStops(info.key);
      const expr = ["interpolate", ["linear"], ["to-number", ["get", info.key]]];
      if (lo === hi) { return SEQ_PALETTE[Math.floor(SEQ_PALETTE.length/2)]; }
      SEQ_PALETTE.forEach((col,i)=>{
        const v = lo + (hi - lo) * (i / (SEQ_PALETTE.length - 1));
        expr.push(v, col);
      });
      return expr;
    }
    const cmap = baseColorMap(info.key);
    const expr = ["match", ["to-string", ["get", info.key]]];
    cmap.forEach((color, val)=>{ expr.push(val, color); });
    expr.push("#9a9a9a");
    return expr;
  }

  function renderColorByLegend(fieldName){
    const info = COLOR_BY_FIELDS[fieldName];
    const el = document.getElementById("colorby-legend");
    el.innerHTML = "";
    if (info.kind === "num") {
      const {lo, hi} = numericStops(info.key);
      const grad = SEQ_PALETTE.join(",");
      el.innerHTML =
        `<div style="height:12px;border-radius:4px;background:linear-gradient(90deg,${grad});"></div>`+
        `<div style="display:flex;justify-content:space-between;font-size:10px;color:var(--text-dim);margin-top:2px;">`+
        `<span>${lo}</span><span>${info.label}</span><span>${hi}</span></div>`;
      return;
    }
    const colorMap = baseColorMap(info.key);
    colorMap.forEach((color, val)=>{
      const row = document.createElement("div");
      row.className = "check-row"; row.style.margin = "2px 0";
      const picker = document.createElement("input");
      picker.type = "color"; picker.className = "legend-color"; picker.value = color;
      picker.title = `Recolour ${val}`;
      picker.addEventListener("input", ()=>{
        colorMap.set(val, picker.value);   // the cached map is what colorExpression reads
        applyColorBy();
      });
      const label = document.createElement("span");
      label.style.cssText = "font-size:11px;color:var(--text-dim)";
      label.textContent = val;
      row.appendChild(picker);
      row.appendChild(label);
      el.appendChild(row);
    });
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
  function buildGslcChips(){
    activeChips.gslcMode.clear();
    activeChips.gslcPol.clear();
    buildChips("chips-gslc-mode", allModesGslc, activeChips.gslcMode, DEFAULT_GSLC_MODES);
    buildChips("chips-gslc-pol", allPolsGslc, activeChips.gslcPol, DEFAULT_GSLC_POLS);
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

  // Reveal the blackout controls only when the viewer was built with blackout data.
  if (META.has_blackout) {
    const sec = document.getElementById("section-blackout");
    const opt = document.getElementById("opt-blackout");
    if (sec) sec.hidden = false;
    if (opt) opt.hidden = false;
  }

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
  function blackoutDetailBlock(p){
    let html = "";
    if (META.has_blackout && p.has_blackout) {
      const ranges = asArray(p.blackout_ranges).map(r=>`<div class="granule-row">${r}</div>`).join("");
      html += `<div class="pop-row" style="margin-top:6px;">Blackout windows (${p.blackout_label}, ${p.blackout_months} mo):</div>`+
              `<div class="granule-list">${ranges}</div>`;
    }
    if (META.has_reference) {
      const refs = asArray(p.reference_dates);
      html += `<div class="pop-row" style="margin-top:6px;">Reference resets: ${refs.length ? refs.join(", ") : "default (first acquisition)"}</div>`;
    }
    return html;
  }

  function refreshBlackoutSummary(features){
    if (!META.has_blackout) return;
    const el = document.getElementById("blackout-summary");
    if (!el) return;
    const bo = features.filter(f=>f.properties.has_blackout);
    if (!bo.length) { el.innerHTML = `<div class="stat-line">No blacked-out frames shown.</div>`; return; }
    const months = bo.map(f=>f.properties.blackout_months);
    const avg = (months.reduce((a,b)=>a+b,0)/months.length).toFixed(1);
    el.innerHTML = `<div class="summary-grid">
      <div class="stat-tile"><div class="num">${bo.length}</div><div class="cap">blackout frames</div></div>
      <div class="stat-tile"><div class="num">${avg}</div><div class="cap">avg months</div></div>
      <div class="stat-tile"><div class="num">${Math.max(...months)}</div><div class="cap">max months</div></div>
    </div>`;
  }

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
  function matchesArrayFilter(propArr, chipSet){
    if (chipSet.size === 0) return true;
    return propArr.some(v => chipSet.has(v));
  }

  function currentFiltered(){
    const trackSet = parseIntSet(document.getElementById("f-track").value);
    const frameSet = parseIntSet(document.getElementById("f-frame").value);
    const idFilter = document.getElementById("f-id").value.trim().toLowerCase();
    const passVal = document.querySelector('input[name="pass"]:checked').value;
    const calval = document.getElementById("f-calval").checked;
    const blackoutEl = document.getElementById("f-blackout");
    const blackoutOnly = blackoutEl && blackoutEl.checked;
    return FRAME_DATA.features.filter(f=>{
      const p = f.properties;
      if (trackSet && !trackSet.has(p.track)) return false;
      if (frameSet && !frameSet.has(p.frame)) return false;
      if (idFilter && !p.id.toLowerCase().includes(idFilter)) return false;
      if (passVal !== "all" && p.passDirection !== passVal) return false;
      if (calval && !p.isCalVal) return false;
      if (blackoutOnly && !p.has_blackout) return false;
      if (!matchesArrayFilter(p.gslc_modes, activeChips.gslcMode)) return false;
      if (!matchesArrayFilter(p.gslc_pols, activeChips.gslcPol)) return false;
      return true;
    });
  }

  function applyFilters(){
    updateSelectedGslcCounts();
    const filtered = currentFiltered();
    if (map.getSource("frames")) {
      map.getSource("frames").setData({type:"FeatureCollection", features: filtered});
    }
    document.getElementById("hdr-count").textContent = filtered.length;
    applyColorBy();          // the GSLC-count ramp follows the mode/pol chips
    refreshSummary(filtered);
    refreshBlackoutSummary(filtered);
    refreshDailyChart(filtered);
  }

  // ---------- GSLC count under the current mode / polarization chips ----------
  const MODE_POL_BY_FRAME = new Map(
    FRAME_DATA.features.map(f=>{
      const granules = Array.isArray(f.properties.granules) ? f.properties.granules : [];
      return [f.properties.id, granules.map(g=>[g.mode, g.pol])];
    })
  );

  function selectedGslcCount(id){
    const rows = MODE_POL_BY_FRAME.get(id) || [];
    const modes = activeChips.gslcMode, pols = activeChips.gslcPol;
    if (!modes.size && !pols.size) return rows.length;
    let n = 0;
    for (const [mode, pol] of rows) {
      if (modes.size && !modes.has(mode)) continue;
      if (pols.size && !pols.has(pol)) continue;
      n++;
    }
    return n;
  }

  // Colouring by "GSLC count in CMR" reads this, so the ramp answers "how many
  // granules of the kind I selected", not "how many of any kind".
  function updateSelectedGslcCounts(){
    FRAME_DATA.features.forEach(f=>{
      f.properties.gslc_count_sel = selectedGslcCount(f.properties.id);
    });
  }
  updateSelectedGslcCounts();

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

  const BIN_LABEL = {1:"per day", 7:"per week", 30:"per month"};
  let dailyBins = [];

  function refreshDailyChart(features){
    const el = document.getElementById("daily-chart");
    const tip = document.getElementById("daily-tip");
    tip.hidden = true;

    const from = document.getElementById("f-date-start").value;
    const to = document.getElementById("f-date-end").value;
    const counts = new Map();
    features.forEach(f=> (DAY_COUNTS_BY_FRAME.get(f.properties.id)||[]).forEach(([d,n])=>{
      if (from && d < from) return;
      if (to && d > to) return;
      counts.set(d, (counts.get(d)||0) + n);
    }));
    const days = Array.from(counts.keys()).sort();
    dailyBins = [];
    if (!days.length) {
      el.innerHTML = `<div class="stat-line">No GSLC acquisitions in the frames shown${from || to ? " for this date range" : ""}.</div>`;
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
      ` aria-label="GSLC granules ${BIN_LABEL[binDays]}">`+
      `<text class="day-axis" x="0" y="9">peak ${maxN}</text>`+
      `${bars}`+
      `<line class="day-base" x1="0" x2="${W}" y1="${H-padB}" y2="${H-padB}"/>`+
      `<text class="day-axis" x="0" y="${H-4}">${fmt(t0)}</text>`+
      `<text class="day-axis" x="${W}" y="${H-4}" text-anchor="end">${fmt(t1)}</text>`+
      `</svg>`+
      `<div class="stat-line">${total.toLocaleString()} granules &middot; counted ${BIN_LABEL[binDays]}</div>`;
  }

  document.getElementById("daily-chart").addEventListener("mousemove", (e)=>{
    const tip = document.getElementById("daily-tip");
    const bar = e.target.closest ? e.target.closest("rect[data-i]") : null;
    if (!bar) { tip.hidden = true; return; }
    const b = dailyBins[Number(bar.dataset.i)];
    const start = new Date(b.t).toISOString().slice(0,10);
    const end = new Date(b.t + (b.binDays-1)*DAY_MS).toISOString().slice(0,10);
    tip.innerHTML = `<b>${b.n}</b> granule${b.n === 1 ? "" : "s"}<br>`+
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
    document.getElementById(id).addEventListener("change", ()=> refreshDailyChart(currentFiltered())));
  document.getElementById("btn-date-reset").addEventListener("click", ()=>{
    document.getElementById("f-date-start").value = "";
    document.getElementById("f-date-end").value = "";
    refreshDailyChart(currentFiltered());
  });

  ["f-track","f-frame","f-id"].forEach(id=>document.getElementById(id).addEventListener("input", applyFilters));
  document.querySelectorAll('input[name="pass"]').forEach(r=>r.addEventListener("change", applyFilters));
  ["f-calval","f-blackout"].forEach(id=>{
    const el = document.getElementById(id);
    if (el) el.addEventListener("change", applyFilters);
  });

  document.getElementById("btn-clear-filters").addEventListener("click", ()=>{
    document.getElementById("f-track").value = "";
    document.getElementById("f-frame").value = "";
    document.getElementById("f-id").value = "";
    document.querySelector('input[name="pass"][value="all"]').checked = true;
    ["f-calval","f-blackout"].forEach(id=>{ const el=document.getElementById(id); if (el) el.checked=false; });
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
      lbl.innerHTML = `T${p.track}_F${p.frame} <span class="sub">${p.passDirection[0]} &middot; ${p.cons_mode}${p.cons_cov!=="none"?"_"+p.cons_cov:""}</span>`;
      lbl.title = "Click to zoom to frame";
      lbl.onclick = ()=> zoomToFeature(entry.feature);
      const x = document.createElement("button");
      x.className = "li-x"; x.textContent = "✕";
      x.title = "Remove from selection";
      x.onclick = ()=>{ selected.delete(p.id); refreshSelectedSource(); renderSelectedList(); };
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
    refreshSelectedSource(); renderSelectedList();
  }
  function selectFrame(feature, color){
    selected.set(feature.properties.id, {feature, color: color || currentColor});
  }

  document.getElementById("btn-clear-sel").addEventListener("click", ()=>{
    selected.clear(); refreshSelectedSource(); renderSelectedList();
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
        refreshSelectedSource(); renderSelectedList();
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
    const rows = [["track","frame","id","passDirection","color","gslc_count","cons_mode","cons_cov","n_modes","n_full","n_partial","isCalVal","isSNWG","isDNC"]];
    Array.from(selected.values()).forEach(e=>{
      const p = e.feature.properties;
      rows.push([p.track,p.frame,p.id,p.passDirection,e.color,p.gslc_count,p.cons_mode,p.cons_cov,
        p.n_modes,p.n_full,p.n_partial,p.isCalVal,p.isSNWG,p.isDNC]);
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
      "carto-light": { type:"raster", tiles:["https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png","https://b.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png","https://c.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png"], tileSize:256, attribution:"&copy; OpenStreetMap &copy; CARTO" },
      "carto-dark": { type:"raster", tiles:["https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png","https://b.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png","https://c.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png"], tileSize:256, attribution:"&copy; OpenStreetMap &copy; CARTO" },
      "esri-sat": { type:"raster", tiles:["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"], tileSize:256, attribution:"Esri World Imagery" },
      "google-hybrid": { type:"raster", tiles:["https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}"], tileSize:256, attribution:"Google" }
    },
    layers: [
      { id:"bm-light", type:"raster", source:"carto-light", layout:{visibility:"visible"} },
      { id:"bm-dark", type:"raster", source:"carto-dark", layout:{visibility:"none"} },
      { id:"bm-sat", type:"raster", source:"esri-sat", layout:{visibility:"none"} },
      { id:"bm-sat2", type:"raster", source:"google-hybrid", layout:{visibility:"none"} }
    ]
  };

  const map = new maplibregl.Map({
    container: "map",
    style: style,
    center: [-100, 40],
    zoom: 1.4,
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

  const BASEMAP_LAYERS = {light:"bm-light", dark:"bm-dark", sat:"bm-sat", sat2:"bm-sat2"};
  document.querySelectorAll('input[name="basemap"]').forEach(r=>{
    r.addEventListener("change", ()=>{
      Object.entries(BASEMAP_LAYERS).forEach(([value, layer])=>
        map.setLayoutProperty(layer, "visibility", value === r.value ? "visible" : "none"));
    });
  });

  // ---------- light / dark theme ----------
  document.getElementById("f-frame-popup").addEventListener("change", (e)=>{
    if (!e.target.checked) document.querySelectorAll(".maplibregl-popup").forEach(el=>el.remove());
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
    const rows = [["frame_id","track","frame","pass","direction","date","mode","coverage","polarization","cycle","granule_id"]];
    granules.forEach(g=> rows.push([p.id,p.track,p.frame,p.passDirection,g.dir,g.date,g.mode,g.cov,g.pol,g.cycle,g.gid]));
    return toCsv(rows);
  }

  function granulePopupHtml(p){
    const granules = parseGranules(p);
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
      <div class="pop-title">Track ${p.track} / Frame ${p.frame} (${p.id})</div>
      <div class="pop-row">Pass: ${p.passDirection} &middot; consistent: ${p.cons_mode}${p.cons_cov!=="none"?"_"+p.cons_cov:""}</div>
      <div class="pop-row">GSLC in CMR: ${p.gslc_count} &middot; ${p.n_modes} mode(s) &middot; ${p.n_full}F / ${p.n_partial}P</div>
      ${blackoutDetailBlock(p)}
      <div class="pop-actions">
        <button class="btn small primary" id="pop-select">${isSel ? "Remove from selection" : "Add to selection"}</button>
        <button class="btn small" id="pop-granules"${granules.length ? "" : " disabled"}>Show granules (${granules.length})</button>
        <button class="btn small" id="pop-csv"${granules.length ? "" : " disabled"}>Export CSV</button>
        <button class="btn small" id="pop-plot"${granules.length ? "" : " disabled"}>Show plot</button>
      </div>
      <div id="pop-granule-panel" hidden>${dirChips}<div class="granule-list" id="pop-granule-list">${rows}</div></div>`;
  }

  function granuleRowsHtml(granules){
    return granules.map(g=>
      `<div class="granule-row"><span class="gdate">${g.date}</span> `+
      `<span class="gmode">${g.mode}_${g.cov}</span> ${g.pol} ${g.dir} c${g.cycle}<br>${g.gid}</div>`
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
    const csvBtn = document.getElementById("pop-csv");
    if (csvBtn) csvBtn.addEventListener("click", ()=>
      downloadBlob(granuleCsv(p, granules), `nisar_granules_${p.id}.csv`, "text/csv"));
    const plotBtn = document.getElementById("pop-plot");
    if (plotBtn) plotBtn.addEventListener("click", ()=> showModeTimeline(p, granules));
  }

  // ---------- acquisition timeline: mode (y) vs. acquisition date (x) ----------
  // Categorical steps chosen for the dark panel surface; adjacent pairs clear the
  // colour-vision-deficiency separation floor. Every row is also directly
  // labelled, so identity never rests on colour alone.
  const CHART_PALETTE = ["#3987e5","#d95926","#199e70","#c98500","#d55181","#008300","#9085e9","#e66767"];
  const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  const DAY_MS = 86400000;
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

  function modeTimelineSvg(granules){
    chartPoints = granules.filter(g=>g.date).map(g=>({
      t: Date.parse(`${g.date}T00:00:00Z`), key: `${g.mode}_${g.cov}`, dir: g.dir, g
    })).sort((a,b)=>a.t-b.t);
    if (!chartPoints.length) return `<div class="stat-line">No dated granules to plot.</div>`;

    const rows = uniqSorted(chartPoints.map(pt=>pt.key));
    const padL = 96, padR = 24, padT = 10, padB = 30, rowH = 34;
    const W = Math.min(720, Math.max(420, window.innerWidth - 140));
    const H = padT + rows.length * rowH + padB;
    let t0 = chartPoints[0].t, t1 = chartPoints[chartPoints.length-1].t;
    if (t1 === t0) { t0 -= 15 * DAY_MS; t1 += 15 * DAY_MS; }
    const pad = (t1 - t0) * 0.03;
    t0 -= pad; t1 += pad;
    const xOf = t => padL + (t - t0) / (t1 - t0) * (W - padL - padR);
    const yOf = key => padT + rows.indexOf(key) * rowH + rowH / 2;

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
      const x = xOf(pt.t), y = yOf(pt.key), fill = modeColor(pt.key);
      if (pt.dir === "D") {
        const r = 5.2;
        const pts = [[x, y-r],[x+r, y],[x, y+r],[x-r, y]].map(c=>c.map(v=>v.toFixed(1)).join(",")).join(" ");
        return `<polygon class="chart-dot" data-i="${i}" points="${pts}" fill="${fill}"/>`;
      }
      return `<circle class="chart-dot" data-i="${i}" cx="${x.toFixed(1)}" cy="${y}" r="4.5" fill="${fill}"/>`;
    }).join("");
    const shapeKey = uniqSorted(chartPoints.map(pt=>pt.dir)).map(d=>
      d === "D" ? "&#9670; descending" : d === "A" ? "&#9679; ascending" : `? ${d}`).join(" &middot; ");

    return `<svg id="chart-svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img"
      aria-label="GSLC acquisitions by mode over time">${ticks}${lanes}${dots}</svg>`+
      `<div class="chart-sub">${shapeKey}</div>`;
  }

  function showModeTimeline(p, granules){
    const dated = granules.filter(g=>g.date).map(g=>g.date).sort();
    document.getElementById("chart-title").textContent =
      `Track ${p.track} / Frame ${p.frame} - acquisitions by mode`;
    document.getElementById("chart-sub").textContent = dated.length
      ? `${dated.length} GSLC granules - ${dated[0]} to ${dated[dated.length-1]}`
      : "No dated GSLC granules";
    document.getElementById("chart-body").innerHTML = modeTimelineSvg(granules);
    document.getElementById("chart-modal").hidden = false;
  }

  function hideModeTimeline(){
    document.getElementById("chart-modal").hidden = true;
    document.getElementById("chart-tip").hidden = true;
  }

  document.getElementById("chart-close").addEventListener("click", hideModeTimeline);
  document.getElementById("chart-modal").addEventListener("click", (e)=>{
    if (e.target.id === "chart-modal") hideModeTimeline();
  });
  document.addEventListener("keydown", (e)=>{ if (e.key === "Escape") hideModeTimeline(); });

  const chartTip = document.getElementById("chart-tip");
  document.getElementById("chart-body").addEventListener("mousemove", (e)=>{
    // Ascending dots are circles and descending ones polygons, so match the class.
    const dot = e.target.closest ? e.target.closest(".chart-dot[data-i]") : null;
    if (!dot) { chartTip.hidden = true; return; }
    const pt = chartPoints[Number(dot.dataset.i)];
    chartTip.innerHTML = `<b>${pt.g.date}</b> &middot; ${pt.key}<br>`+
      `<span class="tdim">${pt.g.pol} &middot; ${DIR_LABEL[pt.dir] || pt.dir} &middot; cycle ${pt.g.cycle}</span><br>`+
      `<span class="tdim">${pt.g.gid}</span>`;
    // Unhide first: a display:none tip measures 0 wide and would defeat the clamp.
    chartTip.hidden = false;
    const card = chartTip.parentElement.getBoundingClientRect();
    chartTip.style.left = `${Math.min(e.clientX - card.left + 12, card.width - chartTip.offsetWidth - 8)}px`;
    chartTip.style.top = `${e.clientY - card.top + 14}px`;
  });
  document.getElementById("chart-body").addEventListener("mouseleave", ()=>{ chartTip.hidden = true; });

  map.on("load", ()=>{
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

    // ---------- color-by / opacity ----------
    applyColorBy = function(){
      const field = document.getElementById("color-by").value;
      const colorExpr = colorExpression(field);
      map.setPaintProperty("frames-fill", "fill-color", colorExpr);
      map.setPaintProperty("frames-outline", "line-color", colorExpr);
      const globalOpacity = Number(document.getElementById("fill-opacity").value) / 100;
      map.setPaintProperty("frames-fill", "fill-opacity", globalOpacity);
      renderColorByLegend(field);
    };
    document.getElementById("color-by").addEventListener("change", applyColorBy);
    document.getElementById("fill-opacity").addEventListener("input", (e)=>{
      document.getElementById("opacity-val").textContent = e.target.value;
      applyColorBy();
    });
    document.getElementById("btn-reset-style").addEventListener("click", ()=>{
      Object.keys(baseColorMapsCache).forEach(k=>delete baseColorMapsCache[k]);
      document.getElementById("color-by").value = "gslc_count";
      document.getElementById("fill-opacity").value = 32;
      document.getElementById("opacity-val").textContent = 32;
      applyColorBy();
    });
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
      cancelHoverClose();
      const p = e.features[0].properties;
      popup.setLngLat(e.lngLat).setHTML(`
        <div class="pop-title">Track ${p.track} / Frame ${p.frame}</div>
        <div class="pop-row">Pass: ${p.passDirection} &middot; ${p.cons_mode}${p.cons_cov!=="none"?"_"+p.cons_cov:""}</div>
        <div class="pop-row">GSLC in CMR: ${p.gslc_count} &middot; ${p.n_modes} mode(s)</div>
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
    const clickPopup = new maplibregl.Popup({ closeButton:true, closeOnClick:false, maxWidth:"340px" });
    map.on("click", "frames-fill", (e)=>{
      if (!document.getElementById("f-frame-popup").checked) return;
      // A GPS marker always sits inside some frame; a click on one belongs to it.
      if (map.getLayer("gps-points") &&
          map.queryRenderedFeatures(e.point, {layers:["gps-points"]}).length) return;
      const feature = idToFeature(e.features[0].properties.id);
      if (!feature) return;
      cancelHoverClose();
      popup.remove();
      clickPopup.setLngLat(e.lngLat).setHTML(granulePopupHtml(feature.properties)).addTo(map);
      wireGranulePopup(feature);
    });
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
        help="GSLC catalog CSV from 'nisar-db create-catalog'; use instead of "
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

    frame_data = build_frame_data(gdf, catalog, consistent, blackout, reference)
    n_with = sum(1 for f in frame_data["features"] if f["properties"]["gslc_count"] > 0)

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
    }

    gps_sites = load_gps_sites(None if args.no_gps else args.gps_source)
    if gps_sites["features"]:
        print(f"  {len(gps_sites['features'])} UNR GPS sites")

    html = render_html(frame_data, meta, gps_sites)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html)
    size_mb = args.output.stat().st_size / 1e6
    print(
        f"Wrote {args.output} ({size_mb:.1f} MB); {n_with}/{meta['n_frames']} frames have GSLC"
    )


if __name__ == "__main__":
    main()
