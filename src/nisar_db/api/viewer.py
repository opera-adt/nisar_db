"""Serving viewer pages, and links that open the viewer in a given state.

The page reads its state from the query string on load (see ``VIEWER_PARAMS``),
so a script or notebook can open the viewer on, say, the GUNW interferograms of
cycles 20-25 coloured by coherence, spinning, in space, full screen.
``/api/v1/viewer/link`` checks the parameters and returns that URL.
"""

from __future__ import annotations

import gzip
from collections import OrderedDict
from datetime import date
from typing import Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from nisar_db.api.security import READ, Caller
from nisar_db.api.store import VIEW_ID

router = APIRouter(prefix="/api/v1/viewer", tags=["viewer"])

#: Query parameters the page understands, with what each sets.
VIEWER_PARAMS: dict[str, str] = {
    "product": "gslc | gunw: the product on the map",
    "opera": "1 | 0: the OPERA switch (DISP planning features)",
    "track": "track filter, e.g. 12 or 10-20 or 12,34",
    "frame": "frame filter, same syntax",
    "id": "frame id (8109) or track_frame (34_19)",
    "cycle": "cycle filter, e.g. 23 or 20-25",
    "start": "first date of the date range, YYYY-MM-DD",
    "end": "last date of the date range, YYYY-MM-DD",
    "pass": "all | asc | desc",
    "modes": "comma-separated mode chips, e.g. 2005,4005 (empty: all)",
    "pols": "comma-separated polarization chips, e.g. DHDH,QPDH",
    "color": "a Color frames by option, e.g. gslc_count, cons_mode, gunw_qa_cm",
    "basemap": "light | dark | sat | sat2",
    "sky": "theme | white | black | space: what shows behind the globe",
    "theme": "dark | light",
    "center": "lon,lat to centre the map on",
    "zoom": "map zoom",
    "play": "spin | cycles | time: start playing (cycles / time step the filters)",
    "spin": "1: also spin the globe while play=cycles / time steps",
    "step_days": "days per step when play=time",
    "cumulative": "1: cumulative cycles / time steps",
    "fullscreen": (
        "1: open with the map alone (the browser may still ask before full screen)"
    ),
    "gps": "1: show the UNR GPS sites",
    "popup": "0: turn the frame popup off",
    "browse": "a GSLC / GUNW granule name: open its browse image",
    "browse_layer": (
        "browse (default) or a GUNW QA layer: wrapped, coherence, cc, "
        "unwrapped, iono, ..."
    ),
    "browse_map": "1: place that image on the map",
}

_GZIP: OrderedDict[str, bytes] = OrderedDict()


def page_response(request: Request, body: bytes, etag: str) -> Response:
    """Return a viewer page: revalidated by ETag, gzipped when accepted."""
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    headers = {"ETag": etag, "Cache-Control": "no-cache", "Vary": "Accept-Encoding"}
    if "gzip" in request.headers.get("accept-encoding", ""):
        # Pages are 10-100 MB of mostly JSON and compress about six-fold.
        if etag not in _GZIP:
            if len(_GZIP) >= 8:
                _GZIP.popitem(last=False)
            _GZIP[etag] = gzip.compress(body, compresslevel=5)
        headers["Content-Encoding"] = "gzip"
        return Response(
            _GZIP[etag], media_type="text/html; charset=utf-8", headers=headers
        )
    return Response(body, media_type="text/html; charset=utf-8", headers=headers)


@router.get("/params")
def params() -> dict[str, str]:
    """List the query parameters a viewer page reads on load."""
    return VIEWER_PARAMS


@router.get("/link")
def link(
    request: Request,
    dataset: str = Query("published", description="'published' or a rebuilt view id"),
    product: Literal["gslc", "gunw"] | None = None,
    opera: bool | None = None,
    track: str | None = None,
    frame: str | None = None,
    id: str | None = Query(None, description="frame id or track_frame"),  # noqa: A002
    cycle: str | None = None,
    start: date | None = None,
    end: date | None = None,
    pass_: Literal["all", "asc", "desc"] | None = Query(None, alias="pass"),
    modes: str | None = None,
    pols: str | None = None,
    color: str | None = None,
    basemap: Literal["light", "dark", "sat", "sat2"] | None = None,
    sky: Literal["theme", "white", "black", "space"] | None = None,
    theme: Literal["dark", "light"] | None = None,
    center: str | None = Query(None, description="lon,lat"),
    zoom: float | None = None,
    play: Literal["spin", "cycles", "time"] | None = None,
    spin: bool | None = None,
    step_days: int | None = Query(None, ge=1, le=366),
    cumulative: bool | None = None,
    fullscreen: bool | None = None,
    gps: bool | None = None,
    popup: bool | None = None,
    browse: str | None = None,
    browse_layer: str | None = None,
    browse_map: bool | None = None,
    _: Caller = Depends(READ),
) -> dict[str, str]:
    """Return a URL that opens the viewer in the state these parameters describe."""
    if dataset != "published" and not VIEW_ID.match(dataset):
        raise HTTPException(400, f"unknown dataset {dataset!r}")
    if center is not None:
        try:
            lon, lat = (float(v) for v in center.split(","))
        except ValueError as exc:
            raise HTTPException(400, "center is lon,lat") from exc
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise HTTPException(400, "center is out of range")
    values = {
        "product": product,
        "opera": opera,
        "track": track,
        "frame": frame,
        "id": id,
        "cycle": cycle,
        "start": start,
        "end": end,
        "pass": pass_,
        "modes": modes,
        "pols": pols,
        "color": color,
        "basemap": basemap,
        "sky": sky,
        "theme": theme,
        "center": center,
        "zoom": zoom,
        "play": play,
        "spin": spin,
        "step_days": step_days,
        "cumulative": cumulative,
        "fullscreen": fullscreen,
        "gps": gps,
        "popup": popup,
        "browse": browse,
        "browse_layer": browse_layer,
        "browse_map": browse_map,
    }
    query = {
        k: (
            int(v)
            if isinstance(v, bool)
            else v.isoformat() if isinstance(v, date) else v
        )
        for k, v in values.items()
        if v is not None
    }
    path = "/" if dataset == "published" else f"/view/{dataset}"
    base = str(request.base_url).rstrip("/")
    return {
        "url": f"{base}{path}{'?' + urlencode(query) if query else ''}",
        "path": path,
    }
