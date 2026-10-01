#!/usr/bin/env python
"""Refresh a built viewer's markup, styles and app code in place.

``generate_scope_viewer.py`` needs the frame GeoPackage and a GSLC catalog, which
are not in the repository, so a checked-in viewer cannot simply be rebuilt after
a UI change. This script swaps the generated parts of an existing HTML file --
``APP_CSS``, ``BODY_HTML``, ``APP_JS`` and the GPS site collection -- for the
current ones, re-derives the frames' CalVal flag from the current site list and,
given a granule flag cache, attaches the per-granule flags. The vendored MapLibre
bundle and the rest of the embedded frame data are left untouched.

Examples
--------
Update the copies tracked in the repository::

    python scripts/sync_viewer_html.py \\
        scripts/opera_nisar_db_viewer.html docs/assets/opera_nisar_db_viewer.html

"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import geopandas as gpd

import generate_scope_viewer as gen

#: Marks the end of the vendored MapLibre stylesheet and the start of ours.
_STYLE_SPLIT = "</style>\n<style>"


def _replace_app_css(html: str) -> str:
    head, sep, rest = html.partition(_STYLE_SPLIT)
    if not sep:
        raise ValueError("no second <style> block: not a generated viewer")
    _, close, tail = rest.partition("</style>")
    return f"{head}{sep}{gen.APP_CSS}{close}{tail}"


def _replace_body(html: str) -> str:
    head, sep, rest = html.partition("</head>\n")
    if not sep:
        raise ValueError("no </head>: not a generated viewer")
    _, script, tail = rest.partition("\n<script>")
    return f"{head}{sep}{gen.BODY_HTML}{script}{tail}"


def _replace_app_js(html: str) -> str:
    marker = "<script>"
    start = html.rindex(marker) + len(marker)
    end = html.index("</script>", start)
    return f"{html[:start]}{gen.APP_JS}{html[end:]}"


def _upsert_gps_data(html: str, gps_sites: dict) -> str:
    payload = json.dumps(gps_sites, separators=(",", ":"))
    if "const UNR_GPS_DATA" in html:
        # The payload is one line of JSON, with or without a trailing newline.
        return re.sub(
            r"const UNR_GPS_DATA = [^\n]*;",
            lambda _: f"const UNR_GPS_DATA = {payload};",
            html,
            count=1,
        )
    # Append to the data script, which is the one holding META.
    anchor = re.search(r"(const META = .*?;)", html, flags=re.S)
    if anchor is None:
        raise ValueError("no META block: not a generated viewer")
    return html.replace(
        anchor.group(1), f"{anchor.group(1)}\nconst UNR_GPS_DATA = {payload};", 1
    )


def _refresh_frame_data(
    html: str, calval_sites: gpd.GeoDataFrame, granule_flags: dict[str, dict] | None
) -> str:
    opener = "const FRAME_DATA = "
    start = html.index(opener) + len(opener)
    end = html.index(";\nconst META", start)
    frame_data = json.loads(html[start:end])
    frames = gpd.GeoDataFrame.from_features(frame_data["features"], crs=4326)
    flags = gen.flag_calval_frames(frames, calval_sites)
    for feature, flag in zip(frame_data["features"], flags, strict=True):
        feature["properties"]["isCalVal"] = bool(flag)
    if granule_flags is not None:
        n_flagged = gen.attach_granule_flags(frame_data, granule_flags)
        print(f"  flags for {n_flagged} granules / interferograms")
    payload = json.dumps(frame_data, separators=(",", ":"))
    html = f"{html[:start]}{payload}{html[end:]}"
    if granule_flags is None:
        return html
    # META is one line of JSON; the viewer only offers the flag views when it
    # says the page carries flags.
    match = re.search(r"const META = ([^\n]*);", html)
    if match is None:
        raise ValueError("no META block: not a generated viewer")
    meta = json.loads(match.group(1))
    meta["has_flags"] = n_flagged > 0
    return html.replace(
        match.group(0), f"const META = {json.dumps(meta, separators=(',', ':'))};", 1
    )


def sync(
    path: Path,
    gps_sites: dict,
    calval_sites: gpd.GeoDataFrame,
    granule_flags: dict[str, dict] | None = None,
) -> None:
    """Rewrite ``path`` with the current generated blocks."""
    html = path.read_text()
    html = _replace_app_css(html)
    html = _replace_body(html)
    html = _replace_app_js(html)
    html = _upsert_gps_data(html, gps_sites)
    html = _refresh_frame_data(html, calval_sites, granule_flags)
    path.write_text(html)
    print(f"synced {path} ({path.stat().st_size / 1e6:.1f} MB)")


def main(argv: list[str] | None = None) -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("html", type=Path, nargs="+", help="Built viewer(s) to update.")
    parser.add_argument(
        "--gps-source",
        default=gen.NGL_STATION_MAP,
        help="UNR/NGL station map URL, a local copy of that page, or a GeoJSON "
        "of sites to embed.",
    )
    parser.add_argument(
        "--no-gps", action="store_true", help="Embed an empty GPS collection."
    )
    parser.add_argument(
        "--calval-sites",
        type=Path,
        default=gen.CALVAL_SITES,
        help="GeoJSON of CalVal site polygons the frames are flagged against.",
    )
    parser.add_argument(
        "--granule-flags",
        type=Path,
        default=None,
        help="Per-granule flag cache (from collect_granule_flags.py) to attach.",
    )
    args = parser.parse_args(argv)

    gps_sites = gen.load_gps_sites(None if args.no_gps else args.gps_source)
    calval_sites = gpd.read_file(args.calval_sites)
    granule_flags = (
        gen.load_granule_flags(args.granule_flags) if args.granule_flags else None
    )
    for path in args.html:
        sync(path, gps_sites, calval_sites, granule_flags)


if __name__ == "__main__":
    main()
