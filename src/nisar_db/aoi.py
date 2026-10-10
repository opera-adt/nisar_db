"""Frames over an area of interest (a polygon, a GeoJSON geometry or a box).

Each frame that touches the area comes back with the share of the area it
covers, and the area's total coverage by all of them. Shares are computed on
an equal-area projection centred on the area, so they hold at any latitude.

Examples
--------
>>> square = [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]]
>>> frames = [{"geometry": {"type": "Polygon", "coordinates": square},
...            "properties": {"id": "a"}}]
>>> r = aoi_frames(frames, box=(1, 1, 3, 3))
>>> r["frames"][0]["id"], r["frames"][0]["aoi_share"], r["covered_share"]
('a', 0.2501, 0.2501)

"""

from __future__ import annotations

from typing import Any, Iterable


def _area_geometry(
    geometry: dict | None, box: tuple[float, float, float, float] | None
):
    from shapely.geometry import box as make_box
    from shapely.geometry import shape

    if geometry is not None:
        geom = shape(
            geometry.get("geometry", geometry)
            if geometry.get("type") == "Feature"
            else geometry
        )
    elif box is not None:
        geom = make_box(*box)
    else:
        raise ValueError("give a GeoJSON geometry or a box")
    if geom.is_empty or geom.area == 0:
        raise ValueError("the area is empty")
    return geom


def aoi_frames(
    features: Iterable[dict],
    *,
    geometry: dict | None = None,
    box: tuple[float, float, float, float] | None = None,
    min_share: float = 0.0,
) -> dict[str, Any]:
    """Return the frames over an area, with the share of the area each covers.

    Parameters
    ----------
    features : iterable of dict
        Frame features (GeoJSON).
    geometry : dict, optional
        A GeoJSON geometry or Feature (Polygon / MultiPolygon).
    box : tuple of float, optional
        west, south, east, north, instead of a geometry.
    min_share : float
        Leave out frames covering less than this share of the area (0-1).

    Returns
    -------
    dict
        ``area_km2``, ``covered_share`` (by all frames together) and
        ``frames`` (largest share first): ``id``, ``aoi_share``, track,
        frame, pass direction and the feature for the caller to use.

    Raises
    ------
    ValueError
        Without a geometry or box, or for an empty area.

    """
    from pyproj import Geod
    from shapely.geometry import shape
    from shapely.ops import unary_union

    area = _area_geometry(geometry, box)
    geod = Geod(ellps="WGS84")

    def km2(g) -> float:
        return abs(geod.geometry_area_perimeter(g)[0]) / 1e6

    total = km2(area)
    hits, parts = [], []
    for f in features:
        g = shape(f["geometry"])
        if not g.intersects(area):
            continue
        inter = g.intersection(area)
        share = km2(inter) / total if total else 0.0
        if share < min_share or share == 0:
            continue
        parts.append(inter)
        p = f.get("properties", {})
        hits.append(
            {
                "id": p.get("id"),
                "frame_idx": p.get("frame_idx"),
                "track": p.get("track"),
                "frame": p.get("frame"),
                "passDirection": p.get("passDirection"),
                "aoi_share": round(share, 4),
                "feature": f,
            }
        )
    covered = km2(unary_union(parts)) / total if parts else 0.0
    hits.sort(key=lambda h: (-h["aoi_share"], str(h["id"])))
    return {
        "area_km2": round(total, 1),
        "covered_share": round(min(covered, 1.0), 4),
        "frames": hits,
    }
