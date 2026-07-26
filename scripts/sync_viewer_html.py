#!/usr/bin/env python
"""Refresh a built viewer's markup, styles and app code in place.

``generate_scope_viewer.py`` needs the frame GeoPackage and a GSLC catalog, which
are not in the repository, so a checked-in viewer cannot simply be rebuilt after
a UI change. This script swaps the generated parts of an existing HTML file --
``APP_CSS``, ``BODY_HTML``, ``APP_JS`` and the GPS site collection -- for the
current ones, leaving the vendored MapLibre bundle and the embedded frame data
untouched.

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


def sync(path: Path, gps_sites: dict) -> None:
    """Rewrite ``path`` with the current generated blocks."""
    html = path.read_text()
    html = _replace_app_css(html)
    html = _replace_body(html)
    html = _replace_app_js(html)
    html = _upsert_gps_data(html, gps_sites)
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
    args = parser.parse_args(argv)

    gps_sites = gen.load_gps_sites(None if args.no_gps else args.gps_source)
    for path in args.html:
        sync(path, gps_sites)


if __name__ == "__main__":
    main()
