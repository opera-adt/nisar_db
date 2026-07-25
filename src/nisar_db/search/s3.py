"""S3-bucket search backend for NISAR products.

The S3 counterpart to :func:`nisar_db.search.cmr.search_nisar_products`: lists
an ``s3://bucket/prefix`` with boto3, parses each ``.h5`` object key into a
:class:`NISARProduct`, and applies the same track/frame/direction/cycle/
polarization/date filters client-side.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union

from .models import NISARProduct, ProductType

__all__ = ["parse_s3_uri", "search_s3_products"]

logger = logging.getLogger(__name__)


def parse_s3_uri(uri: str) -> Tuple[str, str]:
    """Split an ``s3://bucket/prefix`` URI (or ``bucket/prefix``) into (bucket, prefix).

    Tolerates a leading ``s3://`` and redundant slashes, e.g.
    ``"s3://bkt//products/L2_L_GSLC/"`` -> ``("bkt", "products/L2_L_GSLC/")``.
    """
    body = uri[len("s3://") :] if uri.startswith("s3://") else uri
    bucket, _, prefix = body.partition("/")
    return bucket, prefix.lstrip("/")


def _product_sort_key(p: "NISARProduct") -> tuple:
    """Sort products by (track, frame, direction, sensing start)."""
    return (p.track or 0, p.frame or 0, p.direction or "", p.start_datetime)


def _s3_common_prefixes(
    client, bucket: str, prefix: str, delimiter: str = "/"
) -> List[str]:
    """Return the immediate 'sub-folder' common prefixes under ``prefix``."""
    paginator = client.get_paginator("list_objects_v2")
    out: List[str] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter=delimiter):
        out.extend(cp["Prefix"] for cp in page.get("CommonPrefixes", []))
    return out


def _s3_leaf_prefixes(
    client, bucket: str, prefix: str, target: int, max_depth: int = 4
) -> List[str]:
    """Descend common prefixes (e.g. YYYY/MM/DD) to fan a prefix out for listing.

    Stops when there are at least ``target`` sub-prefixes, after ``max_depth``
    levels, or — crucially — when the next level would *explode*: the jump from
    day folders to the thousands of per-granule folders blows past ``cap``, at
    which point we keep the current (coarser) level. Without that guard the
    descent produces one tiny listing per granule, which is slower than a single
    flat scan. Childless branches are kept as leaves so nothing is dropped.
    """
    cap = max(target * 8, 64)
    level = [prefix]
    for _ in range(max_depth):
        if len(level) >= target:
            break
        expanded = False
        nxt: List[str] = []
        for p in level:
            children = _s3_common_prefixes(client, bucket, p)
            if children:
                nxt.extend(children)
                expanded = True
            else:
                nxt.append(p)
        if not expanded:  # every branch is a leaf; can't fan out further
            break
        if len(nxt) > cap:  # next level explodes (granule dirs) — keep current
            break
        level = nxt
    return level


def search_s3_products(
    bucket: str,
    prefix: str = "",
    *,
    profile: Optional[str] = None,
    region: str = "us-west-2",
    product_type: Union[str, ProductType] = ProductType.GSLC,
    track: Optional[int] = None,
    frame: Optional[int] = None,
    direction: Optional[str] = None,
    cycle: Optional[int] = None,
    polarization: Optional[str] = None,
    start_datetime: Optional[datetime] = None,
    end_datetime: Optional[datetime] = None,
    max_results: int = 100000,
    dedupe: bool = True,
    max_workers: int = 8,
) -> List[NISARProduct]:
    """Search a user-specified S3 bucket for NISAR products by listing keys.

    Lists ``s3://bucket/prefix`` with boto3 (using the named AWS ``profile``),
    parses every ``.h5`` object key into a :class:`NISARProduct`, and applies
    the same track/frame/direction/cycle/polarization/date filters as
    :func:`nisar_db.search.cmr.search_nisar_products`.

    .. note::
       S3 can only filter by key *prefix*, but track/frame/pol live in the
       filename, so this must enumerate the whole prefix and filter client-side.
       For repeated track/frame lookups prefer CMR
       (:func:`nisar_db.search.cmr.search_nisar_products`, server-side indexed)
       or a one-time-built catalog.

    Parameters
    ----------
    bucket : str
        Bucket name, or a full ``s3://bucket/prefix`` URI (prefix then optional).
    prefix : str
        Key prefix to scan, e.g. ``"products/L2_L_GSLC/"``. The more specific,
        the less to enumerate (e.g. add ``.../2026/03/`` to restrict by date).
    profile : Optional[str]
        AWS named profile (``~/.aws/credentials``). None uses the default chain.
    region : str
        AWS region for the S3 client.
    product_type : Union[str, ProductType]
        Keep only products of this type ("GSLC" or "GUNW").
    track, frame, direction, cycle, polarization :
        Optional exact-match filters (direction "A"/"D"; naive filename times).
    start_datetime, end_datetime : Optional[datetime]
        Keep products whose sensing start falls within this (inclusive) range.
        Compared against the naive datetimes parsed from the filename.
    max_results : int
        Cap the number of returned products (applied after listing).
    dedupe : bool
        Drop duplicate granule names (OPS buckets list each granule under
        multiple keys).
    max_workers : int
        Parallel listing workers. The prefix is fanned out into sub-prefixes
        (e.g. per day) that are listed concurrently. 1 = sequential.

    Returns
    -------
    List[NISARProduct]
        Matching products, sorted by (track, frame, direction, start time).

    """
    import boto3

    if isinstance(product_type, str):
        product_type = ProductType(product_type.upper())

    if bucket.startswith("s3://"):
        parsed_bucket, parsed_prefix = parse_s3_uri(bucket)
        bucket = parsed_bucket
        prefix = prefix or parsed_prefix

    dir_norm = direction.upper()[0] if direction else None
    pol_norm = polarization.upper() if polarization else None

    # botocore low-level clients are thread-safe, so one client is shared by all
    # listing workers.
    s3 = boto3.Session(profile_name=profile, region_name=region).client("s3")

    def _match(obj: Dict[str, Any]) -> Optional[NISARProduct]:
        """Parse an object into a NISARProduct if it passes all filters."""
        key = obj["Key"]
        if not key.endswith(".h5"):
            return None
        try:
            product = NISARProduct.from_s3_key(
                bucket,
                key,
                size=obj.get("Size"),
                last_modified=obj.get("LastModified"),
            )
        except Exception as e:  # skip keys that don't parse as NISAR granules
            logger.debug(f"Skipping unparseable key {key}: {e}")
            return None
        if product.product_type != product_type:
            return None
        if track is not None and product.track != track:
            return None
        if frame is not None and product.frame != frame:
            return None
        if dir_norm is not None and (product.direction or "").upper() != dir_norm:
            return None
        if cycle is not None and product.cycle != cycle:
            return None
        if pol_norm is not None and (product.polarization or "").upper() != pol_norm:
            return None
        if start_datetime is not None and product.start_datetime < start_datetime:
            return None
        if end_datetime is not None and product.start_datetime > end_datetime:
            return None
        return product

    def _scan(pfx: str) -> Tuple[List[NISARProduct], int, int]:
        """List one prefix fully; return (matched, n_scanned, n_h5)."""
        paginator = s3.get_paginator("list_objects_v2")
        found: List[NISARProduct] = []
        n = nh5 = 0
        for page in paginator.paginate(Bucket=bucket, Prefix=pfx):
            for obj in page.get("Contents", []):
                n += 1
                if obj["Key"].endswith(".h5"):
                    nh5 += 1
                product = _match(obj)
                if product is not None:
                    found.append(product)
        return found, n, nh5

    products: List[NISARProduct] = []
    scanned = n_h5 = 0

    if max_workers and max_workers > 1:
        # Fan the prefix out into enough sub-prefixes to keep workers busy,
        # capped at day-level so we don't explode into per-granule folders.
        leaves = _s3_leaf_prefixes(s3, bucket, prefix, target=max_workers)
        logger.info(
            f"Scanning s3://{bucket}/{prefix}: {len(leaves)} sub-prefixes "
            f"x {max_workers} workers (profile={profile})"
        )
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_scan, lp): lp for lp in leaves}
            for future in as_completed(futures):
                found, n, nh5 = future.result()
                products.extend(found)
                scanned += n
                n_h5 += nh5
    else:
        logger.info(f"Scanning s3://{bucket}/{prefix} (profile={profile})")
        products, scanned, n_h5 = _scan(prefix)

    if dedupe:
        seen: set = set()
        deduped: List[NISARProduct] = []
        for product in products:
            if product.granule_id in seen:
                continue
            seen.add(product.granule_id)
            deduped.append(product)
        products = deduped

    products.sort(key=_product_sort_key)
    if len(products) > max_results:
        products = products[:max_results]

    logger.info(
        f"Scanned {scanned:,} objects ({n_h5:,} .h5); "
        f"matched {len(products):,} products"
    )
    return products
