"""Answer frame-health, area, event and export questions for REST and MCP.

One function per answer, so the REST routes and the MCP tools agree.
"""

from __future__ import annotations

from collections import Counter
from datetime import date

from nisar_db import frame_health as fh
from nisar_db.api import store
from nisar_db.api.store import Dataset, FrameQuery


def _frame(ds: Dataset, key: str) -> dict:
    f = ds.frame(key)
    if f is None:
        raise KeyError(f"no frame {key!r} in {ds.id}")
    return f


def _head(p: dict) -> dict:
    return {
        "id": p["id"],
        "frame_idx": p["frame_idx"],
        "track": p.get("track"),
        "frame": p.get("frame"),
        "passDirection": p.get("passDirection"),
    }


# -- coverage gaps ---------------------------------------------------------------------


def frame_coverage(ds: Dataset, key: str, *, today: date | None = None) -> dict:
    """Return one frame's GSLC acquisition record: missed cycles, gaps, staleness."""
    p = _frame(ds, key)["properties"]
    return {**_head(p), **fh.coverage(p.get("granules") or [], today=today)}


def coverage_scan(
    ds: Dataset,
    q: FrameQuery,
    *,
    today: date | None = None,
    stale_days: int = 30,
    limit: int = 50,
) -> dict:
    """Return the frames with missed cycles or stale for ``stale_days``, worst first."""
    rows, judged = [], 0
    for f in store.select(ds, q):
        p = f["properties"]
        c = fh.coverage(
            q.entries(p) if q.product == "gslc" else p.get("granules") or [],
            today=today,
        )
        if not c["n_acquisitions"]:
            continue
        judged += 1
        stale = c["days_since_last"] is not None and c["days_since_last"] > stale_days
        if c["missed_cycles"] or stale:
            rows.append(
                {
                    **_head(p),
                    "missed": len(c["missed_cycles"]),
                    "missed_cycles": [m["cycle"] for m in c["missed_cycles"]],
                    "last": c["last"],
                    "days_since_last": c["days_since_last"],
                    "stale": stale,
                }
            )
    rows.sort(key=lambda r: (-r["missed"], -(r["days_since_last"] or 0), r["id"]))
    return {
        "frames_judged": judged,
        "frames_flagged": len(rows),
        "stale_days": stale_days,
        "frames": rows[:limit],
    }


# -- network health --------------------------------------------------------------------


def frame_network(ds: Dataset, key: str) -> dict:
    """Return one frame's GUNW network: pieces, breaks, unpaired acquisitions."""
    p = _frame(ds, key)["properties"]
    return {**_head(p), **fh.network(p.get("gunw_ifgs") or [], p.get("granules") or [])}


def network_scan(ds: Dataset, q: FrameQuery, *, limit: int = 50) -> dict:
    """Return connected / disconnected frame counts and the disconnected frames."""
    status: Counter = Counter()
    bad = []
    for f in store.select(ds, q):
        p = f["properties"]
        n = fh.network(p.get("gunw_ifgs") or [])
        status[n["status"]] += 1
        if n["status"] == "disconnected":
            bad.append(
                {**_head(p), "components": n["components"], "breaks": n["breaks"]}
            )
    bad.sort(key=lambda r: (-r["components"], r["id"]))
    return {"status": dict(status), "frames": bad[:limit]}


# -- next passes -----------------------------------------------------------------------


def frame_next_passes(
    ds: Dataset, key: str, *, n: int = 3, today: date | None = None
) -> dict:
    """Return a frame's next expected acquisitions (12-day repeat)."""
    p = _frame(ds, key)["properties"]
    return {
        **_head(p),
        "last": max(
            (g["date"] for g in p.get("granules") or [] if g.get("date")), default=None
        ),
        "next": fh.next_passes(p.get("granules") or [], n=n, today=today),
        "note": "projected from the 12-day repeat, not the mission's acquisition plan",
    }


def point_next_passes(
    ds: Dataset, lon: float, lat: float, *, n: int = 3, today: date | None = None
) -> dict:
    """Return the next expected passes over a point, soonest first.

    Every frame covering the point contributes its own projected passes.
    """
    from nisar_db.events import frames_at

    frames = []
    for f in frames_at(ds.features, lon, lat):
        p = f["properties"]
        nxt = fh.next_passes(p.get("granules") or [], n=n, today=today)
        if nxt:
            frames.append({**_head(p), "next": nxt})
    frames.sort(key=lambda r: r["next"][0])
    return {
        "lon": lon,
        "lat": lat,
        "frames": frames,
        "next": frames[0]["next"][0] if frames else None,
        "note": (
            "projected from each frame's 12-day repeat, "
            "not the mission's acquisition plan"
        ),
    }


# -- DISP readiness --------------------------------------------------------------------


def frame_readiness(
    ds: Dataset, key: str, *, batch_size: int = 15, today: date | None = None
) -> dict:
    """Return one frame's progress towards its DISP-NISAR batches."""
    p = _frame(ds, key)["properties"]
    return {**_head(p), **fh.disp_readiness(p, batch_size=batch_size, today=today)}


def readiness_scan(
    ds: Dataset,
    q: FrameQuery,
    *,
    batch_size: int = 15,
    today: date | None = None,
    limit: int = 50,
) -> dict:
    """Return frame counts by DISP status and the frames closest to their next batch."""
    status: Counter = Counter()
    rows = []
    for f in store.select(ds, q):
        p = f["properties"]
        r = fh.disp_readiness(p, batch_size=batch_size, today=today)
        status[r["status"]] += 1
        if r["status"] != "no consistent mode":
            rows.append(
                {
                    **_head(p),
                    **{
                        k: r[k]
                        for k in (
                            "status",
                            "consistent_mode",
                            "n_usable",
                            "batches",
                            "to_next_batch",
                            "next_batch_date",
                        )
                    },
                }
            )
    rows.sort(
        key=lambda r: (
            r["to_next_batch"] or 99,
            r["next_batch_date"] or "9999",
            r["id"],
        )
    )
    return {"batch_size": batch_size, "status": dict(status), "frames": rows[:limit]}


# -- areas -----------------------------------------------------------------------------


def aoi(
    ds: Dataset,
    *,
    geometry: dict | None = None,
    box: tuple | None = None,
    min_share: float = 0.0,
    today: date | None = None,
) -> dict:
    """Return the frames over an area.

    Each with its share of the area, latest pair, next pass and DISP status.
    """
    from nisar_db.aoi import aoi_frames

    r = aoi_frames(ds.features, geometry=geometry, box=box, min_share=min_share)
    frames = []
    for hit in r["frames"]:
        p = hit.pop("feature")["properties"]
        pairs = sorted(
            p.get("gunw_ifgs") or [],
            key=lambda x: (str(x.get("sec")), str(x.get("ref"))),
        )
        last = pairs[-1] if pairs else None
        frames.append(
            {
                **hit,
                "n_acquisitions": len({g.get("date") for g in p.get("granules") or []}),
                "n_pairs": len(pairs),
                "latest_pair": (
                    {k: last.get(k) for k in ("ref", "sec", "dt", "gid")}
                    if last
                    else None
                ),
                "next_pass": (
                    fh.next_passes(p.get("granules") or [], n=1, today=today) or [None]
                )[0],
                "disp_status": fh.disp_readiness(p, today=today)["status"],
            }
        )
    return {**r, "frames": frames}


# -- events ----------------------------------------------------------------------------


def frame_event_qa(
    ds: Dataset, key: str, when: str, *, metric: str = "cm", window_days: int = 120
) -> dict:
    """Return a QA metric before, across and after an event on one frame."""
    p = _frame(ds, key)["properties"]
    return {
        **_head(p),
        **fh.event_qa_history(
            p.get("gunw_ifgs") or [],
            when,
            metric,
            window_days=window_days,
            granules=p.get("granules"),
        ),
    }


def earthquake_compare(
    ds: Dataset,
    quake: dict,
    *,
    metric: str = "cm",
    window_days: int = 120,
    radius_km: float = 0.0,
) -> dict:
    """Return the metric before, across and after an earthquake, per frame over it."""
    from nisar_db.events import frames_at

    frames = []
    for f in frames_at(ds.features, quake["lon"], quake["lat"], radius_km):
        p = f["properties"]
        h = fh.event_qa_history(
            p.get("gunw_ifgs") or [],
            quake["time"],
            metric,
            window_days=window_days,
            granules=p.get("granules"),
        )
        frames.append(
            {
                **_head(p),
                "median": h["median"],
                "change_pct": h["change_pct"],
                "n": h["n"],
                "pairs": h["pairs"],
            }
        )
    return {
        "event": quake,
        "metric": metric,
        "window_days": window_days,
        "frames": frames,
    }


# -- exports ---------------------------------------------------------------------------

#: Frame columns an export carries.
FRAME_COLUMNS = [
    "id",
    "frame_idx",
    "track",
    "frame",
    "passDirection",
    "gslc_count",
    "n_modes",
    "cons_mode",
    "cons_cov",
    "gunw_count",
    "isCalVal",
    "hasLand",
    "rollout",
    "blackout_label",
    "n_selected",
]
PAIR_COLUMNS = [
    "frame_id",
    "frame_idx",
    "ref",
    "sec",
    "dt",
    "mode",
    "cov",
    "pol",
    "gid",
    "coherence_median",
    "valid_pct",
    "components",
]
GRANULE_COLUMNS = [
    "frame_id",
    "frame_idx",
    "date",
    "cycle",
    "mode",
    "cov",
    "pol",
    "gid",
]


def export_frames(ds: Dataset, q: FrameQuery, fmt: str) -> str:
    """Return the filtered frames as CSV, GeoJSON or KML."""
    from nisar_db.exports import to_csv, to_geojson, to_kml

    sel = store.select(ds, q)
    if fmt == "csv":
        return to_csv([store.frame_summary(f, q) for f in sel], FRAME_COLUMNS)
    feats = [
        {"geometry": f["geometry"], "properties": store.frame_summary(f, q)}
        for f in sel
    ]
    if fmt == "geojson":
        return to_geojson(feats, FRAME_COLUMNS)
    if fmt == "kml":
        return to_kml(feats, name=f"NISAR frames ({ds.id})", properties=FRAME_COLUMNS)
    raise ValueError(f"unknown format {fmt!r}; csv, geojson or kml")


def export_entries(ds: Dataset, q: FrameQuery, fmt: str) -> str:
    """Return the filtered frames' pairs (GUNW) or granules (GSLC) as CSV."""
    from nisar_db.exports import to_csv

    if fmt != "csv":
        raise ValueError("pairs and granules export as csv")
    rows = []
    for f in store.select(ds, q):
        p = f["properties"]
        for e in q.entries(p):
            row = {"frame_id": p["id"], "frame_idx": p["frame_idx"], **e}
            qa = e.get("qa") or {}
            row.update(
                coherence_median=qa.get("cm"),
                valid_pct=qa.get("v"),
                components=qa.get("n"),
            )
            rows.append(row)
    return to_csv(rows, PAIR_COLUMNS if q.product == "gunw" else GRANULE_COLUMNS)
