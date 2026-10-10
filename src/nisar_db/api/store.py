"""The frames the API answers from: the same data the viewer shows.

Each built viewer page embeds its frames (``FRAME_DATA``) and how it was built
(``META``). The API reads them as datasets: ``published`` is the checked-in
North America page, and every view a rebuild wrote (``globe-...``, ``na-...``,
``bbox-...``) is one more. A dataset is parsed on first use and again only when
its file changes.
"""

from __future__ import annotations

import json
import re
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable

#: Frame fields that are lists of granules / pairs: left out of summaries.
HEAVY_FIELDS = ("granules", "gunw_ifgs")
VIEW_ID = re.compile(r"^(na|globe|bbox)-\d{8}T\d{6}$")


def _embedded(text: str, name: str) -> Any:
    """Return the JSON literal assigned to ``const <name>`` in a page."""
    marker = f"const {name} = "
    start = text.index(marker) + len(marker)
    value, _ = json.JSONDecoder().raw_decode(text, start)
    return value


def as_list(value: Any) -> list:
    """Return a frame field that older pages stored as a JSON string, as a list."""
    if isinstance(value, str):
        return json.loads(value)
    return list(value or [])


@dataclass
class Dataset:
    """One viewer page's frames, indexed for lookups."""

    id: str
    path: Path
    meta: dict
    features: list[dict]
    mtime: float

    def __post_init__(self) -> None:
        self.by_key: dict[str, dict] = {}
        for f in self.features:
            p = f["properties"]
            self.by_key[str(p["id"])] = f
            self.by_key[str(p["frame_idx"])] = f
            # Older pages carry granule lists as strings; parse once, here.
            for field in HEAVY_FIELDS:
                if field in p:
                    p[field] = as_list(p[field])

    def frame(self, key: str | int) -> dict | None:
        """Return a frame by ``frame_idx`` (``8109``) or ``track_frame`` (``34_19``)."""
        key = (
            str(key)
            .strip()
            .upper()
            .replace("T", "")
            .replace("F", "_")
            .replace("__", "_")
        )
        return self.by_key.get(key.lstrip("_"))

    def summary(self) -> dict:
        """Return the dataset's header: id, file, scope and counts."""
        return summarize_meta(self.id, self.path, self.meta, len(self.features))


def summarize_meta(
    dataset_id: str, path: Path, meta: dict, n_frames: int | None
) -> dict:
    """Return a dataset's header from its page's ``META``."""
    m = meta
    return {
        "id": dataset_id,
        "file": path.name,
        "scope": m.get("view_scope", "na"),
        "label": m.get("view_label", "North America"),
        "generated_at": m.get("generated_at"),
        "catalog_queried_at": m.get("catalog_queried_at"),
        "n_frames": n_frames,
        "n_frames_with_gslc": m.get("n_frames_with_gslc"),
        "n_granules": m.get("n_granules"),
        "n_gunw": m.get("n_gunw"),
        "has_blackout": bool(m.get("has_blackout")),
        "has_reference": bool(m.get("has_reference")),
        "has_flags": bool(m.get("has_flags")),
        "has_qa": bool(m.get("has_qa")),
        "rollout_options": m.get("rollout_options") or [],
    }


class FrameStore:
    """Datasets by id, parsed lazily and refreshed when their file changes."""

    def __init__(self, published: Path | None, views_dir: Path | None) -> None:
        """Read the published page and the rebuilt views in ``views_dir``."""
        self.published = published
        self.views_dir = views_dir
        self._cache: dict[str, Dataset] = {}
        self._lock = threading.Lock()
        self._volcanoes: list[dict] | None = None

    def paths(self) -> dict[str, Path]:
        """Return every dataset id with its page, the published one first."""
        out: dict[str, Path] = {}
        if self.published is not None and self.published.exists():
            out["published"] = self.published
        if self.views_dir is not None and self.views_dir.is_dir():
            for p in sorted(self.views_dir.glob("*.html"), reverse=True):
                if VIEW_ID.match(p.stem):
                    out[p.stem] = p
        return out

    def header(self, dataset_id: str) -> dict:
        """Return a dataset's header, reading only its ``META`` (not its frames).

        A loaded dataset answers from memory; otherwise the page is read but its
        frames are not parsed, so listing a 100 MB globe view stays cheap.

        Raises
        ------
        KeyError
            For an id with no page.

        """
        path = self.paths().get(dataset_id)
        if path is None:
            raise KeyError(dataset_id)
        cached = self._cache.get(dataset_id)
        if cached is not None and cached.mtime == path.stat().st_mtime:
            return cached.summary()
        meta = _embedded(path.read_text(), "META")
        return summarize_meta(dataset_id, path, meta, meta.get("n_frames"))

    def volcanoes(self) -> list[dict]:
        """Return the GVP volcano list the published page embeds (empty if none)."""
        if self._volcanoes is None:
            from nisar_db.events import volcano_list

            feats: list[dict] = []
            if self.published is not None and self.published.exists():
                try:
                    feats = _embedded(self.published.read_text(), "VOLCANO_DATA")[
                        "features"
                    ]
                except (ValueError, KeyError):
                    feats = []
            self._volcanoes = volcano_list(feats)
        return self._volcanoes

    def get(self, dataset_id: str = "published") -> Dataset:
        """Return a dataset, parsing (or re-parsing) its page when needed.

        Raises
        ------
        KeyError
            For an id with no page.

        """
        path = self.paths().get(dataset_id)
        if path is None:
            raise KeyError(dataset_id)
        mtime = path.stat().st_mtime
        with self._lock:
            cached = self._cache.get(dataset_id)
            if cached is None or cached.mtime != mtime:
                text = path.read_text()
                cached = Dataset(
                    id=dataset_id,
                    path=path,
                    meta=_embedded(text, "META"),
                    features=_embedded(text, "FRAME_DATA")["features"],
                    mtime=mtime,
                )
                self._cache[dataset_id] = cached
            return cached


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def int_set(text: str | None) -> set[int] | None:
    """Parse ``"12"``, ``"10-20"`` or ``"12,34,40-42"`` into a set (None for empty)."""
    if not text or not str(text).strip():
        return None
    out: set[int] = set()
    for raw in str(text).split(","):
        part = raw.strip()
        if not part:
            continue
        if "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
            out.update(range(min(a, b), max(a, b) + 1))
        else:
            out.add(int(part))
    return out


def _bounds(geometry: dict) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []

    def walk(c: Any) -> None:
        if c and isinstance(c[0], (int, float)):
            xs.append(c[0])
            ys.append(c[1])
        else:
            for x in c:
                walk(x)

    walk(geometry["coordinates"])
    return min(xs), min(ys), max(xs), max(ys)


@dataclass
class FrameQuery:
    """Filters on frames, mirroring the viewer's sidebar."""

    product: str = "gslc"
    track: str | None = None
    frame: str | None = None
    direction: str | None = None
    bbox: tuple[float, float, float, float] | None = None
    cycle: str | None = None
    start: date | None = None
    end: date | None = None
    modes: tuple[str, ...] = ()
    pols: tuple[str, ...] = ()
    crids: tuple[str, ...] = ()
    has_data: bool | None = None
    calval: bool | None = None
    land: bool | None = None
    rollout: tuple[str, ...] = ()
    consistent_mode: str | None = None

    def entries(self, p: dict) -> list[dict]:
        """Return the frame's granules (GSLC) or pairs (GUNW) passing the filters."""
        cycles = int_set(self.cycle)
        gunw = self.product == "gunw"
        out = []
        for g in p.get("gunw_ifgs" if gunw else "granules") or []:
            gid = str(g.get("gid", ""))
            parts = gid.split("_")
            if self.modes and str(g.get("mode")) not in self.modes:
                continue
            if self.pols and str(g.get("pol")) not in self.pols:
                continue
            if self.crids:
                crid = (
                    parts[15 if gunw else 13]
                    if len(parts) > (15 if gunw else 13)
                    else ""
                )
                if crid not in self.crids:
                    continue
            if gunw:
                cyc = [
                    int(parts[i])
                    for i in (4, 8)
                    if len(parts) > i and parts[i].isdigit()
                ]
                dates = [g.get("ref"), g.get("sec")]
            else:
                cyc = [int(g["cycle"])] if str(g.get("cycle", "")).isdigit() else []
                dates = [g.get("date")]
            if cycles is not None and not cycles.intersection(cyc):
                continue
            if self.start or self.end:
                ds = [date.fromisoformat(d) for d in dates if d]
                if self.start and not any(d >= self.start for d in ds):
                    continue
                if self.end and not any(d <= self.end for d in ds):
                    continue
            out.append(g)
        return out

    def _entry_filtered(self) -> bool:
        return bool(
            self.cycle
            or self.start
            or self.end
            or self.modes
            or self.pols
            or self.crids
        )

    def match(self, f: dict) -> bool:
        """Return whether a frame passes every filter."""
        p = f["properties"]
        tracks, frames = int_set(self.track), int_set(self.frame)
        if tracks is not None and p["track"] not in tracks:
            return False
        if frames is not None and p["frame"] not in frames:
            return False
        if self.direction and not str(p.get("passDirection", "")).upper().startswith(
            self.direction[0].upper()
        ):
            return False
        if self.calval is not None and bool(p.get("isCalVal")) != self.calval:
            return False
        if self.land is not None and (p.get("hasLand") is not False) != self.land:
            return False
        if self.rollout:
            ro = as_list(p.get("rollout"))
            if not (set(ro) & set(self.rollout) or (not ro and "none" in self.rollout)):
                return False
        if self.consistent_mode and str(p.get("cons_mode")) != self.consistent_mode:
            return False
        if self.bbox:
            w, s, e, n = self.bbox
            fw, fs, fe, fn = _bounds(f["geometry"])
            if fe < w or fw > e or fn < s or fs > n:
                return False
        if self._entry_filtered() or self.has_data is not None:
            n_entries = len(self.entries(p))
            # Like the viewer's cycle filter: frames with nothing left drop out.
            if self._entry_filtered() and self.has_data is None and n_entries == 0:
                return False
            if self.has_data is not None and (n_entries > 0) != self.has_data:
                return False
        return True


def frame_summary(f: dict, query: FrameQuery | None = None) -> dict:
    """Return a frame's properties without its granule / pair lists.

    With a query, ``n_selected`` counts the granules or pairs it keeps.
    """
    p = {
        k: v
        for k, v in f["properties"].items()
        if k not in HEAVY_FIELDS and not k.startswith("_")
    }
    if query is not None:
        p["n_selected"] = len(query.entries(f["properties"]))
    return p


def select(ds: Dataset, query: FrameQuery) -> list[dict]:
    """Return the dataset's frames passing ``query``."""
    return [f for f in ds.features if query.match(f)]


def cycles(ds: Dataset, product: str = "gslc") -> list[dict]:
    """Return every cycle with its date span and how many frames hold it."""
    spans: dict[int, list] = {}
    for f in ds.features:
        p = f["properties"]
        seen: set[int] = set()
        if product == "gunw":
            for g in p.get("gunw_ifgs") or []:
                parts = str(g.get("gid", "")).split("_")
                for i, d in ((4, g.get("ref")), (8, g.get("sec"))):
                    if len(parts) > i and parts[i].isdigit():
                        c = int(parts[i])
                        _see(spans, c, d)
                        seen.add(c)
        else:
            for g in p.get("granules") or []:
                if str(g.get("cycle", "")).isdigit():
                    c = int(g["cycle"])
                    _see(spans, c, g.get("date"))
                    seen.add(c)
        for c in seen:
            spans[c][2] += 1
    return [
        {"cycle": c, "start": s[0], "end": s[1], "n_frames": s[2]}
        for c, s in sorted(spans.items())
    ]


def _see(spans: dict, c: int, d: str | None) -> None:
    s = spans.setdefault(c, [None, None, 0])
    if d:
        s[0] = d if s[0] is None or d < s[0] else s[0]
        s[1] = d if s[1] is None or d > s[1] else s[1]


def consistent_summary(features: Iterable[dict]) -> dict:
    """Return the viewer's Consistent Mode Summary over ``features``."""
    feats = list(features)
    modes: dict[str, int] = {}
    full = partial = multi = with_gslc = 0
    for f in feats:
        p = f["properties"]
        with_gslc += p.get("gslc_count", 0) > 0
        full += p.get("cons_cov") == "F"
        partial += p.get("cons_cov") == "P"
        multi += p.get("n_modes", 0) > 1
        key = p.get("cons_mode") if p.get("cons_mode") not in (None, "none") else "none"
        modes[str(key)] = modes.get(str(key), 0) + 1
    return {
        "n_frames": len(feats),
        "with_gslc": with_gslc,
        "full_frame": full,
        "partial": partial,
        "multi_mode": multi,
        "frames_per_mode": dict(sorted(modes.items(), key=lambda kv: -kv[1])),
    }


def rollout_summary(ds: Dataset, features: Iterable[dict]) -> list[dict]:
    """Return frames per rollout option among ``features`` and in the whole dataset."""
    options = [*list(ds.meta.get("rollout_options") or []), "none"]
    feats = list(features)

    def count(fs: list[dict], opt: str) -> int:
        return sum(
            1
            for f in fs
            if (opt in as_list(f["properties"].get("rollout")))
            or (opt == "none" and not as_list(f["properties"].get("rollout")))
        )

    return [
        {"option": o, "shown": count(feats, o), "total": count(ds.features, o)}
        for o in options
    ]


def blackout(f: dict) -> dict:
    """Return a frame's blackout windows and the share of each month they cover."""
    p = f["properties"]
    ranges = as_list(p.get("blackout_ranges"))
    covered = [0.0] * 12
    for r in ranges:
        a, b = (date.fromisoformat(x.strip()) for x in r.split("->"))
        d = a
        while d <= b:
            nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
            end = min(b, date.fromordinal(nxt.toordinal() - 1))
            days_in = (nxt - date(d.year, d.month, 1)).days
            covered[d.month - 1] += ((end - d).days + 1) / days_in
            d = nxt
    shares = [round(min(1.0, c / len(ranges)), 3) if ranges else 0.0 for c in covered]
    return {
        "frame_idx": p["frame_idx"],
        "has_blackout": bool(p.get("has_blackout")),
        "label": p.get("blackout_label"),
        "months": p.get("blackout_months"),
        "ranges": ranges,
        "month_share": dict(
            zip(
                (
                    "Jan",
                    "Feb",
                    "Mar",
                    "Apr",
                    "May",
                    "Jun",
                    "Jul",
                    "Aug",
                    "Sep",
                    "Oct",
                    "Nov",
                    "Dec",
                ),
                shares,
            )
        ),
        "reference_dates": as_list(p.get("reference_dates")),
    }


def qa_drops_scan(
    ds: Dataset,
    query: FrameQuery,
    metric: str,
    *,
    limit: int = 50,
    **opts: Any,
) -> dict:
    """Run :func:`nisar_db.qa_drops.find_drops` over every frame ``query`` keeps.

    Each frame's stack is the granules or pairs the query's entry filters
    (cycle, dates, modes, polarizations) leave. Frames come back worst first
    (most flagged dates, then pairs); ``dates`` counts in how many frames each
    date was flagged, which picks out events wider than one frame.
    """
    from nisar_db.qa_drops import find_drops

    frames, by_date = [], defaultdict(list)
    judged = 0
    for f in select(ds, query):
        p = f["properties"]
        report = find_drops(
            query.entries(p),
            metric,
            product=query.product,  # type: ignore[arg-type]
            granules=p.get("granules"),
            **opts,
        )
        if report["status"] != "ok":
            continue
        judged += 1
        if not report["flagged"]:
            continue
        for d in report["dates"]:
            by_date[d["date"]].append(p["id"])
        frames.append(
            {
                "id": p["id"],
                "frame_idx": p["frame_idx"],
                "n": report["n"],
                "median": report["median"],
                "n_flagged": len(report["flagged"]),
                "dates": [d["date"] for d in report["dates"]],
                "worst": report["flagged"][0],
            }
        )
    frames.sort(key=lambda r: (-len(r["dates"]), -r["n_flagged"], -r["worst"]["score"]))
    dates = sorted(
        (
            {"date": d, "n_frames": len(ids), "frames": ids[:20]}
            for d, ids in by_date.items()
        ),
        key=lambda r: (-r["n_frames"], r["date"]),
    )
    return {
        "metric": metric,
        "product": query.product,
        "frames_judged": judged,
        "frames_flagged": len(frames),
        "dates": dates[:limit],
        "frames": frames[:limit],
    }


def duplicates_scan(ds: Dataset, query: FrameQuery, *, limit: int = 50) -> dict:
    """List the frames ``query`` keeps that hold duplicate granules (or pairs).

    The duplicate test runs on what the query's entry filters (cycle, dates,
    modes, polarizations) leave. Frames come back with the most extra granules
    first, with totals by reason (reprocessed, split, repeat).
    """
    from nisar_db.duplicates import duplicate_groups, summarize

    frames: list[dict] = []
    n_groups = n_extra = judged = 0
    by_reason: dict[str, int] = defaultdict(int)
    for f in select(ds, query):
        p = f["properties"]
        judged += 1
        groups = duplicate_groups(query.entries(p), query.product)  # type: ignore[arg-type]
        if not groups:
            continue
        s = summarize(groups)
        n_groups += s["groups"]
        n_extra += s["extra_granules"]
        for reason, n in s["by_reason"].items():
            by_reason[reason] += n
        frames.append(
            {
                "id": p["id"],
                "frame_idx": p["frame_idx"],
                **s,
                "dates": sorted({str(g.get("date") or g.get("ref")) for g in groups}),
            }
        )
    frames.sort(key=lambda r: (-r["extra_granules"], r["id"]))
    return {
        "product": query.product,
        "frames_judged": judged,
        "frames_with_duplicates": len(frames),
        "groups": n_groups,
        "extra_granules": n_extra,
        "by_reason": dict(sorted(by_reason.items())),
        "frames": frames[:limit],
    }


def _pair_links(pair: dict, base: str | None, dataset: str) -> dict:
    """Return a pair's summary with links to its browse image, overlay and viewer."""
    from urllib.parse import urlencode

    out = {
        k: pair.get(k)
        for k in ("gid", "ref", "sec", "dt", "mode", "pol")
        if pair.get(k) is not None
    }
    qa = pair.get("qa") or {}
    if qa:
        out["coherence_median"] = qa.get("cm")
        out["valid_pct"] = qa.get("v")
    if base and pair.get("gid"):
        gid = pair["gid"]
        path = "/" if dataset == "published" else f"/view/{dataset}"
        out["browse_url"] = f"{base}/api/v1/browse/{gid}"
        out["overlay_url"] = f"{base}/api/v1/browse/{gid}/overlay?dataset={dataset}"
        out["viewer_url"] = f"{base}{path}?" + urlencode(
            {"product": "gunw", "browse": gid, "browse_map": 1}
        )
    return out


def earthquake_report(
    ds: Dataset,
    quake: dict,
    *,
    radius_km: float = 0.0,
    max_dt: int | None = None,
    n_pairs: int = 3,
    base: str | None = None,
) -> dict:
    """Return the frames over an earthquake and, in each, its coseismic GUNW pairs.

    Pairs come shortest temporal baseline first; the GSLC acquisitions just
    before and after the event are listed too, for when no pair spans it yet.
    """
    from urllib.parse import urlencode

    from nisar_db.events import acquisitions_around, coseismic_pairs, frames_at

    frames = []
    for f in frames_at(ds.features, quake["lon"], quake["lat"], radius_km):
        p = f["properties"]
        pairs = coseismic_pairs(p.get("gunw_ifgs") or [], quake["time"], max_dt=max_dt)
        frames.append(
            {
                "id": p["id"],
                "frame_idx": p["frame_idx"],
                "passDirection": p.get("passDirection"),
                "n_coseismic": len(pairs),
                "coseismic_pairs": [
                    _pair_links(x, base, ds.id) for x in pairs[: max(0, n_pairs)]
                ],
                "gslc": acquisitions_around(p.get("granules") or [], quake["time"]),
            }
        )
    frames.sort(key=lambda r: (-r["n_coseismic"], r["id"]))
    out: dict[str, Any] = {
        "event": quake,
        "dataset": ds.id,
        "frames": frames,
        "status": (
            "coseismic pairs found"
            if any(r["n_coseismic"] for r in frames)
            else (
                "no GUNW pair spans the event in this dataset"
                if frames
                else "no frame of this dataset covers the epicentre"
            )
        ),
    }
    if base:
        path = "/" if ds.id == "published" else f"/view/{ds.id}"
        out["viewer_url"] = f"{base}{path}?" + urlencode(
            {"product": "gunw", "center": f"{quake['lon']},{quake['lat']}", "zoom": 7}
        )
    return out


def volcano_report(
    ds: Dataset,
    volcano: dict,
    *,
    radius_km: float = 10.0,
    start: date | None = None,
    end: date | None = None,
    n_pairs: int = 5,
    base: str | None = None,
) -> dict:
    """Return the frames over a volcano and their GUNW pairs in a date window."""
    from urllib.parse import urlencode

    from nisar_db.events import frames_at

    q = FrameQuery(product="gunw", start=start, end=end)
    frames = []
    for f in frames_at(ds.features, volcano["lon"], volcano["lat"], radius_km):
        p = f["properties"]
        pairs = sorted(
            q.entries(p),
            key=lambda x: (str(x.get("sec")), -(x.get("dt") or 0)),
            reverse=True,
        )
        frames.append(
            {
                "id": p["id"],
                "frame_idx": p["frame_idx"],
                "passDirection": p.get("passDirection"),
                "n_pairs": len(pairs),
                "pairs": [
                    _pair_links(x, base, ds.id) for x in pairs[: max(0, n_pairs)]
                ],
            }
        )
    frames.sort(key=lambda r: (-r["n_pairs"], r["id"]))
    out: dict[str, Any] = {"volcano": volcano, "dataset": ds.id, "frames": frames}
    if base:
        path = "/" if ds.id == "published" else f"/view/{ds.id}"
        out["viewer_url"] = f"{base}{path}?" + urlencode(
            {
                "product": "gunw",
                "center": f"{volcano['lon']},{volcano['lat']}",
                "zoom": 8,
            }
        )
    return out
