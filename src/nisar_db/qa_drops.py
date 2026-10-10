"""Find significant QA drops in a frame's stack, and the dates behind them.

A frame's GUNW interferograms (pairs) and GSLC acquisitions carry QA metrics
read from each product's ``QA_STATS.h5``: coherence, valid unwrapped share,
connected components, ionosphere, RFI likelihood. A pair or acquisition is
*flagged* when its value sits far from the stack's median on the metric's bad
side, measured as a robust z-score: the distance from the median in units of
the median absolute deviation (MAD, scaled by 1.4826 to match a standard
deviation for normal data), so one bad pair cannot hide others by inflating
the spread.

Coherence falls with the temporal baseline, so GUNW pairs are compared with
pairs of the same baseline when there are enough of them, and with the whole
stack otherwise.

A bad *acquisition* drags down every pair it is in. A date is therefore
flagged when at least ``min_pairs`` of its pairs, and at least ``min_share`` of
them, are flagged; for GSLC each flagged acquisition flags its date.

Examples
--------
>>> pairs = [{"ref": f"2026-01-{d:02d}", "sec": f"2026-01-{d + 12:02d}", "dt": 12,
...           "qa": {"cm": 0.6}} for d in range(1, 9)]
>>> pairs[3]["qa"]["cm"] = 0.1
>>> report = find_drops(pairs, "cm", product="gunw", min_pairs=1)
>>> [f["ref"] for f in report["flagged"]]
['2026-01-04']

"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from statistics import median
from typing import Any, Iterable, Literal

Product = Literal["gslc", "gunw"]

#: 1 / Phi^-1(3/4): turns a MAD into a standard deviation for normal data.
MAD_TO_SIGMA = 1.4826


@dataclass(frozen=True)
class Metric:
    """A QA metric: where it lives and which way is bad."""

    key: str
    label: str
    #: -1: low is bad (coherence); 1: high is bad (components); 0: either way.
    bad: int
    products: tuple[str, ...]
    #: Smallest spread to judge against, in the metric's units (log10 for log
    #: metrics): a stack with almost no spread (one component in every pair)
    #: would otherwise flag any change at all.
    min_sigma: float = 0.0
    #: Compared on a log10 scale (RFI likelihood spans many decades).
    log: bool = False


METRICS: dict[str, Metric] = {
    m.key: m
    for m in (
        Metric("cm", "Coherence median", -1, ("gunw",), min_sigma=0.02),
        Metric("ca", "Coherence mean", -1, ("gunw",), min_sigma=0.02),
        Metric("v", "Valid unwrapped (%)", -1, ("gunw",), min_sigma=2.0),
        Metric("l", "Largest region (%)", -1, ("gunw",), min_sigma=2.0),
        Metric("n", "Connected components", 1, ("gunw",), min_sigma=1.0),
        Metric("im", "Ionosphere mean (rad)", 0, ("gunw",), min_sigma=0.5),
        Metric("imd", "Ionosphere median (rad)", 0, ("gunw",), min_sigma=0.5),
        Metric("is", "Ionosphere spread (rad)", 1, ("gunw",), min_sigma=0.5),
        Metric("iu", "Ionosphere uncertainty (rad)", 1, ("gunw",), min_sigma=0.1),
        Metric(
            "rl", "RFI likelihood (log10)", 1, ("gunw", "gslc"), min_sigma=0.1, log=True
        ),
    )
}


def robust_stats(values: Iterable[float]) -> tuple[float, float]:
    """Return the median and the MAD-based sigma of ``values``.

    Raises
    ------
    ValueError
        For no values.

    >>> robust_stats([1, 2, 3, 4, 100])
    (3, 1.4826)

    """
    vals = list(values)
    if not vals:
        raise ValueError("no values")
    m = median(vals)
    return m, MAD_TO_SIGMA * median(abs(v - m) for v in vals)


def _value(
    entry: dict, metric: Metric, rfi_by_date: dict[str, float] | None
) -> float | None:
    if metric.key == "rl" and rfi_by_date is not None:
        # A pair's RFI likelihood is the worse of its two acquisitions'.
        found = [rfi_by_date.get(entry.get(k, "")) for k in ("ref", "sec")]
        vals: list[float] = [v for v in found if v is not None]
        raw: float | None = max(vals) if vals else None
    else:
        raw = (entry.get("qa") or {}).get(metric.key)
    if not isinstance(raw, (int, float)) or not math.isfinite(raw):
        return None
    if metric.log:
        return math.log10(raw) if raw > 0 else None
    return float(raw)


def value_of(
    entry: dict, metric: str, granules: list[dict] | None = None
) -> float | None:
    """Return one pair's (or acquisition's) value of ``metric`` on its own scale.

    >>> value_of({"qa": {"cm": 0.42}}, "cm")
    0.42
    """
    m = METRICS[metric]
    rfi = rfi_by_date(granules or []) if metric == "rl" and "ref" in entry else None
    v = _value(entry, m, rfi)
    return None if v is None else _shown(v, m)


def rfi_by_date(granules: Iterable[dict]) -> dict[str, float]:
    """Return each acquisition date's worst GSLC RFI likelihood."""
    out: dict[str, float] = {}
    for g in granules:
        rl = (g.get("qa") or {}).get("rl")
        if isinstance(rl, (int, float)) and math.isfinite(rl) and g.get("date"):
            out[g["date"]] = max(out.get(g["date"], -math.inf), rl)
    return out


def find_drops(
    entries: list[dict],
    metric: str,
    *,
    product: Product = "gunw",
    threshold: float = 3.0,
    min_samples: int = 5,
    min_pairs: int = 2,
    min_share: float = 0.5,
    by_baseline: bool = True,
    granules: list[dict] | None = None,
) -> dict[str, Any]:
    """Flag the pairs (or acquisitions) of one stack whose ``metric`` drops.

    Parameters
    ----------
    entries : list of dict
        GUNW pairs (``ref``, ``sec``, ``dt``, ``gid``, ``qa``) or GSLC
        granules (``date``, ``gid``, ``qa``) of one frame.
    metric : str
        A key of :data:`METRICS`, e.g. ``"cm"``.
    product : {"gunw", "gslc"}
        Whether ``entries`` are GUNW pairs or GSLC granules.
    threshold : float
        Robust z-score on the bad side at which an entry is flagged.
    min_samples : int
        Fewest values a baseline (and a per-temporal-baseline group) needs.
    min_pairs, min_share : int, float
        A GUNW date is flagged when at least ``min_pairs`` of its pairs, and
        at least ``min_share`` of them, are flagged.
    by_baseline : bool
        Compare GUNW pairs with pairs of the same temporal baseline.
    granules : list of dict, optional
        The frame's GSLC granules, for a GUNW pair's RFI likelihood.

    Returns
    -------
    dict
        ``metric``, ``n`` values used, the stack ``median`` and ``sigma``,
        ``flagged`` entries (worst first, with ``value``, ``median``,
        ``score``, ``change_pct``) and flagged ``dates``. ``status`` is
        ``"ok"``, or says why nothing could be judged.

    Raises
    ------
    ValueError
        For an unknown metric, or one the product does not carry.

    """
    if metric not in METRICS:
        raise ValueError(f"unknown metric {metric!r}; known: {sorted(METRICS)}")
    m = METRICS[metric]
    if product not in m.products:
        raise ValueError(f"{metric!r} is not a {product.upper()} metric")
    rfi = (
        rfi_by_date(granules or []) if (product == "gunw" and metric == "rl") else None
    )

    values = []
    for e in entries:
        v = _value(e, m, rfi)
        if v is not None:
            values.append((e, v))
    report: dict[str, Any] = {
        "metric": metric,
        "label": m.label,
        "product": product,
        "n": len(values),
        "threshold": threshold,
        "flagged": [],
        "dates": [],
    }
    if len(values) < min_samples:
        report["status"] = f"too few values ({len(values)} < {min_samples})"
        return report
    med, sigma = robust_stats(v for _, v in values)
    report.update(median=_shown(med, m), sigma=round(sigma, 4))

    # Per-baseline medians where a baseline has enough pairs.
    groups: dict[Any, list[float]] = defaultdict(list)
    if product == "gunw" and by_baseline:
        for e, v in values:
            groups[e.get("dt")].append(v)
    baseline = {
        k: robust_stats(vs) for k, vs in groups.items() if len(vs) >= min_samples
    }

    for e, v in values:
        g_med, g_sigma = (
            baseline.get(e.get("dt"), (med, sigma))
            if product == "gunw"
            else (med, sigma)
        )
        # A flat stack (MAD 0) is judged against the metric's smallest
        # meaningful spread, not against zero.
        g_sigma = max(g_sigma, m.min_sigma, 1e-6)
        z = (v - g_med) / g_sigma
        score = -z if m.bad < 0 else z if m.bad > 0 else abs(z)
        if score < threshold:
            continue
        item = {
            k: e[k]
            for k in ("date", "ref", "sec", "dt", "mode", "pol", "gid")
            if k in e
        }
        item.update(
            value=_shown(v, m),
            median=_shown(g_med, m),
            score=round(score, 2),
            change_pct=(
                None
                if g_med == 0 or m.log
                else round(100 * (v - g_med) / abs(g_med), 1)
            ),
        )
        report["flagged"].append(item)
    report["flagged"].sort(key=lambda f: -f["score"])
    report["dates"] = _flag_dates(
        report["flagged"], [e for e, _ in values], product, min_pairs, min_share
    )
    report["status"] = "ok"
    return report


def _shown(v: float, m: Metric) -> float:
    """Return a value on the metric's own scale (log metrics are compared as log10)."""
    return round(10**v, 4) if m.log else round(v, 4)


def _pair_dates(entry: dict) -> set[str]:
    """Return a pair's reference and secondary dates."""
    return {str(entry[k]) for k in ("ref", "sec") if entry.get(k)}


def _flag_dates(
    flagged: list[dict],
    judged: list[dict],
    product: str,
    min_pairs: int,
    min_share: float,
) -> list[dict]:
    if product == "gslc":
        return [
            {"date": f["date"], "score": f["score"], "gids": [f["gid"]]}
            for f in flagged
            if "date" in f
        ]
    pairs_on: dict[str, int] = defaultdict(int)
    for e in judged:
        for d in _pair_dates(e):
            pairs_on[d] += 1
    hits: dict[str, list[dict]] = defaultdict(list)
    for f in flagged:
        for d in _pair_dates(f):
            hits[d].append(f)
    out = []
    for d, fs in hits.items():
        share = len(fs) / pairs_on[d]
        if len(fs) >= min_pairs and share >= min_share:
            out.append(
                {
                    "date": d,
                    "n_pairs": pairs_on[d],
                    "n_flagged": len(fs),
                    "share": round(share, 2),
                    "worst_score": max(f["score"] for f in fs),
                    "gids": [f["gid"] for f in fs if "gid" in f],
                }
            )
    return sorted(out, key=lambda r: (-r["share"], -r["worst_score"], r["date"]))
