#!/usr/bin/env python
"""Build a frame-viewer page for any part of the world, on demand.

The published viewer covers the OPERA North America frames and is rebuilt
weekly. This module rebuilds it locally for one of three scopes, straight from
CMR:

* ``na`` -- the OPERA North America frames (the published page's scope);
* ``globe`` -- every frame of the NISAR TrackFrame database (~30,000);
* ``bbox`` -- the frames intersecting a ``west, south, east, north`` box.

Frames keep the ``frame_idx`` OPERA uses (the TrackFrame database's row index),
so a North American frame has the same number in every scope. GSLC and GUNW
granules come from one CMR search each (the whole archive, or the box); blackout
dates, granule flags and QA metrics are attached from the repo's caches where
they cover a frame or granule; on request, the flags and QA metrics of granules
the caches miss are collected first into local caches beside the page, so the
repo's own caches are left untouched. Both come from one small ``QA_STATS.h5``
download per granule (``collect_granule_qa.py``, Earthdata login in
``~/.netrc``), with the orbit type taken from the CMR search. The consistent
mode is computed from the catalog.

``scripts/qa_browse_server.py`` runs it when the viewer's search button asks;
it can also be run by hand::

    python scripts/build_local_view.py --scope bbox --bbox -125,32,-114,42 \\
        --output view.html

"""

from __future__ import annotations

import argparse
import gzip
import json
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import pandas as pd
import shapely
from shapely.geometry import box

import generate_scope_viewer as gen
from collect_granule_qa import catalog_flags, release_group, rfi_by_release

REPO = Path(__file__).resolve().parents[1]
BLACKOUT_JSON = REPO / "catalog" / "opera-nisar-disp-blackout-dates.json"
GRANULE_FLAGS = REPO / "catalog" / "granule_flags.json.gz"
GRANULE_QA = REPO / "catalog" / "granule_qa.json.gz"
SCOPES = ("na", "globe", "bbox")
#: Prefix of the last output line, which carries the built page's summary.
META_LINE = "@@meta "
SCOPE_LABELS = {"na": "OPERA North America", "globe": "Globe", "bbox": "Screen view"}
# Coordinates to ~10 m: plenty for a frame outline, and it keeps a 30,000-frame
# page from carrying full float precision.
GRID = 1e-4


def select_frames(
    trackframe_gpkg: Path,
    scope: str,
    bbox: tuple[float, ...] | None = None,
    land_only: bool = False,
) -> gpd.GeoDataFrame:
    """Return the frames of ``scope`` with the columns the viewer reads.

    Parameters
    ----------
    trackframe_gpkg : Path
        The global NISAR TrackFrame GeoPackage.
    scope : str
        ``na``, ``globe`` or ``bbox``.
    bbox : tuple of float, optional
        ``west, south, east, north`` for the ``bbox`` scope.
    land_only : bool
        Drop the frames with no land (the database's ``hasLand``).

    Returns
    -------
    geopandas.GeoDataFrame
        Frames in lon/lat with ``frame_idx`` (the database's row index) and
        ``direction``.

    """
    from nisar_db.geodb import filter_frames_to_na

    gdf = gpd.read_file(trackframe_gpkg)
    if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
        gdf = gdf.to_crs(4326)
    gdf["frame_idx"] = gdf.index
    if scope == "na":
        gdf = filter_frames_to_na(gdf)
    elif scope == "bbox":
        if bbox is None:
            raise ValueError("the bbox scope needs a bbox")
        gdf = gdf[gdf.intersects(box(*bbox))]
    elif scope != "globe":
        raise ValueError(f"unknown scope {scope!r}; expected one of {SCOPES}")
    if land_only:
        gdf = gdf[gdf["hasLand"].astype(bool)]
    gdf = gdf.copy()
    gdf["direction"] = gdf["passDirection"].str[0]
    gdf["geometry"] = shapely.set_precision(gdf.geometry.values, GRID)
    return gdf


def with_catalog_flags(
    cached: dict[str, dict], from_catalog: dict[str, dict]
) -> dict[str, dict]:
    """Granule flags: the cached ones over those the catalog gives.

    The catalog's ``j f o`` gain the RFI mitigation flag of the granule's
    release, where the cached flags agree on one, so a granule never read
    shows four of its six flags.

    Examples
    --------
    >>> gslc = ("NISAR_L2_PR_GSLC_031_155_D_084_4005_DHDH_A_20260928T231125_"
    ...         "20260928T231159_P05023_N_F_J_001")
    >>> old = gslc.replace("_001", "_002")
    >>> with_catalog_flags({old: {"r": 0, "m": 1}}, {gslc: {"j": 1, "o": "MOE"}})[gslc]
    {'j': 1, 'o': 'MOE', 'r': 0}

    """
    rfi = rfi_by_release(cached)
    out = {}
    for gid in {**from_catalog, **cached}:
        value = dict(from_catalog.get(gid, {}))
        if release_group(gid) in rfi:
            value["r"] = rfi[release_group(gid)]
        value.update(cached.get(gid, {}))
        out[gid] = {k: value[k] for k in "jformd" if k in value}
    return out


def search_gslc(
    bbox: tuple[float, ...] | None, workdir: Path, report: object = None
) -> tuple[pd.DataFrame, dict[str, dict]]:
    """Search CMR for GSLC granules: the viewer's catalog and their CMR flags."""
    from nisar_db.gslc_catalog import parse_gslc_list, write_catalog_csv
    from nisar_db.search.cmr import search_nisar_products
    from nisar_db.search.frames import products_to_dataframe

    products = search_nisar_products(
        bbox=bbox, product_type="GSLC", max_results=0, report=report
    )
    found = products_to_dataframe(products)
    listing = workdir / "gslc_search.csv"
    found[["name", "url"]].to_csv(listing, index=False)
    parsed, _failed = parse_gslc_list(listing)
    catalog_csv = workdir / "gslc_catalog.csv"
    write_catalog_csv(parsed, catalog_csv)
    return gen.load_gslc_catalog_csv(catalog_csv), catalog_flags(products)


def gunw_rows(names: list[str]) -> pd.DataFrame:
    """Turn GUNW granule names into the viewer's GUNW catalog rows.

    The fields are those ``load_gunw_catalog`` reads from the GUNW catalog JSON:
    track 5, direction 6, frame 7, mode 9, polarization 10, reference start 11,
    secondary start 13 and coverage 17.

    Examples
    --------
    >>> name = ("NISAR_L2_PR_GUNW_030_155_D_084_031_4000_SH_20260916T231125_"
    ...         "20260916T231159_20260928T231125_20260928T231159_P05023_N_F_J_001")
    >>> gunw_rows([name]).iloc[0][["track", "ref", "sec", "mode", "coverage"]].tolist()
    [155, '2026-09-16', '2026-09-28', '4000', 'F']

    """
    rows = []
    for name in names:
        parts = str(name).split("_")
        if len(parts) < 18 or parts[3] != "GUNW":
            continue
        rows.append(
            {
                "track": int(parts[5]),
                "frame": int(parts[7]),
                "direction": parts[6],
                "ref": gen._iso_date(parts[11][:8]),
                "sec": gen._iso_date(parts[13][:8]),
                "mode": parts[9],
                "coverage": parts[17],
                "polarization": parts[10],
                "granule_id": name,
            }
        )
    return pd.DataFrame(rows, columns=gen._GUNW_COLUMNS).drop_duplicates("granule_id")


def search_gunw(
    bbox: tuple[float, ...] | None, report: object = None
) -> tuple[pd.DataFrame, dict[str, dict]]:
    """Search CMR for GUNW granules: the viewer's catalog and their CMR flags."""
    from nisar_db.search.cmr import search_nisar_products

    products = search_nisar_products(
        bbox=bbox, product_type="GUNW", max_results=0, report=report
    )
    return gunw_rows([p.name for p in products]), catalog_flags(products)


#: Files of a saved catalog; ``catalog.json`` says when and over what it was searched.
SAVED_GSLC = "gslc_catalog.csv.gz"
SAVED_GUNW = "gunw_catalog.csv.gz"
SAVED_FLAGS = "cmr_flags.json.gz"
SAVED_INFO = "catalog.json"


def save_catalog(
    directory: Path,
    gslc: pd.DataFrame,
    gunw: pd.DataFrame,
    cmr_flags: dict[str, dict],
    info: dict,
) -> None:
    """Save a build's CMR search results, so later builds can skip CMR.

    ``info`` records when the search ran and the box it covered (``None`` for
    the whole archive), plus anything else worth keeping, such as the CMR
    reports.
    """
    directory.mkdir(parents=True, exist_ok=True)
    gslc[gen._CATALOG_COLUMNS].to_csv(directory / SAVED_GSLC, index=False)
    gunw[gen._GUNW_COLUMNS].to_csv(directory / SAVED_GUNW, index=False)
    with gzip.open(directory / SAVED_FLAGS, "wt") as fh:
        json.dump(cmr_flags, fh, separators=(",", ":"))
    info = {
        **info,
        "n_gslc": int(len(gslc)),
        "n_gunw": int(len(gunw)),
    }
    (directory / SAVED_INFO).write_text(json.dumps(info, indent=1) + "\n")


def load_catalog(
    directory: Path, bbox: tuple[float, ...] | None
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, dict], dict]:
    """Load a catalog :func:`save_catalog` wrote, for a page covering ``bbox``.

    Raises
    ------
    ValueError
        When the catalog was searched over a box that does not contain
        ``bbox`` (``None`` meaning the whole archive).

    """
    info = json.loads((directory / SAVED_INFO).read_text())
    saved = info.get("bbox")
    if saved is not None and (
        bbox is None or not box(*saved).buffer(1e-9).contains(box(*bbox))
    ):
        raise ValueError(
            f"the saved catalog in {directory} covers only the box {saved}, "
            f"not {'the whole archive' if bbox is None else list(bbox)}"
        )
    gslc = gen.load_gslc_catalog_csv(directory / SAVED_GSLC)
    gunw = pd.read_csv(
        directory / SAVED_GUNW,
        dtype={c: str for c in gen._GUNW_COLUMNS if c not in ("track", "frame")},
    )
    with gzip.open(directory / SAVED_FLAGS, "rt") as fh:
        cmr_flags = json.load(fh)
    return gslc, gunw, cmr_flags, info


def on_frames(rows: pd.DataFrame, frames: gpd.GeoDataFrame) -> pd.DataFrame:
    """The catalog rows of the frames in ``frames``, by track, frame and direction.

    Examples
    --------
    >>> frames = pd.DataFrame({"track": [1], "frame": [10], "direction": ["A"]})
    >>> rows = pd.DataFrame({"track": [1, 1], "frame": [10, 10],
    ...                      "direction": ["A", "D"], "granule_id": ["a", "d"]})
    >>> list(on_frames(rows, frames)["granule_id"])
    ['a']

    """
    keys = ["track", "frame", "direction"]
    wanted = pd.MultiIndex.from_frame(frames[keys].astype({"track": int, "frame": int}))
    have = pd.MultiIndex.from_frame(rows[keys].astype({"track": int, "frame": int}))
    return rows[have.isin(wanted)]


def merged_cache(repo_cache: Path, local_cache: Path | None) -> dict[str, dict]:
    """Read a per-granule cache, the repo's updated with a local one."""
    cache = gen.load_granule_flags(repo_cache) if repo_cache.exists() else {}
    if local_cache is not None and local_cache.exists():
        cache.update(gen.load_granule_flags(local_cache))
    return cache


def to_collect(
    ids: list[str],
    flags: dict[str, dict],
    qa: dict[str, dict],
    collect_flags: bool,
    collect_qa: bool,
) -> list[str]:
    """The granules of ``ids`` whose requested flags or QA metrics are not cached.

    Examples
    --------
    >>> to_collect(["A", "B", "B", "C"], {"A": {}, "B": {}}, {"A": {}}, True, False)
    ['C']
    >>> to_collect(["A", "B", "C"], {"A": {}, "B": {}}, {"A": {}}, True, True)
    ['B', 'C']

    """
    return [
        g
        for g in dict.fromkeys(ids)
        if (collect_flags and g not in flags) or (collect_qa and g not in qa)
    ]


def build_view(
    scope: str,
    output: Path,
    trackframe_gpkg: Path,
    bbox: tuple[float, ...] | None = None,
    progress: Callable[[str], None] = print,
    collect_flags: bool = False,
    collect_qa: bool = False,
    cache_dir: Path | None = None,
    land_only: bool = False,
    catalog_dir: Path | None = None,
    save_catalog_dir: Path | None = None,
) -> dict:
    """Search CMR, build the frame data for ``scope`` and write the page.

    Parameters
    ----------
    scope : str
        ``na``, ``globe`` or ``bbox``.
    output : Path
        HTML file to write.
    trackframe_gpkg : Path
        The global NISAR TrackFrame GeoPackage.
    bbox : tuple of float, optional
        ``west, south, east, north`` for the ``bbox`` scope.
    progress : callable
        Called with a short message as each step starts.
    collect_flags, collect_qa : bool
        Read the granule flags / QA metrics the caches miss before building
        (needs ``~/.netrc`` and ``cache_dir``).
    cache_dir : Path, optional
        Where the local flag and QA caches live.
    land_only : bool
        Leave out the frames with no land.
    catalog_dir : Path, optional
        Build from a catalog saved earlier instead of searching CMR.
    save_catalog_dir : Path, optional
        Save this build's CMR search results here for later builds.

    Returns
    -------
    dict
        The page's ``META``.

    """
    progress("Selecting frames")
    gdf = select_frames(trackframe_gpkg, scope, bbox, land_only)
    gdf["isCalVal"] = gen.flag_calval_frames(gdf, gpd.read_file(gen.CALVAL_SITES))
    rollout_options, gdf["rollout"], gdf["rollout_regions"] = gen.rollout_by_frame(
        gdf, gen.ROLLOUT_REGIONS
    )
    rollout_regions = gen.rollout_overview(
        gdf, gen.ROLLOUT_REGIONS, rollout_options, list(gdf["rollout"])
    )
    search_box = tuple(bbox) if scope == "bbox" and bbox is not None else None

    if catalog_dir is not None:
        progress(f"Reading the saved catalog in {catalog_dir}")
        catalog, gunw, cmr_flags, saved_info = load_catalog(catalog_dir, search_box)
        queried_at = saved_info["queried_at"]
        source = f"CMR (saved catalog, searched {queried_at[:10]})"
    else:
        from nisar_db.search.cmr import CMRSearchReport

        queried_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        source = "CMR (local search)"
        reports = {"GSLC": CMRSearchReport(), "GUNW": CMRSearchReport()}
        with tempfile.TemporaryDirectory() as tmp:
            progress(f"Searching CMR for GSLC granules ({len(gdf)} frames)")
            catalog, cmr_flags = search_gslc(search_box, Path(tmp), reports["GSLC"])
        progress(f"Searching CMR for GUNW granules ({len(catalog)} GSLC found)")
        gunw, gunw_flags = search_gunw(search_box, reports["GUNW"])
        cmr_flags.update(gunw_flags)
        if save_catalog_dir is not None:
            progress(f"Saving the catalog to {save_catalog_dir}")
            save_catalog(
                save_catalog_dir,
                catalog,
                gunw,
                cmr_flags,
                {
                    "queried_at": queried_at,
                    "bbox": list(search_box) if search_box else None,
                    "cmr_reports": {k: r.to_dict() for k, r in reports.items()},
                },
            )

    # The globe and North America searches cover the whole archive; only the
    # granules of this page's frames are worth collecting.
    ids = list(on_frames(catalog, gdf)["granule_id"]) + list(
        on_frames(gunw, gdf)["granule_id"]
    )
    local = Path(cache_dir) if cache_dir is not None else None
    local_flags = local / "granule_flags.json.gz" if local else None
    local_qa = local / "granule_qa.json.gz" if local else None
    flags = merged_cache(GRANULE_FLAGS, local_flags)
    qa = merged_cache(GRANULE_QA, local_qa)
    missing = to_collect(ids, flags, qa, collect_flags, collect_qa)
    if collect_flags or collect_qa:
        if local_qa is None or local_flags is None:
            raise ValueError("collecting needs a cache directory to write to")
        if missing:
            from collect_granule_qa import collect

            progress(f"Collecting flags and QA for {len(missing):,} granules")
            # Each QA file fills both caches, so both are kept up to date
            # whichever was asked for.
            collect(
                missing,
                local_qa,
                progress=lambda m: progress(f"flags + QA: {m.strip()}"),
                flags_output=local_flags,
                orbits={g: v["o"] for g, v in cmr_flags.items() if "o" in v},
                known_flags=flags,
            )
            flags = merged_cache(GRANULE_FLAGS, local_flags)
            qa = merged_cache(GRANULE_QA, local_qa)
        else:
            progress("All flags and QA metrics already cached")
    progress(f"Building {len(gdf)} frames ({len(gunw)} GUNW found)")
    blackout = (
        gen.load_period_json(BLACKOUT_JSON, "blackout_dates", "data")
        if BLACKOUT_JSON.exists()
        else None
    )
    frame_data = gen.build_frame_data(gdf, catalog, None, blackout, None, gunw=gunw)
    flags = with_catalog_flags(flags, cmr_flags)
    n_flagged = gen.attach_granule_flags(frame_data, flags) if flags else 0
    n_qa = gen.attach_granule_qa(frame_data, qa) if qa else 0

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n_with = sum(1 for f in frame_data["features"] if f["properties"]["gslc_count"])
    meta = {
        "title": "OPERA NISAR-DB Viewer",
        "generated_at": now,
        "catalog_queried_at": queried_at,
        "catalog_source": source,
        "catalog_kind": "cmr",
        "n_frames": len(frame_data["features"]),
        "n_frames_with_gslc": n_with,
        "n_granules": sum(
            len(f["properties"]["granules"]) for f in frame_data["features"]
        ),
        "n_catalog_rows": int(len(catalog)),
        "consistent_source": "computed",
        "has_blackout": blackout is not None,
        "has_reference": False,
        "has_gunw": True,
        "has_flags": n_flagged > 0,
        "has_qa": n_qa > 0,
        "rollout_options": rollout_options,
        "rollout_source": gen.ROLLOUT_REGIONS.name,
        "n_gunw": sum(
            f["properties"].get("gunw_count", 0) for f in frame_data["features"]
        ),
        "view_scope": scope,
        "view_label": SCOPE_LABELS[scope],
        "view_bbox": list(bbox) if scope == "bbox" and bbox is not None else None,
    }

    progress("Reading the UNR GPS sites")
    gps_sites = gen.load_gps_sites(gen.NGL_STATION_MAP)

    progress("Writing the page")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(gen.render_html(frame_data, meta, gps_sites, rollout_regions))
    progress(
        f"Done: {meta['n_frames']} frames, {n_with} with GSLC, "
        f"{meta['n_gunw']} GUNW ({output.stat().st_size / 1e6:.1f} MB)"
    )
    return meta


def main(argv: list[str] | None = None) -> None:
    """Command-line entry point."""
    from nisar_db.geodb import get_trackframe_db

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scope", choices=SCOPES, default="na")
    parser.add_argument("--bbox", help="west,south,east,north for --scope bbox")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--trackframe-gpkg",
        type=Path,
        default=None,
        help="TrackFrame GeoPackage; downloaded next to --output when omitted.",
    )
    parser.add_argument(
        "--collect-flags",
        action="store_true",
        help="Read the granule flags the caches miss (needs --cache-dir, ~/.netrc).",
    )
    parser.add_argument(
        "--collect-qa",
        action="store_true",
        help="Read the QA metrics the caches miss (needs --cache-dir, ~/.netrc).",
    )
    parser.add_argument(
        "--land-only",
        action="store_true",
        help="Leave out the frames with no land (about half of the globe's).",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Local flag / QA caches, merged with the repository's.",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=None,
        help="Build from a catalog saved with --save-catalog instead of CMR.",
    )
    parser.add_argument(
        "--save-catalog",
        type=Path,
        default=None,
        help="Save this build's CMR search results to this directory.",
    )
    args = parser.parse_args(argv)
    if args.catalog is not None and args.save_catalog is not None:
        parser.error("--catalog and --save-catalog cannot be used together")
    bbox = tuple(float(v) for v in args.bbox.split(",")) if args.bbox else None

    def say(msg: str) -> None:
        # One line per step: the viewer helper reads them as progress.
        print(msg, flush=True)

    if args.trackframe_gpkg is None:
        say("Fetching the NISAR frame database")
    gpkg = args.trackframe_gpkg or get_trackframe_db(output_dir=args.output.parent)
    meta = build_view(
        args.scope,
        args.output,
        gpkg,
        bbox,
        progress=say,
        collect_flags=args.collect_flags,
        collect_qa=args.collect_qa,
        cache_dir=args.cache_dir,
        land_only=args.land_only,
        catalog_dir=args.catalog,
        save_catalog_dir=args.save_catalog,
    )
    say(META_LINE + json.dumps({"n_frames": meta["n_frames"]}))


if __name__ == "__main__":
    main()
