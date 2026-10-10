"""Find duplicate granules in a frame: the same acquisition delivered twice.

GSLC granules are duplicates when they share a date, mode and coverage (the
key the catalog's ``n_unique`` counts, and the viewer's "Duplicate granules"
colouring uses). GUNW interferograms are duplicates when they share both
dates, mode and coverage. Each group says why it repeats, read from the
granule names:

* ``reprocessed`` -- different CRIDs: a newer processing version came out;
* ``split`` -- one CRID, different start times: one acquisition delivered in
  pieces;
* ``repeat`` -- same CRID and times: delivered again (different product
  counter).

``keep`` names the granule to use: the newest CRID, then the highest product
counter.

Examples
--------
>>> a = ("NISAR_L2_PR_GSLC_005_113_D_065_2005_QPDH_A_20251118T010538"
...      "_20251118T010600_P05012_N_P_J_001")
>>> b = a.replace("P05012", "P05023")
>>> grans = [{"gid": g, "date": "2025-11-18", "mode": "2005", "cov": "P"}
...          for g in (a, b)]
>>> [(d["reason"], parse_name(d["keep"], "gslc")["crid"])
...  for d in duplicate_groups(grans)]
[('reprocessed', 'P05023')]

"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Literal

Product = Literal["gslc", "gunw"]

#: Where the start time, CRID and product counter sit in a granule name.
_FIELDS = {
    "gslc": {"start": 11, "crid": 13, "counter": 17},
    "gunw": {"start": 11, "crid": 15, "counter": 19},
}


def parse_name(gid: str, product: Product) -> dict[str, str]:
    """Return a granule name's start time, CRID and product counter ("" if absent).

    >>> parse_name("NISAR_L2_PR_GSLC_005_113_D_065_2005_QPDH_A_20251118T010538"
    ...            "_20251118T010600_P05012_N_P_J_001", "gslc")
    {'start': '20251118T010538', 'crid': 'P05012', 'counter': '001'}
    """
    parts = str(gid).split("_")
    return {k: parts[i] if len(parts) > i else "" for k, i in _FIELDS[product].items()}


def _key(entry: dict, product: Product) -> tuple:
    if product == "gunw":
        return (entry.get("ref"), entry.get("sec"), entry.get("mode"), entry.get("cov"))
    return (entry.get("date"), entry.get("mode"), entry.get("cov"))


def duplicate_groups(
    entries: list[dict], product: Product = "gslc"
) -> list[dict[str, Any]]:
    """Group a frame's granules (or pairs) that repeat one acquisition.

    Parameters
    ----------
    entries : list of dict
        GSLC granules (``gid``, ``date``, ``mode``, ``cov``, ``pol``) or GUNW
        pairs (``gid``, ``ref``, ``sec``, ``mode``, ``cov``) of one frame.
    product : {"gslc", "gunw"}
        Which kind of entries these are.

    Returns
    -------
    list of dict
        One per repeated acquisition, oldest first: its key fields, ``n``
        granules, their ``gids``, ``crids`` and start times, the ``reason``
        and the granule to ``keep``.

    """
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for e in entries:
        groups[_key(e, product)].append(e)
    out = []
    for key, es in groups.items():
        if len(es) < 2:
            continue
        names = [(e, parse_name(e.get("gid", ""), product)) for e in es]
        crids = sorted({n["crid"] for _, n in names})
        starts = sorted({n["start"] for _, n in names})
        reason = (
            "reprocessed"
            if len(crids) > 1
            else "split" if len(starts) > 1 else "repeat"
        )
        keep = max(names, key=lambda en: (en[1]["crid"], en[1]["counter"]))[0].get(
            "gid"
        )
        fields = (
            ("ref", "sec", "mode", "cov")
            if product == "gunw"
            else ("date", "mode", "cov")
        )
        group = dict(zip(fields, key))
        group.update(
            n=len(es),
            reason=reason,
            crids=crids,
            starts=starts,
            pols=sorted({str(e.get("pol")) for e in es if e.get("pol")}),
            gids=[e.get("gid") for e in es],
            keep=keep,
        )
        out.append(group)
    first = "ref" if product == "gunw" else "date"
    return sorted(
        out, key=lambda g: (str(g[first]), str(g.get("sec", "")), str(g["mode"]))
    )


def summarize(groups: list[dict]) -> dict[str, Any]:
    """Return how many groups and extra granules there are, by reason.

    >>> summarize([{"n": 3, "reason": "split"}, {"n": 2, "reason": "reprocessed"}])
    {'groups': 2, 'extra_granules': 3, 'by_reason': {'reprocessed': 1, 'split': 1}}
    """
    by_reason: dict[str, int] = defaultdict(int)
    for g in groups:
        by_reason[g["reason"]] += 1
    return {
        "groups": len(groups),
        "extra_granules": sum(g["n"] - 1 for g in groups),
        "by_reason": dict(sorted(by_reason.items())),
    }
