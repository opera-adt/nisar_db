"""Browse and QA images of a granule, sized for a person or a model to look at.

``browse`` is ASF's public browse PNG (GSLC backscatter, GUNW unwrapped phase);
it needs no login. The other layers come from the product's QA report, which
the viewer helper fetches with the Earthdata login and splits into images:
wrapped phase, coherence, connected components, unwrapped phase, ionosphere.
"""

from __future__ import annotations

import io
import re
import threading
from collections import OrderedDict
from typing import Any

import requests

GID = re.compile(r"^NISAR_L2_PR_(GSLC|GUNW)_[A-Za-z0-9_]+$")
COLLECTIONS = {
    "GSLC": "NISAR_L2_GSLC_PROVISIONAL_V1",
    "GUNW": "NISAR_L2_GUNW_PROVISIONAL_V1",
}
BROWSE_URL = "https://nisar.asf.earthdatacloud.nasa.gov/BROWSE/{collection}/{gid}/{gid}_LATLON.png"

#: Layer names and what they show; all but ``browse`` come from the QA report.
LAYERS = {
    "browse": "Public browse (GSLC backscatter / GUNW unwrapped phase)",
    "wrapped": "Wrapped phase",
    "coherence": "Coherence",
    "coherence_wrapped": "Coherence (wrapped group)",
    "cc": "Connected components",
    "unwrapped": "Unwrapped phase",
    "rewrapped": "Unwrapped, rewrapped",
    "iono": "Ionosphere screen",
    "iono_unc": "Ionosphere uncertainty",
}

#: The QA layer that shows each QA metric best.
LAYER_FOR_METRIC = {
    "cm": "coherence",
    "ca": "coherence",
    "v": "cc",
    "l": "cc",
    "n": "cc",
    "im": "iono",
    "imd": "iono",
    "is": "iono",
    "iu": "iono_unc",
    "rl": "browse",
}

_CACHE: OrderedDict[tuple, tuple[bytes, dict]] = OrderedDict()
_LOCK = threading.Lock()
_CACHE_SIZE = 64


def browse_url(gid: str) -> str:
    """Return the public browse PNG's URL for a GSLC or GUNW granule.

    >>> browse_url("NISAR_L2_PR_GUNW_x")[-29:]
    'NISAR_L2_PR_GUNW_x_LATLON.png'
    """
    kind = gid.split("_")[3]
    return BROWSE_URL.format(collection=COLLECTIONS[kind], gid=gid)


def _shrink(raw: bytes, max_side: int) -> tuple[bytes, dict]:
    """Return a JPEG at most ``max_side`` pixels on its long side."""
    from PIL import Image

    img = Image.open(io.BytesIO(raw))
    size = img.size
    img.thumbnail((max_side, max_side))
    if img.mode not in ("RGB", "L"):
        # Transparent areas (outside the swath) go white, not black.
        rgba = img.convert("RGBA")
        img = Image.new("RGB", rgba.size, "white")
        img.paste(rgba, mask=rgba.getchannel("A"))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    return out.getvalue(), {"original_size": list(size), "size": list(img.size)}


def fetch_image(
    gid: str,
    layer: str = "browse",
    *,
    helper: Any = None,
    max_side: int = 768,
    session: requests.Session | None = None,
) -> tuple[bytes, dict]:
    """Return one image of a granule as JPEG bytes, with what it is.

    Parameters
    ----------
    gid : str
        A GSLC or GUNW granule name.
    layer : str
        ``"browse"`` or a QA-report layer (:data:`LAYERS`); QA layers exist for
        GUNW only.
    helper : nisar_db.api.helper.Helper, optional
        The viewer helper, whose QA cache fetches and splits the report.
    max_side : int
        Longest side of the returned image, in pixels.
    session : requests.Session, optional
        For the public browse download (tests pass a fake).

    Raises
    ------
    ValueError
        For a bad granule name or layer, or a QA layer without the helper.
    LookupError
        When the report has no such layer.
    requests.RequestException
        When the download fails.

    """
    if not GID.match(gid):
        raise ValueError(f"not a NISAR GSLC / GUNW granule name: {gid!r}")
    if layer not in LAYERS:
        raise ValueError(f"unknown layer {layer!r}; known: {sorted(LAYERS)}")
    key = (gid, layer, max_side)
    with _LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    if layer == "browse":
        url = browse_url(gid)
        r = (session or requests).get(url, timeout=60)
        r.raise_for_status()
        raw, source = r.content, url
    else:
        if gid.split("_")[3] != "GUNW":
            raise ValueError(
                f"QA layers exist for GUNW pairs only; use layer='browse' for {gid}"
            )
        if helper is None:
            raise ValueError(
                "QA layers need the viewer helper (a nisar_db checkout); "
                "use layer='browse'"
            )
        names = helper.cache.layers(gid)
        if layer not in names:
            raise LookupError(
                f"the QA report of {gid} has no {layer!r} layer; it has {names}"
            )
        raw = (helper.cache.root / gid / f"{layer}.png").read_bytes()
        source = "QA report"
    data, meta = _shrink(raw, max_side)
    meta.update(gid=gid, layer=layer, label=LAYERS[layer], source=source)
    with _LOCK:
        _CACHE[key] = (data, meta)
        while len(_CACHE) > _CACHE_SIZE:
            _CACHE.popitem(last=False)
    return data, meta


def pick_qa_examples(
    report: dict,
    entries: list[dict],
    metric: str,
    *,
    n: int = 2,
    granules: list[dict] | None = None,
) -> list[dict]:
    """Choose what to look at after a QA-drop check.

    The worst flagged entries and, for comparison, the unflagged one closest
    to the stack median.

    >>> rep = {"median": 0.6, "flagged": [{"gid": "b", "score": 9.0, "value": 0.1}]}
    >>> es = [{"gid": g, "qa": {"cm": v}}
    ...       for g, v in (("a", 0.59), ("b", 0.1), ("c", 0.7))]
    >>> [(x["role"], x["gid"]) for x in pick_qa_examples(rep, es, "cm")]
    [('flagged', 'b'), ('typical', 'a')]
    """
    from nisar_db.qa_drops import value_of

    picks = [{"role": "flagged", **f} for f in report.get("flagged", [])[: max(0, n)]]
    flagged = {f.get("gid") for f in report.get("flagged", [])}
    med = report.get("median")
    best = None
    for e in entries:
        if e.get("gid") in flagged or med is None:
            continue
        v = value_of(e, metric, granules)
        if v is not None and (best is None or abs(v - med) < best[0]):
            best = (abs(v - med), e, v)
    if best is not None:
        e, v = best[1], best[2]
        picks.append(
            {
                "role": "typical",
                **{k: e[k] for k in ("date", "ref", "sec", "dt", "gid") if k in e},
                "value": v,
                "median": med,
            }
        )
    return [p for p in picks if p.get("gid")]


def browse_overlay(
    gid: str,
    layer: str = "browse",
    *,
    dataset: str = "published",
    base: str,
    helper: Any = None,
) -> dict[str, Any]:
    """Return how to put a granule's image on a map.

    ``viewer_url`` opens the viewer with the image placed; ``image_url`` and
    ``coordinates`` (top-left, top-right, bottom-right, bottom-left, as
    lon/lat) suit any MapLibre ``image`` source. The public browse is a
    lon/lat grid placed by its bounding box; QA layers sit on the product's own
    grid, placed by its four corners. Corners need the viewer helper.

    Raises
    ------
    ValueError
        For a bad granule name or layer.

    """
    if not GID.match(gid):
        raise ValueError(f"not a NISAR GSLC / GUNW granule name: {gid!r}")
    if layer not in LAYERS:
        raise ValueError(f"unknown layer {layer!r}; known: {sorted(LAYERS)}")
    from urllib.parse import urlencode

    base = base.rstrip("/")
    path = "/" if dataset == "published" else f"/view/{dataset}"
    query = {
        "browse": gid,
        "browse_map": 1,
        **({"browse_layer": layer} if layer != "browse" else {}),
    }
    out: dict[str, Any] = {
        "gid": gid,
        "layer": layer,
        "label": LAYERS[layer],
        "viewer_url": f"{base}{path}?{urlencode(query)}",
        "image_url": f"{base}/api/v1/browse/{gid}?{urlencode({'layer': layer})}",
        "coordinates": None,
    }
    if helper is None:
        out["note"] = (
            "corners need the viewer helper (a nisar_db checkout); "
            "the viewer link still works"
        )
        return out
    try:
        corners = helper.cache.corners(gid)
    except Exception as exc:
        out["note"] = (
            "could not read the grid corners: "
            f"{type(exc).__name__}: {str(exc).split('?')[0]}"
        )
        return out
    if layer == "browse":
        w, s, e, n = corners["bbox"]
        out["coordinates"] = [[w, n], [e, n], [e, s], [w, s]]
    else:
        out["coordinates"] = corners["quad"]
    out["epsg"] = corners.get("epsg")
    return out
