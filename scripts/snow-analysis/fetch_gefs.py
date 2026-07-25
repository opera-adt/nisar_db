#!/usr/bin/env python
"""Subset the NOAA GEFS analysis archive to a local Zarr store.

Step 1 of the NISAR snow analysis. Pulls the two fields the blackout logic
needs -- ``categorical_snow_surface`` and ``temperature_2m`` -- over a bounding
box and time range, and rechunks them time-major so that the per-frame
timelines in ``snow_month_filter.py`` read a handful of chunks instead of the
whole cube.

The archive is the public Dynamical.org mirror of the NOAA GEFS analysis; it
asks for an email address in the query string as contact info.

Notes
-----
Snow fields are absent before 2020-01-01, so an earlier ``--start`` silently
yields all-NaN snow.

Examples
--------
North America (the OPERA NISAR footprint), 2020 onward::

    python fetch_gefs.py --email you@example.com --outfile noaa_gefs.zarr

Alaska only, since 2022::

    python fetch_gefs.py --email you@example.com \\
        --bbox -180 49 -120 76 --start 2022-01-01 --outfile gefs_alaska.zarr

"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import xarray as xr
import zarr
from zarr.codecs import BloscCodec

DEFAULT_URL = "https://data.dynamical.org/noaa/gefs/analysis/latest.zarr"
DATA_VARS = ["categorical_snow_surface", "temperature_2m"]
# North America, matching the OPERA NA polygon that nisar_db filters frames to.
DEFAULT_BBOX = (-180.0, 35.0, -50.0, 80.0)


class RetryingFsspecStore(zarr.storage.FsspecStore):
    """Zarr store that retries transient HTTP failures with backoff.

    Pulling several years of a remote store issues tens of thousands of range
    requests; without retries a single dropped connection kills the transfer.
    """

    max_attempts = 5
    backoff = 0.1

    async def get(
        self,
        key: str,
        prototype: zarr.core.buffer.core.BufferPrototype,
        byte_range: zarr.abc.store.ByteRequest | None = None,
    ) -> zarr.core.buffer.Buffer | None:
        """Fetch ``key``, retrying with exponential backoff."""
        for attempt in range(self.max_attempts):
            try:
                return await super().get(key, prototype, byte_range)
            except Exception:
                if attempt >= self.max_attempts - 1:
                    raise
                await asyncio.sleep(self.backoff * (2**attempt))

        raise AssertionError("Unreachable")


def open_gefs(url: str, email: str) -> xr.Dataset:
    """Open the remote GEFS analysis Zarr store."""
    store = RetryingFsspecStore.from_url(f"{url}?email={email}")
    return xr.open_zarr(store, decode_timedelta=True, chunks=None)


def subset(
    ds: xr.Dataset,
    bbox: tuple[float, float, float, float],
    start: str | None,
    end: str | None,
) -> xr.Dataset:
    """Select ``DATA_VARS`` over ``bbox`` and the requested time range."""
    minx, miny, maxx, maxy = bbox
    lat = ds["latitude"].values
    lat_slice = slice(maxy, miny) if lat[0] > lat[-1] else slice(miny, maxy)
    return ds[DATA_VARS].sel(
        time=slice(start, end),
        latitude=lat_slice,
        longitude=slice(minx, maxx),
    )


def write_zarr(ds_sub: xr.Dataset, outfile: Path, time_chunk: int = 480) -> Path:
    """Write ``ds_sub`` time-major, one spatial chunk per time block."""
    _, rows, cols = ds_sub[DATA_VARS[0]].shape
    encoding = dict.fromkeys(
        ds_sub.data_vars,
        {
            "chunks": (time_chunk, rows, cols),
            "compressors": [BloscCodec(cname="zstd", clevel=6)],
        },
    )
    ds_sub.chunk({"time": time_chunk, "latitude": -1, "longitude": -1}).to_zarr(
        outfile, encoding=encoding, consolidated=False
    )
    return outfile


def main() -> None:
    """Fetch and write the local GEFS subset."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--email", required=True, help="Contact address required by the archive."
    )
    parser.add_argument(
        "--outfile",
        type=Path,
        default=Path("noaa_gefs.zarr"),
        help="Output Zarr store.",
    )
    parser.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        metavar=("MINX", "MINY", "MAXX", "MAXY"),
        default=DEFAULT_BBOX,
        help="Longitude/latitude bounding box to keep.",
    )
    parser.add_argument("--start", default="2020-01-01", help="First date to keep.")
    parser.add_argument("--end", default=None, help="Last date to keep (default: all).")
    parser.add_argument("--url", default=DEFAULT_URL, help="Remote Zarr store URL.")
    parser.add_argument(
        "--time-chunk", type=int, default=480, help="Time steps per output chunk."
    )
    args = parser.parse_args()

    if args.outfile.exists():
        raise FileExistsError(f"{args.outfile} already exists; remove it or rename.")

    ds_sub = subset(
        open_gefs(args.url, args.email), tuple(args.bbox), args.start, args.end
    )
    nbytes_gb = sum(v.size * 4 for v in ds_sub.data_vars.values()) / 1e9
    print(f"Subset: {dict(ds_sub.sizes)}  (~{nbytes_gb:.1f} GB uncompressed)")

    write_zarr(ds_sub, args.outfile, time_chunk=args.time_chunk)
    print(f"Wrote {args.outfile}")


if __name__ == "__main__":
    main()
