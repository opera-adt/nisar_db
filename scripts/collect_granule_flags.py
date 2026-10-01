#!/usr/bin/env python
"""Collect per-granule quality flags from NISAR GSLC / GUNW HDF5 products.

The flags live in each product's ``identification`` and ``metadata`` groups, and
three of them (mixed mode, dithering, RFI mitigation) are not exposed in CMR. They
are read remotely with HTTP byte-range requests, so only a few hundred kilobytes
of each multi-gigabyte product are fetched. Earthdata credentials are read from
``~/.netrc`` (machine ``urs.earthdata.nasa.gov``).

Results go to a gzipped JSON cache keyed by granule id. Granules already in the
cache are skipped, so a rerun only reads new products; failed reads are retried.

Each granule maps to

* ``j`` -- joint observation (GUNW: reference or secondary),
* ``f`` -- full frame,
* ``o`` -- orbit type, e.g. ``MOE`` (GUNW: ``REF/SEC`` when they differ),
* ``r`` -- RFI mitigation applied (GUNW: to both reference and secondary),
* ``m`` -- mixed mode,
* ``d`` -- dithered,

with booleans as 0 / 1.

Examples
--------
Collect the flags of every granule in a built viewer::

    python scripts/collect_granule_flags.py \\
        --viewer-html docs/assets/opera_nisar_db_viewer.html \\
        --output catalog/granule_flags.json.gz

"""

from __future__ import annotations

import argparse
import gzip
import json
import netrc
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING

import requests

if TYPE_CHECKING:
    import h5py

DATA_URL = "https://nisar.asf.earthdatacloud.nasa.gov/NISAR/{collection}/{gid}/{gid}.h5"
COLLECTIONS = {
    "GSLC": "NISAR_L2_GSLC_PROVISIONAL_V1",
    "GUNW": "NISAR_L2_GUNW_PROVISIONAL_V1",
}

# HDF5 metadata is scattered through the file; small blocks keep each lookup cheap.
BLOCK_SIZE = 2**18

_SESSION: requests.Session | None = None


def _session() -> requests.Session:
    # One per worker process; the Earthdata login cookies it keeps make later
    # redirects cheaper.
    global _SESSION
    if _SESSION is None:
        auth = netrc.netrc().authenticators("urs.earthdata.nasa.gov")
        if auth is None:
            raise RuntimeError("no urs.earthdata.nasa.gov entry in ~/.netrc")
        _SESSION = requests.Session()
        _SESSION.auth = (auth[0], auth[2] or "")
    return _SESSION


def _text(ds: h5py.Dataset) -> str:
    value = ds[()]
    return value.decode() if isinstance(value, bytes) else str(value)


def _flag(ds: h5py.Dataset) -> int:
    return int(_text(ds).strip().lower() == "true")


def product_url(gid: str) -> str:
    """Return the HTTPS download URL of a GSLC or GUNW granule."""
    kind = gid.split("_")[3]
    return DATA_URL.format(collection=COLLECTIONS[kind], gid=gid)


def read_flags(gid: str) -> dict:
    """Read one granule's flags from its HDF5 product.

    Parameters
    ----------
    gid : str
        GSLC or GUNW granule id.

    Returns
    -------
    dict
        Flags keyed ``j f o r m d`` as described in the module docstring.

    """
    # Only this function needs the HDF5 / HTTP stack (the ``flags`` extra), so the
    # cache and listing helpers stay importable without it.
    import fsspec
    import h5py

    # The data URL redirects to a signed CloudFront URL, which serves byte ranges.
    signed = (
        _session()
        .get(product_url(gid), headers={"Range": "bytes=0-0"}, timeout=120)
        .url
    )
    fs = fsspec.filesystem("http")
    with fs.open(signed, block_size=BLOCK_SIZE, cache_type="blockcache") as fo:
        with h5py.File(fo) as h5:
            root = h5["science/LSAR"]
            ident = root["identification"]
            if "GUNW" in root:
                md = root["GUNW/metadata"]
                params = md["processingInformation/parameters"]
                orbits = {
                    _text(md[f"orbit/{s}/orbitType"])
                    for s in ("reference", "secondary")
                }
                return {
                    "j": (
                        _flag(ident["referenceIsJointObservation"])
                        | _flag(ident["secondaryIsJointObservation"])
                    ),
                    "f": _flag(ident["isFullFrame"]),
                    "o": (
                        "/".join(
                            _text(md[f"orbit/{s}/orbitType"])
                            for s in ("reference", "secondary")
                        )
                        if len(orbits) > 1
                        else orbits.pop()
                    ),
                    "r": (
                        _flag(params["reference/rfiMitigationApplied"])
                        & _flag(params["secondary/rfiMitigationApplied"])
                    ),
                    "m": _flag(ident["isMixedMode"]),
                    "d": _flag(ident["isDithered"]),
                }
            md = root["GSLC/metadata"]
            return {
                "j": _flag(ident["isJointObservation"]),
                "f": _flag(ident["isFullFrame"]),
                "o": _text(md["orbit/orbitType"]),
                "r": _flag(md["processingInformation/parameters/rfiMitigationApplied"]),
                "m": _flag(ident["isMixedMode"]),
                "d": _flag(ident["isDithered"]),
            }


def _read_or_error(gid: str) -> dict | str:
    # Runs in a worker process: an HTTP error can carry unpicklable response
    # objects, so failures come back as text rather than as the exception.
    try:
        return read_flags(gid)
    except Exception as exc:  # noqa: BLE001 - one bad granule must not stop the run
        return f"{type(exc).__name__}: {str(exc).split('?', 1)[0]}"


def viewer_granule_ids(html_path: Path) -> list[str]:
    """Return every GSLC and GUNW granule id embedded in a built viewer."""
    html = html_path.read_text()
    opener = "const FRAME_DATA = "
    start = html.index(opener) + len(opener)
    end = html.index(";\nconst META", start)
    ids: list[str] = []
    for feature in json.loads(html[start:end])["features"]:
        props = feature["properties"]
        ifgs = props.get("gunw_ifgs") or []
        if isinstance(ifgs, str):
            ifgs = json.loads(ifgs)
        ids += [g["gid"] for g in props.get("granules") or []]
        ids += [g["gid"] for g in ifgs]
    return ids


def load_cache(path: Path) -> dict[str, dict]:
    """Read a flag cache, or an empty one when ``path`` does not exist."""
    if not path.exists():
        return {}
    with gzip.open(path, "rt") as fh:
        return json.load(fh)


def save_cache(path: Path, flags: dict[str, dict]) -> None:
    """Write the flag cache atomically, sorted for stable diffs."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with gzip.open(tmp, "wt") as fh:
        json.dump(dict(sorted(flags.items())), fh, separators=(",", ":"))
    tmp.replace(path)


def collect(ids: list[str], output: Path, workers: int, save_every: int = 200) -> None:
    """Read the flags of every granule in ``ids`` not already in ``output``."""
    flags = load_cache(output)
    todo = [g for g in dict.fromkeys(ids) if g not in flags]
    print(f"{len(flags)} cached, {len(todo)} to read with {workers} workers")
    failed: dict[str, str] = {}
    t0 = time.time()
    # h5py serialises every HDF5 call behind one lock, so remote reads only run
    # in parallel across processes, not threads.
    with ProcessPoolExecutor(workers) as pool:
        futures = {pool.submit(_read_or_error, gid): gid for gid in todo}
        for n, fut in enumerate(as_completed(futures), 1):
            gid = futures[fut]
            result = fut.result()
            if isinstance(result, str):
                failed[gid] = result
            else:
                flags[gid] = result
            if n % save_every == 0 or n == len(todo):
                save_cache(output, flags)
                rate = n / (time.time() - t0)
                print(
                    f"  {n}/{len(todo)} read, {len(failed)} failed, "
                    f"{rate:.1f}/s, ~{(len(todo) - n) / rate / 60:.0f} min left",
                    flush=True,
                )
    for gid, err in list(failed.items())[:20]:
        print(f"  failed {gid}: {err}")
    print(f"{len(flags)} granules in {output}; {len(failed)} failed (rerun to retry)")


def main(argv: list[str] | None = None) -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--viewer-html",
        type=Path,
        required=True,
        help="Built viewer whose GSLC and GUNW granules to read.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("catalog/granule_flags.json.gz"),
        help="Flag cache to create or extend.",
    )
    parser.add_argument(
        "--workers", type=int, default=32, help="Worker processes reading at once."
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Read at most this many new granules."
    )
    args = parser.parse_args(argv)

    ids = viewer_granule_ids(args.viewer_html)
    if args.limit is not None:
        cached = load_cache(args.output)
        ids = [g for g in ids if g not in cached][: args.limit]
    collect(ids, args.output, args.workers)


if __name__ == "__main__":
    main()
