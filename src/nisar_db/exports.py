"""Write frame and pair lists as CSV, GeoJSON or KML for GIS tools.

Examples
--------
>>> print(to_csv([{"id": "34_19", "n": 3}, {"id": "1_61", "n": 0}]), end="")
id,n
34_19,3
1_61,0

"""

from __future__ import annotations

import csv
import io
import json
from typing import Any, Iterable
from xml.sax.saxutils import escape

FORMATS = ("csv", "geojson", "kml")
MEDIA_TYPES = {
    "csv": "text/csv",
    "geojson": "application/geo+json",
    "kml": "application/vnd.google-earth.kml+xml",
}


def _flat(value: Any) -> Any:
    return json.dumps(value) if isinstance(value, (list, dict)) else value


def to_csv(rows: Iterable[dict], columns: list[str] | None = None) -> str:
    """Return rows as CSV; lists and dicts are written as JSON text."""
    rows = list(rows)
    if columns is None:
        columns = []
        for r in rows:
            columns += [k for k in r if k not in columns]
    out = io.StringIO()
    w = csv.DictWriter(
        out, fieldnames=columns, extrasaction="ignore", lineterminator="\n"
    )
    w.writeheader()
    for r in rows:
        w.writerow({k: _flat(r.get(k)) for k in columns})
    return out.getvalue()


def to_geojson(features: Iterable[dict], properties: list[str] | None = None) -> str:
    """Return features as a GeoJSON FeatureCollection.

    Only ``properties`` are kept when given.
    """
    feats = []
    for f in features:
        p = f.get("properties", {})
        keep = {k: p.get(k) for k in properties} if properties else p
        feats.append({"type": "Feature", "geometry": f["geometry"], "properties": keep})
    return json.dumps({"type": "FeatureCollection", "features": feats})


def _rings(geometry: dict) -> list[list]:
    if geometry["type"] == "Polygon":
        return [geometry["coordinates"]]
    if geometry["type"] == "MultiPolygon":
        return list(geometry["coordinates"])
    raise ValueError(f"KML export takes polygons, not {geometry['type']}")


def to_kml(
    features: Iterable[dict],
    *,
    name: str = "NISAR frames",
    label: str = "id",
    properties: list[str] | None = None,
) -> str:
    """Return polygon features as KML placemarks, their properties as ExtendedData.

    >>> tri = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}
    >>> kml = to_kml([{"geometry": tri, "properties": {"id": "34_19"}}])
    >>> "<name>34_19</name>" in kml and "0,0 1,0 1,1 0,0" in kml
    True
    """
    marks = []
    for f in features:
        p = f.get("properties", {})
        keys = properties or [k for k in p if not isinstance(p[k], (list, dict))]
        data = "".join(
            f'<Data name="{escape(str(k))}">'
            f"<value>{escape(str(p.get(k)))}</value></Data>"
            for k in keys
        )
        polys = "".join(
            "<Polygon><outerBoundaryIs><LinearRing><coordinates>"
            + " ".join(f"{x:g},{y:g}" for x, y, *_ in poly[0])
            + "</coordinates></LinearRing></outerBoundaryIs></Polygon>"
            for poly in _rings(f["geometry"])
        )
        geom = (
            polys
            if polys.count("<Polygon>") == 1
            else f"<MultiGeometry>{polys}</MultiGeometry>"
        )
        marks.append(
            f"<Placemark><name>{escape(str(p.get(label, '')))}</name>"
            f"<ExtendedData>{data}</ExtendedData>{geom}</Placemark>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        f"<name>{escape(name)}</name>{''.join(marks)}</Document></kml>\n"
    )
