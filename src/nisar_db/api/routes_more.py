"""Serve ``/api/v1`` routes for frame health, areas, events, exports and DISP.

They cover frame health, areas, event comparisons, exports and the DISP-NISAR
assets. The MCP server offers the same through
:mod:`nisar_db.api.mcp_more`; both call :mod:`nisar_db.api.analysis`.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse, Response

from nisar_db.api import analysis, assets, store
from nisar_db.api.routes import _bbox, dataset, frame_query
from nisar_db.api.security import JOBS, LIMITED_READ, READ, Caller

router = APIRouter(prefix="/api/v1")


def _known(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except KeyError as exc:
        raise HTTPException(404, str(exc.args[0])) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


# -- frame health ----------------------------------------------------------------------


@router.get("/frames/{key}/coverage", tags=["frame health"])
def frame_coverage(
    key: str,
    ds: store.Dataset = Depends(dataset),
    today: date | None = None,
    _: Caller = Depends(READ),
) -> dict:
    """Return a frame's GSLC record: missed cycles, gaps, days since the last one."""
    return _known(analysis.frame_coverage, ds, key, today=today)


@router.get("/coverage", tags=["frame health"])
def coverage(
    ds: store.Dataset = Depends(dataset),
    q: store.FrameQuery = Depends(frame_query),
    stale_days: int = Query(30, ge=1),
    today: date | None = None,
    limit: int = Query(50, ge=1, le=1000),
    _: Caller = Depends(READ),
) -> dict:
    """List frames with missed cycles or no recent acquisition, worst first."""
    return analysis.coverage_scan(
        ds, q, today=today, stale_days=stale_days, limit=limit
    )


@router.get("/frames/{key}/network", tags=["frame health"])
def frame_network(
    key: str, ds: store.Dataset = Depends(dataset), _: Caller = Depends(READ)
) -> dict:
    """Return a frame's GUNW network: pieces, breaks, acquisitions in no pair."""
    return _known(analysis.frame_network, ds, key)


@router.get("/network", tags=["frame health"])
def network(
    ds: store.Dataset = Depends(dataset),
    q: store.FrameQuery = Depends(frame_query),
    limit: int = Query(50, ge=1, le=1000),
    _: Caller = Depends(READ),
) -> dict:
    """Count connected / disconnected GUNW networks and list the disconnected frames."""
    return analysis.network_scan(ds, q, limit=limit)


@router.get("/frames/{key}/next-passes", tags=["frame health"])
def frame_next_passes(
    key: str,
    ds: store.Dataset = Depends(dataset),
    n: int = Query(3, ge=1, le=30),
    today: date | None = None,
    _: Caller = Depends(READ),
) -> dict:
    """Return a frame's next expected acquisitions (12-day repeat)."""
    return _known(analysis.frame_next_passes, ds, key, n=n, today=today)


@router.get("/next-passes", tags=["frame health"])
def next_passes(
    lon: float,
    lat: float,
    ds: store.Dataset = Depends(dataset),
    n: int = Query(3, ge=1, le=30),
    today: date | None = None,
    _: Caller = Depends(READ),
) -> dict:
    """Return the next expected NISAR passes over a point, soonest first."""
    return analysis.point_next_passes(ds, lon, lat, n=n, today=today)


@router.get("/frames/{key}/disp-readiness", tags=["frame health"])
def frame_readiness(
    key: str,
    ds: store.Dataset = Depends(dataset),
    batch_size: int = Query(15, ge=1, le=200),
    today: date | None = None,
    _: Caller = Depends(READ),
) -> dict:
    """Return a frame's progress towards its DISP-NISAR batches."""
    return _known(analysis.frame_readiness, ds, key, batch_size=batch_size, today=today)


@router.get("/disp-readiness", tags=["frame health"])
def readiness(
    ds: store.Dataset = Depends(dataset),
    q: store.FrameQuery = Depends(frame_query),
    batch_size: int = Query(15, ge=1, le=200),
    today: date | None = None,
    limit: int = Query(50, ge=1, le=1000),
    _: Caller = Depends(READ),
) -> dict:
    """Count frames by DISP status and list those closest to their next batch."""
    return analysis.readiness_scan(
        ds, q, batch_size=batch_size, today=today, limit=limit
    )


# -- areas -----------------------------------------------------------------------------


@router.get("/aoi", tags=["areas"])
def aoi_box(
    bbox: str = Query(..., description="west,south,east,north"),
    ds: store.Dataset = Depends(dataset),
    min_share: float = Query(0.0, ge=0, le=1),
    today: date | None = None,
    _: Caller = Depends(READ),
) -> dict:
    """List the frames over a box: share, latest pair, next pass, DISP status."""
    return _known(analysis.aoi, ds, box=_bbox(bbox), min_share=min_share, today=today)


@router.post("/aoi", tags=["areas"])
def aoi_geometry(
    geometry: dict = Body(..., description="a GeoJSON geometry or Feature"),
    ds: store.Dataset = Depends(dataset),
    min_share: float = Query(0.0, ge=0, le=1),
    today: date | None = None,
    _: Caller = Depends(READ),
) -> dict:
    """List the frames over a GeoJSON polygon (body)."""
    return _known(analysis.aoi, ds, geometry=geometry, min_share=min_share, today=today)


# -- event comparisons -----------------------------------------------------------------


@router.get("/frames/{key}/event-qa", tags=["events"])
def frame_event_qa(
    key: str,
    when: str = Query(..., description="event date or time (UTC)"),
    ds: store.Dataset = Depends(dataset),
    metric: str = "cm",
    window_days: int = Query(120, ge=12, le=1000),
    _: Caller = Depends(READ),
) -> dict:
    """Compare a QA metric before, across and after an event on one frame."""
    return _known(
        analysis.frame_event_qa, ds, key, when, metric=metric, window_days=window_days
    )


@router.get("/events/earthquakes/{event_id}/compare", tags=["events"])
def earthquake_compare(
    event_id: str,
    ds: store.Dataset = Depends(dataset),
    metric: str = "cm",
    window_days: int = Query(120, ge=12, le=1000),
    radius_km: float = Query(0, ge=0, le=500),
    _: Caller = Depends(LIMITED_READ),
) -> dict:
    """For every frame over an earthquake: the metric before, across and after it."""
    import requests as _requests

    from nisar_db.events import get_earthquake

    try:
        quake = get_earthquake(event_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except _requests.RequestException as exc:
        raise HTTPException(502, f"USGS did not answer: {type(exc).__name__}") from exc
    return _known(
        analysis.earthquake_compare,
        ds,
        quake,
        metric=metric,
        window_days=window_days,
        radius_km=radius_km,
    )


# -- exports ---------------------------------------------------------------------------


@router.get("/export/frames", tags=["exports"])
def export_frames(
    ds: store.Dataset = Depends(dataset),
    q: store.FrameQuery = Depends(frame_query),
    format: Literal["csv", "geojson", "kml"] = "csv",  # noqa: A002
    _: Caller = Depends(READ),
) -> Response:
    """Download the filtered frames as CSV, GeoJSON or KML."""
    from nisar_db.exports import MEDIA_TYPES

    text = analysis.export_frames(ds, q, format)
    return Response(
        text,
        media_type=MEDIA_TYPES[format],
        headers={
            "Content-Disposition": (
                f'attachment; filename="nisar_frames_{ds.id}.{format}"'
            )
        },
    )


@router.get("/export/entries", tags=["exports"])
def export_entries(
    ds: store.Dataset = Depends(dataset),
    q: store.FrameQuery = Depends(frame_query),
    _: Caller = Depends(READ),
) -> Response:
    """Download the filtered frames' GUNW pairs or GSLC granules as CSV."""
    text = analysis.export_entries(ds, q, "csv")
    name = "pairs" if q.product == "gunw" else "granules"
    return Response(
        text,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="nisar_{name}_{ds.id}.csv"'
        },
    )


# -- DISP-NISAR assets -----------------------------------------------------------------


def _repo(request: Request) -> Path | None:
    rd = request.app.state.settings.repo_dir
    return Path(rd) if rd else None


@router.get("/disp/assets", tags=["disp"])
def disp_assets(request: Request, _: Caller = Depends(READ)) -> dict:
    """List the DISP-NISAR assets: published release, latest build here, repo."""
    return assets.overview(repo_dir=_repo(request), jobs=request.app.state.jobs)


@router.get("/disp/assets/{kind}", tags=["disp"])
def disp_asset_file(
    kind: str,
    request: Request,
    source: Literal["auto", "built", "published", "repo"] = "auto",
    _: Caller = Depends(READ),
) -> Response:
    """Download one asset by kind (consistent_gslc, blackout_dates, ...).

    ``auto`` serves a local build (or the repo's blackout dates) and otherwise
    redirects to the published release.
    """
    if kind not in assets.KINDS:
        raise HTTPException(
            400, f"unknown asset kind {kind!r}; known: {sorted(assets.KINDS)}"
        )
    if source in ("auto", "built", "repo"):
        path = assets.local_file(
            kind, repo_dir=_repo(request), jobs=request.app.state.jobs
        )
        if path is not None and (source != "repo" or kind == "blackout_dates"):
            return FileResponse(path, filename=path.name)
    if source in ("auto", "published"):
        for a in assets.published().get("assets", []):
            if a["kind"] == kind:
                return RedirectResponse(a["url"])
    raise HTTPException(
        404,
        f"no {kind} asset here or in the latest release; "
        "start a 'build-disp-assets' job for a fresh set",
    )


def _frame_asset(request: Request, kind: str, key: str, ds: store.Dataset) -> dict:
    path = assets.local_file(kind, repo_dir=_repo(request), jobs=request.app.state.jobs)
    if path is None:
        raise HTTPException(
            404,
            f"no local {kind} asset; start a 'build-disp-assets' job "
            "(or download the published one from /api/v1/disp/assets)",
        )
    f = ds.frame(key)
    idx = f["properties"]["frame_idx"] if f is not None else key
    entry = assets.frame_entry(assets.load_json(path), idx)
    if entry is None:
        raise HTTPException(404, f"frame {key} is not in {path.name}")
    return {"frame_idx": idx, "asset": path.name, kind: entry}


@router.get("/disp/consistent/{key}", tags=["disp"])
def disp_consistent(
    key: str,
    request: Request,
    ds: store.Dataset = Depends(dataset),
    _: Caller = Depends(READ),
) -> dict:
    """Return a frame's consistent-GSLC entry: mode, coverage, sensing times."""
    return _frame_asset(request, "consistent_gslc", key, ds)


@router.get("/disp/blackout-dates/{key}", tags=["disp"])
def disp_blackout(
    key: str,
    request: Request,
    ds: store.Dataset = Depends(dataset),
    _: Caller = Depends(READ),
) -> dict:
    """Return a frame's blackout windows from the blackout-dates asset."""
    return _frame_asset(request, "blackout_dates", key, ds)


@router.get("/disp/reference-dates/{key}", tags=["disp"])
def disp_reference(
    key: str,
    request: Request,
    ds: store.Dataset = Depends(dataset),
    _: Caller = Depends(READ),
) -> dict:
    """Return a frame's reference (reset) dates from the reference-dates asset."""
    return _frame_asset(request, "reference_dates", key, ds)


@router.post("/disp/build", tags=["disp"], status_code=202)
def disp_build(
    request: Request,
    max_results: int = Body(0, embed=True),
    caller: Caller = Depends(JOBS),
) -> dict:
    """Start a fresh DISP-NISAR asset build from CMR (a job; takes minutes).

    Uses the repo's blackout dates when there are some.
    """
    jobs = request.app.state.jobs
    params: dict = {"max_results": max_results}
    repo = assets.repo_blackout(_repo(request))
    if repo is not None:
        params["blackout_file"] = str(repo)
    try:
        return jobs.submit("build-disp-assets", params, caller.name).public()
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
