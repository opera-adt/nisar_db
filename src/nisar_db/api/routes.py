"""``/api/v1``: catalog queries over the viewer's datasets, and jobs."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import ValidationError

from nisar_db.api import store
from nisar_db.api.jobs import KINDS, Jobs
from nisar_db.api.security import COOKIE, JOBS, LIMITED_READ, READ, Caller, match_key
from nisar_db.api.settings import Settings

router = APIRouter(prefix="/api/v1")

# ---------------------------------------------------------------------------
# Datasets and frames
# ---------------------------------------------------------------------------


def dataset(
    request: Request,
    dataset: str = Query("published", description="'published' or a rebuilt view id"),
) -> store.Dataset:
    """Resolve the ``dataset`` query parameter."""
    try:
        return request.app.state.store.get(dataset)
    except KeyError as exc:
        raise HTTPException(
            404, f"no dataset {dataset!r}; see /api/v1/datasets"
        ) from exc


def _csv(text: str | None) -> tuple[str, ...]:
    return tuple(v.strip() for v in (text or "").split(",") if v.strip())


def frame_query(
    product: Literal["gslc", "gunw"] = "gslc",
    track: str | None = Query(None, description="e.g. 12, 10-20 or 12,34"),
    frame: str | None = Query(None, description="same syntax as track"),
    direction: Literal["A", "D", "asc", "desc"] | None = None,
    bbox: str | None = Query(None, description="west,south,east,north"),
    cycle: str | None = Query(
        None, description="e.g. 23 or 20-25 (a GUNW pair matches on either date)"
    ),
    start: date | None = None,
    end: date | None = None,
    modes: str | None = Query(
        None, description="comma-separated modes, e.g. 2005,4005"
    ),
    pols: str | None = Query(None, description="comma-separated polarizations"),
    crids: str | None = None,
    has_data: bool | None = Query(
        None,
        description="only frames with (true) / without (false) granules or pairs left",
    ),
    calval: bool | None = None,
    land: bool | None = None,
    rollout: str | None = Query(
        None, description="comma-separated rollout options, 'none' for none"
    ),
    consistent_mode: str | None = None,
) -> store.FrameQuery:
    """Collect the frame filters from the query string."""
    box = None
    if bbox:
        try:
            w, s, e, n = (float(v) for v in bbox.split(","))
        except ValueError as exc:
            raise HTTPException(400, "bbox is west,south,east,north") from exc
        box = (w, s, e, n)
    for name, text in (("track", track), ("frame", frame), ("cycle", cycle)):
        try:
            store.int_set(text)
        except ValueError as exc:
            raise HTTPException(
                400, f"{name}: use numbers, ranges (10-20) and commas"
            ) from exc
    return store.FrameQuery(
        product=product,
        track=track,
        frame=frame,
        direction=direction[0].upper() if direction else None,
        bbox=box,
        cycle=cycle,
        start=start,
        end=end,
        modes=_csv(modes),
        pols=_csv(pols),
        crids=_csv(crids),
        has_data=has_data,
        calval=calval,
        land=land,
        rollout=_csv(rollout),
        consistent_mode=consistent_mode,
    )


@router.get("/datasets", tags=["catalog"])
def datasets(request: Request, _: Caller = Depends(READ)) -> list[dict]:
    """List the datasets: the published page and every rebuilt view."""
    st = request.app.state.store
    return [st.header(ds_id) for ds_id in st.paths()]


@router.get("/datasets/{dataset_id}", tags=["catalog"])
def dataset_info(dataset_id: str, request: Request, _: Caller = Depends(READ)) -> dict:
    """One dataset's header (parses it if needed)."""
    return dataset(request, dataset_id).summary()


@router.get("/frames", tags=["catalog"])
def frames(
    ds: store.Dataset = Depends(dataset),
    q: store.FrameQuery = Depends(frame_query),
    format: Literal["json", "geojson"] = "json",  # noqa: A002
    limit: int = Query(500, ge=1, le=50000),
    offset: int = Query(0, ge=0),
    _: Caller = Depends(READ),
) -> Any:
    """Frames passing the filters, as summaries (or GeoJSON with geometry).

    ``n_selected`` counts each frame's granules (GSLC) or pairs (GUNW) left by
    the cycle / date / mode / polarization / CRID filters.
    """
    sel = store.select(ds, q)
    page = sel[offset : offset + limit]
    if format == "geojson":
        return {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": f["geometry"],
                    "properties": store.frame_summary(f, q),
                }
                for f in page
            ],
            "total": len(sel),
        }
    return {
        "total": len(sel),
        "offset": offset,
        "frames": [store.frame_summary(f, q) for f in page],
    }


def _frame(ds: store.Dataset, key: str) -> dict:
    f = ds.frame(key)
    if f is None:
        raise HTTPException(404, f"no frame {key!r} in {ds.id}")
    return f


@router.get("/frames/{key}", tags=["catalog"])
def frame(
    key: str,
    ds: store.Dataset = Depends(dataset),
    geometry: bool = False,
    _: Caller = Depends(READ),
) -> dict:
    """One frame by id (``8109``) or track_frame (``34_19``, ``T34_F19``)."""
    f = _frame(ds, key)
    out = store.frame_summary(f)
    if geometry:
        out["geometry"] = f["geometry"]
    return out


@router.get("/frames/{key}/granules", tags=["catalog"])
def granules(
    key: str,
    ds: store.Dataset = Depends(dataset),
    q: store.FrameQuery = Depends(frame_query),
    _: Caller = Depends(READ),
) -> dict:
    """List a frame's GSLC granules or (``product=gunw``) GUNW pairs."""
    f = _frame(ds, key)
    entries = q.entries(f["properties"])
    return {
        "frame_idx": f["properties"]["frame_idx"],
        "product": q.product,
        "total": len(entries),
        "items": entries,
    }


@router.get("/frames/{key}/blackout", tags=["catalog"])
def frame_blackout(
    key: str, ds: store.Dataset = Depends(dataset), _: Caller = Depends(READ)
) -> dict:
    """Return a frame's blackout windows, monthly shares and reference dates."""
    return store.blackout(_frame(ds, key))


@router.get("/cycles", tags=["catalog"])
def cycles(
    ds: store.Dataset = Depends(dataset),
    product: Literal["gslc", "gunw"] = "gslc",
    _: Caller = Depends(READ),
) -> list[dict]:
    """Every cycle in the dataset with its date span and frame count."""
    return store.cycles(ds, product)


@router.get("/summary", tags=["catalog"])
def summary(
    ds: store.Dataset = Depends(dataset),
    q: store.FrameQuery = Depends(frame_query),
    _: Caller = Depends(READ),
) -> dict:
    """Summarise consistent modes and rollout options over the filtered frames."""
    sel = store.select(ds, q)
    return {
        "consistent": store.consistent_summary(sel),
        "rollout": store.rollout_summary(ds, sel),
    }


# ---------------------------------------------------------------------------
# QA drops
# ---------------------------------------------------------------------------


def qa_options(
    product: Literal["gslc", "gunw"] = Query(
        "gunw", description="whose QA: GUNW pairs or GSLC acquisitions"
    ),
    metric: str = Query(
        "cm", description="a metric from /api/v1/qa-metrics, e.g. cm, v, n, is, rl"
    ),
    threshold: float = Query(
        3.0, gt=0, description="robust z-score on the bad side that flags an entry"
    ),
    min_pairs: int = Query(2, ge=1, description="flagged pairs a date needs (GUNW)"),
    min_share: float = Query(
        0.5, gt=0, le=1, description="share of its pairs a date needs flagged (GUNW)"
    ),
    by_baseline: bool = Query(
        True, description="compare GUNW pairs with the same temporal baseline"
    ),
) -> dict:
    """Collect the QA-drop options from the query string."""
    from nisar_db.qa_drops import METRICS

    if metric not in METRICS:
        raise HTTPException(400, f"unknown metric {metric!r}; see /api/v1/qa-metrics")
    if product not in METRICS[metric].products:
        raise HTTPException(400, f"{metric!r} is not a {product.upper()} metric")
    return {
        "product": product,
        "metric": metric,
        "threshold": threshold,
        "min_pairs": min_pairs,
        "min_share": min_share,
        "by_baseline": by_baseline,
    }


@router.get("/qa-metrics", tags=["qa"])
def qa_metrics(_: Caller = Depends(READ)) -> list[dict]:
    """List the QA metrics, their products and which way is bad."""
    from nisar_db.qa_drops import METRICS

    side = {-1: "low", 1: "high", 0: "either"}
    return [
        {
            "metric": m.key,
            "label": m.label,
            "products": list(m.products),
            "bad_when": side[m.bad],
            "log": m.log,
        }
        for m in METRICS.values()
    ]


@router.get("/frames/{key}/qa-drops", tags=["qa"])
def frame_qa_drops(
    key: str,
    ds: store.Dataset = Depends(dataset),
    q: store.FrameQuery = Depends(frame_query),
    opts: dict = Depends(qa_options),
    _: Caller = Depends(READ),
) -> dict:
    """Flag a frame's pairs (or acquisitions) whose QA metric drops from its median.

    Returns the stack median, the flagged entries (worst first) and the dates
    they point to. Cycle, date, mode and polarization filters narrow the stack.
    """
    from nisar_db.qa_drops import find_drops

    f = _frame(ds, key)
    q.product = opts.pop("product")
    entries = q.entries(f["properties"])
    report = find_drops(
        entries,
        opts.pop("metric"),
        product=q.product,  # type: ignore[arg-type]
        granules=f["properties"].get("granules"),
        **opts,
    )
    return {
        "frame_idx": f["properties"]["frame_idx"],
        "id": f["properties"]["id"],
        **report,
    }


@router.get("/qa-drops", tags=["qa"])
def qa_drops(
    ds: store.Dataset = Depends(dataset),
    q: store.FrameQuery = Depends(frame_query),
    opts: dict = Depends(qa_options),
    limit: int = Query(50, ge=1, le=1000),
    _: Caller = Depends(READ),
) -> dict:
    """Scan the filtered frames for QA drops: worst frames, dates flagged in many."""
    q.product = opts.pop("product")
    return store.qa_drops_scan(ds, q, opts.pop("metric"), limit=limit, **opts)


# ---------------------------------------------------------------------------
# Browse images
# ---------------------------------------------------------------------------


@router.get("/browse/{gid}", tags=["browse"], response_class=Response)
def browse_image(
    gid: str,
    request: Request,
    layer: str = Query(
        "browse", description="browse, or a GUNW QA layer (coherence, cc, iono, ...)"
    ),
    max_side: int = Query(768, ge=128, le=1600),
    _: Caller = Depends(LIMITED_READ),
) -> Response:
    """Return a granule's browse (or QA-layer) image as a JPEG."""
    import requests as _requests

    from nisar_db.api.browse import fetch_image

    try:
        data, meta = fetch_image(
            gid, layer, helper=request.app.state.helper, max_side=max_side
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(401, str(exc)) from exc
    except _requests.RequestException as exc:
        raise HTTPException(
            502, f"could not fetch the image: {type(exc).__name__}"
        ) from exc
    return Response(
        data,
        media_type="image/jpeg",
        headers={"Cache-Control": "max-age=86400", "X-Image-Layer": meta["layer"]},
    )


@router.get("/browse/{gid}/overlay", tags=["browse"])
def browse_overlay_route(
    gid: str,
    request: Request,
    layer: str = "browse",
    dataset: str = Query("published", description="the viewer page the link opens"),
    _: Caller = Depends(LIMITED_READ),
) -> dict:
    """Return how to put a granule's image on a map.

    A viewer link that places it, the image URL and its corner coordinates
    (for a MapLibre image source).
    """
    from nisar_db.api.browse import browse_overlay

    try:
        return browse_overlay(
            gid,
            layer,
            dataset=dataset,
            base=str(request.base_url),
            helper=request.app.state.helper,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/frames/{key}/qa-drops/images", tags=["qa", "browse"])
def frame_qa_drop_images(
    key: str,
    request: Request,
    ds: store.Dataset = Depends(dataset),
    metric: str = Query("cm"),
    n: int = Query(2, ge=0, le=4),
    layer: str | None = Query(
        None, description="default: the QA layer that shows the metric"
    ),
    threshold: float = Query(3.0, gt=0),
    _: Caller = Depends(READ),
) -> dict:
    """Pick what to look at after a QA-drop check.

    The worst flagged pairs and a typical one, each with its image and
    overlay URLs.
    """
    from urllib.parse import urlencode

    from nisar_db.api.browse import LAYER_FOR_METRIC, LAYERS, pick_qa_examples
    from nisar_db.qa_drops import METRICS, find_drops

    if metric not in METRICS or "gunw" not in METRICS[metric].products:
        raise HTTPException(
            400, f"{metric!r} is not a GUNW metric; see /api/v1/qa-metrics"
        )
    want = layer or LAYER_FOR_METRIC.get(metric, "browse")
    if want not in LAYERS:
        raise HTTPException(400, f"unknown layer {want!r}")
    f = _frame(ds, key)
    props = f["properties"]
    entries = props.get("gunw_ifgs") or []
    report = find_drops(
        entries,
        metric,
        product="gunw",
        threshold=threshold,
        granules=props.get("granules"),
    )
    base = str(request.base_url).rstrip("/")
    picks = []
    for pick in pick_qa_examples(
        report, entries, metric, n=n, granules=props.get("granules")
    ):
        q = urlencode({"layer": want})
        picks.append(
            {
                **pick,
                "layer": want,
                "image_url": f"{base}/api/v1/browse/{pick['gid']}?{q}",
                "overlay_url": (
                    f"{base}/api/v1/browse/{pick['gid']}/overlay?{q}&dataset={ds.id}"
                ),
            }
        )
    return {
        "id": props["id"],
        "metric": metric,
        "median": report.get("median"),
        "status": report.get("status"),
        "dates": [d["date"] for d in report.get("dates", [])],
        "picks": picks,
    }


# ---------------------------------------------------------------------------
# Events: earthquakes and volcanoes
# ---------------------------------------------------------------------------


def _bbox(text: str | None) -> tuple[float, float, float, float] | None:
    if not text:
        return None
    try:
        w, s, e, n = (float(v) for v in text.split(","))
    except ValueError as exc:
        raise HTTPException(400, "bbox is west,south,east,north") from exc
    return (w, s, e, n)


@router.get("/events/earthquakes", tags=["events"])
def earthquakes(
    bbox: str | None = Query(None, description="west,south,east,north"),
    lon: float | None = None,
    lat: float | None = None,
    radius_km: float = Query(200, gt=0, le=20000),
    start: str | None = Query(None, description="YYYY-MM-DD or ISO time (UTC)"),
    end: str | None = None,
    min_magnitude: float = 4.5,
    order: Literal["time", "magnitude"] = "time",
    limit: int = Query(50, ge=1, le=2000),
    _: Caller = Depends(LIMITED_READ),
) -> list[dict]:
    """Search USGS earthquakes by box or circle, dates and magnitude."""
    import requests as _requests

    from nisar_db.events import search_earthquakes

    center = (lon, lat) if lon is not None and lat is not None else None
    try:
        return search_earthquakes(
            bbox=_bbox(bbox),
            center=center,
            radius_km=radius_km if center else None,
            start=start,
            end=end,
            min_magnitude=min_magnitude,
            limit=limit,
            order=order,
        )
    except _requests.RequestException as exc:
        raise HTTPException(502, f"USGS did not answer: {type(exc).__name__}") from exc


@router.get("/events/earthquakes/{event_id}/frames", tags=["events"])
def earthquake_frames(
    event_id: str,
    request: Request,
    ds: store.Dataset = Depends(dataset),
    radius_km: float = Query(
        0, ge=0, le=500, description="also frames within this distance of the epicentre"
    ),
    max_dt: int | None = Query(
        None, ge=1, description="longest temporal baseline, days"
    ),
    n_pairs: int = Query(3, ge=0, le=50),
    _: Caller = Depends(LIMITED_READ),
) -> dict:
    """List the frames over a USGS earthquake and their coseismic GUNW pairs."""
    import requests as _requests

    from nisar_db.events import get_earthquake

    try:
        quake = get_earthquake(event_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except _requests.RequestException as exc:
        raise HTTPException(502, f"USGS did not answer: {type(exc).__name__}") from exc
    return store.earthquake_report(
        ds,
        quake,
        radius_km=radius_km,
        max_dt=max_dt,
        n_pairs=n_pairs,
        base=str(request.base_url).rstrip("/"),
    )


@router.get("/events/volcanoes", tags=["events"])
def volcanoes(
    request: Request,
    name: str | None = None,
    bbox: str | None = Query(None, description="west,south,east,north"),
    country: str | None = None,
    erupted_since: int | None = Query(
        None, description="last eruption in or after this year"
    ),
    limit: int = Query(50, ge=1, le=2000),
    _: Caller = Depends(READ),
) -> list[dict]:
    """Search the Smithsonian GVP Holocene volcanoes (name, box, country, eruption)."""
    from nisar_db.events import find_volcanoes

    return find_volcanoes(
        request.app.state.store.volcanoes(),
        name=name,
        bbox=_bbox(bbox),
        country=country,
        erupted_since=erupted_since,
        limit=limit,
    )


@router.get("/events/volcanoes/{vnum}/frames", tags=["events"])
def volcano_frames(
    vnum: int,
    request: Request,
    ds: store.Dataset = Depends(dataset),
    radius_km: float = Query(10, ge=0, le=500),
    start: date | None = None,
    end: date | None = None,
    n_pairs: int = Query(5, ge=0, le=50),
    _: Caller = Depends(READ),
) -> dict:
    """List the frames over a volcano (GVP number) and their GUNW pairs."""
    match = [v for v in request.app.state.store.volcanoes() if v.get("vnum") == vnum]
    if not match:
        raise HTTPException(404, f"no volcano {vnum}; see /api/v1/events/volcanoes")
    return store.volcano_report(
        ds,
        match[0],
        radius_km=radius_km,
        start=start,
        end=end,
        n_pairs=n_pairs,
        base=str(request.base_url).rstrip("/"),
    )


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------


@router.get("/frames/{key}/duplicates", tags=["duplicates"])
def frame_duplicates(
    key: str,
    ds: store.Dataset = Depends(dataset),
    q: store.FrameQuery = Depends(frame_query),
    _: Caller = Depends(READ),
) -> dict:
    """List a frame's duplicate granules: same date, mode and coverage (GUNW: pair).

    Each group says why it repeats (reprocessed, split, repeat) and which
    granule to keep (newest CRID, then highest product counter).
    """
    from nisar_db.duplicates import duplicate_groups, summarize

    f = _frame(ds, key)
    groups = duplicate_groups(q.entries(f["properties"]), q.product)  # type: ignore[arg-type]
    return {
        "id": f["properties"]["id"],
        "frame_idx": f["properties"]["frame_idx"],
        "product": q.product,
        **summarize(groups),
        "duplicates": groups,
    }


@router.get("/duplicates", tags=["duplicates"])
def duplicates(
    ds: store.Dataset = Depends(dataset),
    q: store.FrameQuery = Depends(frame_query),
    limit: int = Query(50, ge=1, le=1000),
    _: Caller = Depends(READ),
) -> dict:
    """Scan the filtered frames for duplicates: totals by reason, worst frames first."""
    return store.duplicates_scan(ds, q, limit=limit)


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def jobs(request: Request) -> Jobs:
    """Return the app's job runner."""
    return request.app.state.jobs


def _visible(caller: Caller, owner: str) -> bool:
    return caller.key is None or caller.allows("admin") or caller.name == owner


@router.get("/jobs/kinds", tags=["jobs"])
def job_kinds(request: Request, _: Caller = Depends(READ)) -> list[dict]:
    """Job kinds with their parameter schemas, and whether this service runs them."""
    refused = request.app.state.jobs.refuse
    return [
        {
            "kind": k.name,
            "summary": k.summary,
            "enabled": k.name not in refused,
            "inputs": list(k.inputs),
            "params": k.params.model_json_schema(),
        }
        for k in KINDS.values()
    ]


@router.post("/jobs", tags=["jobs"], status_code=202)
def submit(
    kind: str = Body(..., embed=True),
    params: dict = Body(default_factory=dict, embed=True),
    runner: Jobs = Depends(jobs),
    caller: Caller = Depends(JOBS),
) -> dict:
    """Start a ``nisar-db`` command; poll ``/jobs/{id}`` for its state."""
    try:
        return runner.submit(kind, params, caller.name).public()
    except KeyError as exc:
        raise HTTPException(
            404, f"no job kind {kind!r}; see /api/v1/jobs/kinds"
        ) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(
            422, exc.errors(include_url=False, include_context=False)
        ) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/jobs", tags=["jobs"])
def list_jobs(
    runner: Jobs = Depends(jobs), caller: Caller = Depends(JOBS)
) -> list[dict]:
    """Jobs, newest first (on a shared service, your own unless your key is admin)."""
    return [j.public() for j in runner.listed() if _visible(caller, j.owner)]


def _job(runner: Jobs, job_id: str, caller: Caller):
    job = runner.get(job_id)
    if job is None or not _visible(caller, job.owner):
        raise HTTPException(404, f"no job {job_id!r}")
    return job


@router.get("/jobs/{job_id}", tags=["jobs"])
def job(
    job_id: str, runner: Jobs = Depends(jobs), caller: Caller = Depends(JOBS)
) -> dict:
    """Return a job's record and the tail of its log."""
    j = _job(runner, job_id, caller)
    return {**j.public(), "log": runner.log_tail(job_id)}


@router.delete("/jobs/{job_id}", tags=["jobs"])
def cancel(
    job_id: str, runner: Jobs = Depends(jobs), caller: Caller = Depends(JOBS)
) -> dict:
    """Cancel a queued or running job."""
    _job(runner, job_id, caller)
    return runner.cancel(job_id).public()


@router.get("/jobs/{job_id}/files/{name:path}", tags=["jobs"])
def job_file(
    job_id: str,
    name: str,
    runner: Jobs = Depends(jobs),
    caller: Caller = Depends(JOBS),
) -> Response:
    """Download one of a job's outputs."""
    _job(runner, job_id, caller)
    try:
        path = runner.output_path(job_id, name)
    except KeyError as exc:
        raise HTTPException(404, f"job {job_id} has no output {name!r}") from exc
    return FileResponse(path, filename=path.name)


# ---------------------------------------------------------------------------
# Browser session (shared mode)
# ---------------------------------------------------------------------------


@router.post("/session", tags=["auth"])
def session(request: Request, key: str = Body(..., embed=True)) -> Response:
    """Trade an API key for an HttpOnly cookie (a browser opening a private viewer)."""
    settings: Settings = request.app.state.settings
    k = match_key(settings, key)
    if settings.shared and k is None:
        raise HTTPException(401, "unknown API key")
    resp = JSONResponse(
        {
            "ok": True,
            "name": k.name if k else "local",
            "scopes": sorted(k.scopes) if k else [],
        }
    )
    resp.set_cookie(
        COOKIE,
        key,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        max_age=7 * 86400,
    )
    return resp


@router.delete("/session", tags=["auth"])
def end_session() -> Response:
    """Forget the browser's key cookie."""
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp
