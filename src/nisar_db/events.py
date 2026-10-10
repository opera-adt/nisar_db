"""Earthquakes and volcanoes, and the NISAR frames and acquisitions that see them.

Earthquakes come from the USGS FDSN event service (the same feed the viewer's
earthquake layer reads); volcanoes from the Smithsonian GVP Holocene list the
viewer embeds. Frames are matched by their footprint, and a frame's GUNW pairs
are *coseismic* when the reference acquisition is before the event and the
secondary after it, compared by acquisition time (read from the granule name),
so an acquisition on the day of the event lands on the right side.

Examples
--------
>>> pair = {"gid": "NISAR_L2_PR_GUNW_021_013_D_070_022_4000_SH_20260522T024004"
...         "_20260522T024039_20260603T024004_20260603T024039_P05023_N_F_J_001",
...         "ref": "2026-05-22", "sec": "2026-06-03", "dt": 12}
>>> [p["dt"] for p in coseismic_pairs([pair], "2026-05-30T10:00:00Z")]
[12]
>>> coseismic_pairs([pair], "2026-06-03T05:00:00Z")   # after the secondary pass
[]

"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Iterable

import requests

USGS_EVENTS = "https://earthquake.usgs.gov/fdsnws/event/1/query"
KM_PER_DEG = 111.32


def _utc(text: str) -> datetime:
    """Parse an ISO date or time (``Z`` or naive means UTC)."""
    t = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _quake(feature: dict) -> dict[str, Any]:
    p, (lon, lat, depth) = feature["properties"], feature["geometry"]["coordinates"][:3]
    return {
        "id": feature.get("id"),
        "mag": p.get("mag"),
        "mag_type": p.get("magType"),
        "place": p.get("place"),
        "time": (
            datetime.fromtimestamp(p["time"] / 1000, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        ),
        "lon": lon,
        "lat": lat,
        "depth_km": depth,
        "url": p.get("url"),
        "tsunami": bool(p.get("tsunami")),
        "alert": p.get("alert"),
    }


def search_earthquakes(
    *,
    bbox: tuple[float, float, float, float] | None = None,
    center: tuple[float, float] | None = None,
    radius_km: float | None = None,
    start: str | None = None,
    end: str | None = None,
    min_magnitude: float = 4.5,
    limit: int = 50,
    order: str = "time",
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    """Search the USGS catalog.

    Parameters
    ----------
    bbox : tuple of float, optional
        west, south, east, north; longitudes may run past -180 (USGS accepts
        -360..360).
    center, radius_km : optional
        A (lon, lat) circle instead of a box.
    start, end : str, optional
        ISO dates or times (UTC).
    min_magnitude : float
        Smallest magnitude.
    limit : int
        At most this many events (USGS caps at 20000).
    order : {"time", "magnitude"}
        Newest first, or largest first.
    session : requests.Session, optional
        For the HTTP call (tests pass a fake).

    Returns
    -------
    list of dict
        ``id``, ``mag``, ``place``, ``time`` (UTC), ``lon``, ``lat``,
        ``depth_km``, ``url``, ``tsunami``, ``alert``.

    """
    params: dict[str, Any] = {
        "format": "geojson",
        "minmagnitude": min_magnitude,
        "limit": max(1, min(int(limit), 20000)),
        "orderby": "magnitude" if order == "magnitude" else "time",
    }
    if start:
        params["starttime"] = start
    if end:
        params["endtime"] = end
    if bbox is not None:
        w, s, e, n = bbox
        params.update(minlongitude=w, minlatitude=s, maxlongitude=e, maxlatitude=n)
    if center is not None:
        params.update(
            longitude=center[0], latitude=center[1], maxradiuskm=radius_km or 100
        )
    r = (session or requests).get(USGS_EVENTS, params=params, timeout=60)
    r.raise_for_status()
    return [_quake(f) for f in r.json().get("features", [])]


def get_earthquake(
    event_id: str, *, session: requests.Session | None = None
) -> dict[str, Any]:
    """Return one USGS event by id (e.g. ``us6000u18k``).

    Raises
    ------
    LookupError
        When USGS has no such event.

    """
    r = (session or requests).get(
        USGS_EVENTS, params={"format": "geojson", "eventid": event_id}, timeout=60
    )
    if r.status_code == 404:
        raise LookupError(f"USGS has no event {event_id!r}")
    r.raise_for_status()
    return _quake(r.json())


def volcano_list(features: Iterable[dict]) -> list[dict[str, Any]]:
    """Return the GVP volcano features (as the viewer embeds them) as records."""
    out = []
    for f in features:
        p, (lon, lat) = f["properties"], f["geometry"]["coordinates"][:2]
        out.append(
            {
                "vnum": p.get("v"),
                "name": p.get("n"),
                "type": p.get("t"),
                "last_eruption": p.get("y"),
                "elevation_m": p.get("e"),
                "country": p.get("c"),
                "region": p.get("r"),
                "evidence": p.get("ev"),
                "lon": lon,
                "lat": lat,
            }
        )
    return out


def find_volcanoes(
    volcanoes: list[dict],
    *,
    name: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    country: str | None = None,
    erupted_since: int | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Filter volcano records by name (substring), box, country or last eruption.

    >>> v = [{"name": "Mount St. Helens", "country": "United States",
    ...       "lon": -122.18, "lat": 46.2,
    ...       "last_eruption": 2008}]
    >>> [x["name"] for x in find_volcanoes(v, name="helens", erupted_since=2000)]
    ['Mount St. Helens']
    """
    out = []
    for v in volcanoes:
        if name and name.lower() not in str(v.get("name", "")).lower():
            continue
        if country and country.lower() not in str(v.get("country", "")).lower():
            continue
        if erupted_since is not None and not (
            isinstance(v.get("last_eruption"), (int, float))
            and v["last_eruption"] >= erupted_since
        ):
            continue
        if bbox is not None:
            w, s, e, n = bbox
            if not (
                s <= v["lat"] <= n
                and any(w <= v["lon"] + k <= e for k in (-360, 0, 360))
            ):
                continue
        out.append(v)
        if len(out) >= limit:
            break
    return out


def frames_at(
    features: Iterable[dict], lon: float, lat: float, radius_km: float = 0.0
) -> list[dict]:
    """Return the frames whose footprint holds a point (or is within ``radius_km``)."""
    from shapely.geometry import Point, shape

    # A circle in degrees, stretched in longitude by latitude; good enough for
    # "which frames cover this place".
    pt = Point(lon, lat)
    out = []
    for f in features:
        geom = shape(f["geometry"])
        if radius_km <= 0:
            hit = geom.covers(pt)
        else:
            scale = max(math.cos(math.radians(lat)), 0.05)
            hit = geom.distance(Point(lon, lat)) <= radius_km / (KM_PER_DEG * scale)
        if hit:
            out.append(f)
    return out


def _start_times(gid: str) -> tuple[datetime | None, datetime | None]:
    """Return a GUNW pair's reference and secondary start times from its name."""
    parts = str(gid).split("_")
    try:
        return (
            datetime.strptime(parts[11], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc),
            datetime.strptime(parts[13], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc),
        )
    except (IndexError, ValueError):
        return None, None


def coseismic_pairs(
    pairs: Iterable[dict], when: str, *, max_dt: int | None = None
) -> list[dict]:
    """Return the pairs whose reference is before ``when`` and secondary after it.

    Acquisition times come from the granule name; a pair without them falls
    back to its dates (``ref`` before the event day, ``sec`` on or after it).
    Shortest temporal baseline first: the least other deformation and
    decorrelation mixed in.
    """
    t = _utc(when)
    out = []
    for p in pairs:
        ref_t, sec_t = _start_times(p.get("gid", ""))
        if ref_t is None or sec_t is None:
            ok = str(p.get("ref", "")) < t.date().isoformat() <= str(p.get("sec", ""))
        else:
            ok = ref_t < t <= sec_t
        if ok and (max_dt is None or (p.get("dt") or 0) <= max_dt):
            out.append(p)
    return sorted(out, key=lambda p: (p.get("dt") or 0, str(p.get("sec"))))


def acquisitions_around(
    granules: Iterable[dict], when: str, n: int = 2
) -> dict[str, list[str]]:
    """Return the ``n`` GSLC acquisition dates just before and just after ``when``."""
    day = _utc(when).date().isoformat()
    dates = sorted({str(g["date"]) for g in granules if g.get("date")})
    before = [d for d in dates if d < day][-n:]
    after = [d for d in dates if d >= day][:n]
    return {"before": before, "after": after}
