"""Judge a frame's record: coverage, network, next passes, DISP readiness, events.

Covers coverage gaps, network health, next passes, DISP readiness, and QA
around an event.

All of it reads a frame's properties as the viewer and the API carry them:
``granules`` (GSLC acquisitions: ``date``, ``cycle``, ``mode``, ``cov``),
``gunw_ifgs`` (pairs: ``ref``, ``sec``, ``dt``, ``qa``), the consistent mode
(``cons_mode``, ``cons_cov``) and the blackout windows (``blackout_ranges``).
NISAR repeats every 12 days, so missed cycles and next passes follow from it;
predictions are from that repeat, not from the mission's acquisition plan.

Examples
--------
>>> seen = (("2026-06-02", 22), ("2026-06-14", 23), ("2026-07-08", 25))
>>> grans = [{"date": d, "cycle": c} for d, c in seen]
>>> coverage(grans, today="2026-07-20")["missed_cycles"]
[{'cycle': 24, 'expected_date': '2026-06-26'}]

"""

from __future__ import annotations

from datetime import date, timedelta
from statistics import median
from typing import Any, Iterable

REPEAT_DAYS = 12


def _day(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _today(today: Any) -> date:
    return _day(today) if today is not None else date.today()


# -- 1. coverage gaps --------------------------------------------------------------


def coverage(
    granules: Iterable[dict], *, today: Any = None, gap_days: int = 18
) -> dict[str, Any]:
    """Return a frame's acquisition record: missed cycles, long gaps, staleness.

    Parameters
    ----------
    granules : iterable of dict
        GSLC acquisitions with ``date`` and ``cycle``.
    today : date or str, optional
        Reference for ``days_since_last`` (default: today).
    gap_days : int
        A gap between consecutive acquisitions longer than this is listed
        (one 12-day repeat plus margin).

    Returns
    -------
    dict
        ``n_acquisitions``, ``first``, ``last``, ``days_since_last``,
        ``missed_cycles`` (cycle and the date it would have been acquired),
        ``gaps`` (from, to, days) and ``longest_gap_days``.

    """
    by_cycle: dict[int, date] = {}
    dates: set[date] = set()
    for g in granules:
        if not g.get("date"):
            continue
        d = _day(g["date"])
        dates.add(d)
        if str(g.get("cycle", "")).isdigit():
            c = int(g["cycle"])
            by_cycle[c] = min(by_cycle.get(c, d), d)
    out: dict[str, Any] = {
        "n_acquisitions": len(dates),
        "missed_cycles": [],
        "gaps": [],
    }
    if not dates:
        out.update(first=None, last=None, days_since_last=None, longest_gap_days=None)
        return out
    ordered = sorted(dates)
    out.update(
        first=ordered[0].isoformat(),
        last=ordered[-1].isoformat(),
        days_since_last=(_today(today) - ordered[-1]).days,
    )
    if by_cycle:
        known = sorted(by_cycle)
        for c in range(known[0], known[-1] + 1):
            if c not in by_cycle:
                ref = min(known, key=lambda k: abs(k - c))
                out["missed_cycles"].append(
                    {
                        "cycle": c,
                        "expected_date": (
                            (
                                by_cycle[ref] + timedelta(days=REPEAT_DAYS * (c - ref))
                            ).isoformat()
                        ),
                    }
                )
    longest = 0
    for a, b in zip(ordered, ordered[1:]):
        days = (b - a).days
        longest = max(longest, days)
        if days > gap_days:
            out["gaps"].append(
                {"from": a.isoformat(), "to": b.isoformat(), "days": days}
            )
    out["longest_gap_days"] = longest
    return out


# -- 2. network health ---------------------------------------------------------------


def network(
    pairs: Iterable[dict], granules: Iterable[dict] | None = None
) -> dict[str, Any]:
    """Return a frame's GUNW network: pieces, breaks, dates off the main piece.

    Acquisition dates are nodes and pairs edges, as in the viewer's GUNW plot.
    A *break* is a span between consecutive dates that no pair bridges, where
    a time series built from these interferograms comes apart. With the
    frame's GSLC granules, ``unpaired`` lists acquisitions in no pair at all.

    >>> p = [{"ref": "2026-01-01", "sec": "2026-01-13"},
    ...      {"ref": "2026-01-25", "sec": "2026-02-06"}]
    >>> r = network(p)
    >>> r["status"], r["components"], r["breaks"]
    ('disconnected', 2, [{'from': '2026-01-13', 'to': '2026-01-25'}])
    """
    edges = [
        (_day(p["ref"]), _day(p["sec"])) for p in pairs if p.get("ref") and p.get("sec")
    ]
    if not edges:
        return {
            "status": "no GUNW",
            "components": 0,
            "n_dates": 0,
            "breaks": [],
            "off_main": [],
            "unpaired": [],
        }
    parent: dict[date, date] = {}

    def find(x: date) -> date:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        parent.setdefault(a, a)
        parent.setdefault(b, b)
    for a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    nodes = sorted(parent)
    breaks = [
        {"from": lo.isoformat(), "to": hi.isoformat()}
        for lo, hi in zip(nodes, nodes[1:])
        if not any(a <= lo and b >= hi for a, b in edges)
    ]
    size: dict[date, int] = {}
    for d in nodes:
        size[find(d)] = size.get(find(d), 0) + 1
    main = max(size, key=lambda r: size[r])
    off_main = [d.isoformat() for d in nodes if find(d) != main]
    unpaired = sorted(
        {_day(g["date"]).isoformat() for g in granules or [] if g.get("date")}
        - {d.isoformat() for d in nodes}
    )
    return {
        "status": "disconnected" if len(size) > 1 else "connected",
        "components": len(size),
        "n_dates": len(nodes),
        "n_pairs": len(edges),
        "breaks": breaks,
        "off_main": off_main,
        "unpaired": unpaired,
    }


# -- 3. next passes ------------------------------------------------------------------


def next_passes(
    granules: Iterable[dict], *, n: int = 3, today: Any = None
) -> list[str]:
    """Return the next ``n`` expected acquisition dates on or after ``today``.

    From the frame's last acquisition stepped by the 12-day repeat.

    >>> next_passes([{"date": "2026-07-08"}], n=2, today="2026-07-20")
    ['2026-07-20', '2026-08-01']
    """
    dates = sorted({_day(g["date"]) for g in granules if g.get("date")})
    if not dates:
        return []
    t, d = _today(today), dates[-1]
    while d < t:
        d += timedelta(days=REPEAT_DAYS)
    return [(d + timedelta(days=REPEAT_DAYS * k)).isoformat() for k in range(max(0, n))]


# -- 7. DISP readiness -----------------------------------------------------------------


def _blackouts(ranges: Iterable[str]) -> list[tuple[date, date]]:
    out = []
    for r in ranges or []:
        a, b = (x.strip() for x in str(r).split("->"))
        out.append((_day(a), _day(b)))
    return out


def disp_readiness(
    props: dict, *, batch_size: int = 15, today: Any = None
) -> dict[str, Any]:
    """Return how far a frame is towards its DISP-NISAR batches.

    A DISP time series stacks the frame's consistent-mode acquisitions
    (``cons_mode`` and ``cons_cov``) outside its blackout windows, in batches
    of ``batch_size`` (the processing-mode default). The next batch's date is
    projected by the 12-day repeat, skipping blackout windows.

    Returns
    -------
    dict
        ``status`` (``"no consistent mode"``, ``"accumulating"`` until the
        first batch is full, then ``"ready"``), ``n_usable``, ``batches``,
        ``to_next_batch`` and ``next_batch_date``, plus how many acquisitions
        the blackouts or other modes left out.

    """
    mode, cov = props.get("cons_mode"), props.get("cons_cov")
    granules = props.get("granules") or []
    blackouts = _blackouts(props.get("blackout_ranges") or [])
    out: dict[str, Any] = {
        "consistent_mode": mode,
        "consistent_coverage": cov,
        "batch_size": batch_size,
    }
    if not mode or mode == "none":
        out.update(
            status="no consistent mode",
            n_usable=0,
            batches=0,
            to_next_batch=None,
            next_batch_date=None,
        )
        return out
    all_dates = {_day(g["date"]) for g in granules if g.get("date")}
    same = {
        _day(g["date"])
        for g in granules
        if g.get("date")
        and str(g.get("mode")) == str(mode)
        and (cov in (None, "none") or g.get("cov") == cov)
    }
    blacked = {d for d in same if any(a <= d <= b for a, b in blackouts)}
    usable = sorted(same - blacked)
    n = len(usable)
    remainder = n % batch_size
    to_next = batch_size - remainder
    nxt = None
    if usable:
        d, left, t = max(usable), to_next, _today(today)
        # Only future passes outside a blackout window count; passes already
        # due but missing from the catalog do not. Capped at ~13 years.
        for _ in range(400):
            d += timedelta(days=REPEAT_DAYS)
            if d >= t and not any(a <= d <= b for a, b in blackouts):
                left -= 1
                if not left:
                    nxt = d.isoformat()
                    break
    out.update(
        status="ready" if n >= batch_size else "accumulating",
        n_usable=n,
        batches=n // batch_size,
        to_next_batch=to_next,
        next_batch_date=nxt,
        n_blacked_out=len(blacked),
        n_other_modes=len(all_dates - same),
        last_usable=usable[-1].isoformat() if usable else None,
    )
    return out


# -- 5. QA around an event -------------------------------------------------------------


def event_qa_history(
    pairs: Iterable[dict],
    when: str,
    metric: str,
    *,
    window_days: int = 120,
    granules: list[dict] | None = None,
) -> dict[str, Any]:
    """Compare a QA metric before, across and after an event.

    Pairs within ``window_days`` of the event are split into ``before`` (the
    secondary is before it), ``spanning`` (reference before, secondary after:
    coseismic for an earthquake) and ``after``. Each group's median and its
    change from the ``before`` median show what the event did to, say,
    coherence; ``n`` counts the pairs on each side (``n_with_value`` those
    carrying the metric); ``pairs`` lists them in time order with their values.
    """
    from nisar_db.qa_drops import METRICS, value_of

    if metric not in METRICS:
        raise ValueError(f"unknown metric {metric!r}; known: {sorted(METRICS)}")
    t = _day(when)
    lo, hi = t - timedelta(days=window_days), t + timedelta(days=window_days)
    groups: dict[str, list[float]] = {"before": [], "spanning": [], "after": []}
    counts = {"before": 0, "spanning": 0, "after": 0}
    rows = []
    for p in pairs:
        if not (p.get("ref") and p.get("sec")):
            continue
        ref, sec = _day(p["ref"]), _day(p["sec"])
        if sec < lo or ref > hi:
            continue
        side = "before" if sec < t else "spanning" if ref < t else "after"
        v = value_of(p, metric, granules)
        rows.append(
            {
                "ref": p["ref"],
                "sec": p["sec"],
                "dt": p.get("dt"),
                "gid": p.get("gid"),
                "side": side,
                "value": v,
            }
        )
        counts[side] += 1
        if v is not None:
            groups[side].append(v)
    med = {k: round(median(v), 4) if v else None for k, v in groups.items()}
    base = med["before"]

    def change(x: float | None) -> float | None:
        return None if x is None or not base else round(100 * (x - base) / abs(base), 1)

    return {
        "metric": metric,
        "label": METRICS[metric].label,
        "event_date": t.isoformat(),
        "window_days": window_days,
        "median": med,
        "change_pct": {
            "spanning": change(med["spanning"]),
            "after": change(med["after"]),
        },
        "n": counts,
        "n_with_value": {k: len(v) for k, v in groups.items()},
        "pairs": sorted(rows, key=lambda r: (r["sec"], r["ref"])),
    }
