"""CMR (NASA Common Metadata Repository) search backends.

Two entry points:

- :func:`search_nisar_products` returns typed :class:`NISARProduct` objects from
  the UMM-G endpoint (used by the ``search`` CLI and downstream tooling).
- :func:`search_nisar_granules` returns a flat granule DataFrame from the
  echo10 ``granules.json`` endpoint (used by the catalog builders, which parse
  the granule *name* and CMR link ``rel`` values themselves).
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Union

import pandas as pd
import requests

from ..filenames import NISARCollection
from .models import NISARProduct, ProductType, UrlType

__all__ = [
    "fetch_cmr_pages",
    "search_nisar_granules",
    "search_nisar_products",
]

logger = logging.getLogger(__name__)

_UMM_SEARCH_URL = "https://cmr.earthdata.nasa.gov/search/granules.umm_json"
_JSON_SEARCH_URL = "https://cmr.earthdata.nasa.gov/search/granules.json"


def _extract_items(response_json: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the granule list from a CMR response body.

    Handles both response shapes: UMM-JSON (``items``) and the echo10 atom-JSON
    used by ``granules.json`` (``feed.entry``).
    """
    if "items" in response_json:
        return response_json["items"]
    return response_json.get("feed", {}).get("entry", [])


def _extract_total_hits(response: requests.Response) -> int:
    """Return the total granule count for a CMR search, or 0 if not reported.

    ``CMR-Hits`` is set on every CMR search response and is the only place the
    total appears for the atom-JSON (``granules.json``) format, whose body
    carries no hit count -- reading the header rather than the body is what
    keeps a granule search from stopping short of the full archive.
    """
    return int(response.headers.get("CMR-Hits", 0))


def _fetch_page(
    url: str, params: Dict[str, Any], page_num: int
) -> List[Dict[str, Any]]:
    """Fetch a single page of CMR results."""
    response = requests.get(url, params={**params, "page_num": page_num}, timeout=30)
    response.raise_for_status()
    return _extract_items(response.json())


def fetch_cmr_pages(
    url: str,
    params: Dict[str, Any],
    max_workers: int = 1,
    rate_limit_delay: float = 0.5,
) -> List[Dict[str, Any]]:
    """Fetch all pages from CMR search API.

    Parameters
    ----------
    url : str
        CMR API URL.
    params : Dict[str, Any]
        Query parameters.
    max_workers : int
        Maximum number of concurrent workers for fetching pages.
        Default is 1 to avoid overwhelming the CMR API.
    rate_limit_delay : float
        Delay in seconds between page requests to respect rate limits.
        Default is 0.5 seconds.

    Returns
    -------
    List[Dict[str, Any]]
        List of items from all pages.

    Raises
    ------
    requests.HTTPError
        If any page request fails. A dropped page silently truncates the
        archive, so page errors are never swallowed.

    """
    page_size = params.get("page_size", 10)

    logger.debug("Fetching page 1 to determine total pages")
    response = requests.get(url, params={**params, "page_num": 1}, timeout=30)
    response.raise_for_status()

    first_page_items = _extract_items(response.json())
    total_hits = _extract_total_hits(response)
    if not total_hits:
        # No count reported: the first short page is the only end-of-results signal.
        total_hits = len(first_page_items)

    total_pages = (total_hits + page_size - 1) // page_size
    logger.debug(f"Total pages: {total_pages} (from {total_hits} hits)")

    if total_pages <= 1 or len(first_page_items) < page_size:
        return first_page_items

    all_items = first_page_items.copy()
    remaining = range(2, total_pages + 1)

    # Never use more than 4 workers, and no more than there are pages left.
    max_workers = min(max_workers, len(remaining), 4)

    # For small numbers of pages, sequential is simpler and less risky
    if total_pages <= 3 or max_workers <= 1:
        for page_num in remaining:
            logger.debug(f"Fetching page {page_num}/{total_pages} sequentially")
            if rate_limit_delay > 0:
                time.sleep(rate_limit_delay)

            items = _fetch_page(url, params, page_num)
            all_items.extend(items)

            if len(items) < page_size:
                break
        return all_items

    def fetch_page(page_num: int) -> List[Dict[str, Any]]:
        """Fetch one page, staggering the start to spread out requests."""
        logger.debug(f"Fetching page {page_num}/{total_pages} in parallel")
        if rate_limit_delay > 0:
            time.sleep(rate_limit_delay * ((page_num - 2) % max_workers))
        return _fetch_page(url, params, page_num)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(fetch_page, page_num) for page_num in remaining]
        for future in as_completed(futures):
            all_items.extend(future.result())

    # Over-fetching is normal -- ASF ingests granules while the pages are being
    # walked -- but coming up short means a page was dropped.
    if len(all_items) < total_hits:
        logger.warning(
            "Fetched only %d of the %d granules CMR reported.",
            len(all_items),
            total_hits,
        )

    return all_items


def _build_search_params(
    short_name: Union[str, List[str]],
    provider: str,
    *,
    bbox: Optional[Tuple[float, float, float, float]] = None,
    track: Optional[int] = None,
    frame: Optional[int] = None,
    direction: Optional[str] = None,
    cycle: Optional[int] = None,
    polarization: Optional[str] = None,
    start_datetime: Optional[datetime] = None,
    end_datetime: Optional[datetime] = None,
    page_size: int = 2000,
) -> Dict[str, Any]:
    """Assemble CMR query params shared by the product and granule searches."""
    params: Dict[str, Any] = {
        "short_name": short_name,  # str or list; a list is sent as repeated params (OR)
        "provider": provider,
        "page_size": page_size,
        # Paging is not a snapshot: without a stable sort, granules ingested
        # mid-query shift rows across page boundaries and get duplicated or
        # missed. Sorting by start_date appends new ingests past the last page.
        "sort_key": "start_date",
    }

    if bbox is not None:
        west, south, east, north = bbox
        params["bounding_box"] = f"{west},{south},{east},{north}"

    if start_datetime is not None or end_datetime is not None:
        start_str = start_datetime.isoformat() if start_datetime is not None else ""
        end_str = end_datetime.isoformat() if end_datetime is not None else ""
        params["temporal"] = f"{start_str},{end_str}"

    attribute_filters: List[str] = []
    if track is not None:
        attribute_filters.append(f"int,TRACK_NUMBER,{track}")
    if frame is not None:
        attribute_filters.append(f"int,FRAME_NUMBER,{frame}")
    if direction is not None:
        dir_str = "ASCENDING" if direction.upper() == "A" else "DESCENDING"
        attribute_filters.append(f"string,ASCENDING_DESCENDING,{dir_str}")
    if cycle is not None:
        attribute_filters.append(f"int,CYCLE_NUMBER,{cycle}")
    if polarization is not None:
        attribute_filters.append(f"string,POLARIZATION,{polarization.upper()}")

    if attribute_filters:
        params["attribute[]"] = attribute_filters

    return params


def _apply_max_results(items: List[Any], max_results: Optional[int]) -> List[Any]:
    """Truncate ``items`` to ``max_results``, warning when anything is dropped.

    ``None`` or a non-positive cap keeps everything. CMR is paged eagerly, so a
    cap never saves query time -- silently returning a partial archive would
    make a catalog look complete when it is not.
    """
    if max_results is None or max_results <= 0 or len(items) <= max_results:
        return items
    logger.warning(
        "Truncating %d CMR results to max_results=%d; %d granules dropped. "
        "Pass max_results=None (CLI: --max-results 0) for the whole archive.",
        len(items),
        max_results,
        len(items) - max_results,
    )
    return items[:max_results]


def search_nisar_products(
    bbox: Optional[Tuple[float, float, float, float]] = None,
    track: Optional[int] = None,
    frame: Optional[int] = None,
    direction: Optional[str] = None,
    cycle: Optional[int] = None,
    product_type: Union[str, ProductType] = ProductType.GSLC,
    polarization: Optional[str] = None,
    start_datetime: Optional[datetime] = None,
    end_datetime: Optional[datetime] = None,
    url_type: UrlType = UrlType.HTTPS,
    provider: str = "ASF",
    short_name: Optional[Union[str, List[str]]] = None,
    max_results: Optional[int] = None,
    max_workers: int = 4,  # Increased workers to 4 by default
) -> List[NISARProduct]:
    """Search for NISAR products in CMR.

    Parameters
    ----------
    bbox : Optional[Tuple[float, float, float, float]]
        Bounding box as (west, south, east, north) in degrees lon/lat.
    track : Optional[int]
        Track/relative orbit number.
    frame : Optional[int]
        Frame number.
    direction : Optional[str]
        Orbit direction: "A" for ascending, "D" for descending.
    cycle : Optional[int]
        Cycle number.
    product_type : Union[str, ProductType]
        Product type: "GSLC" or "GUNW".
    polarization : Optional[str]
        Polarization (e.g., "HH", "VV").
    start_datetime : Optional[datetime]
        Start datetime for temporal filtering.
    end_datetime : Optional[datetime]
        End datetime for temporal filtering.
    url_type : UrlType
        URL type: "https" or "s3".
    provider : str
        Data provider (default: "ASF").
    short_name : Optional[str]
        CMR short name. If None, determined from product_type.
    max_results : Optional[int]
        Cap on the number of returned products. ``None`` (the default) or a
        value <= 0 returns the whole matching archive. The cap is applied
        *after* every page is fetched, so it saves no query time -- it only
        drops granules, in arbitrary CMR order. Truncation is logged as a
        warning.
    max_workers : int
        Maximum number of concurrent workers for fetching pages.
        Default is 2 to avoid overwhelming the CMR API.

    Returns
    -------
    List[NISARProduct]
        List of NISAR products matching the search criteria.

    """
    # Convert product_type to enum if it's a string
    if isinstance(product_type, str):
        product_type = ProductType(product_type.upper())

    # Determine which collection(s) to search if not provided. Defaults span
    # both the BETA and PROVISIONAL collections for the product type; CMR ORs
    # multiple ``short_name`` values into a single search.
    if short_name is None:
        if product_type == ProductType.GSLC:
            short_name = list(NISARCollection.GSLC_SHORT_NAMES)
        else:
            short_name = list(NISARCollection.GUNW_SHORT_NAMES)

    params = _build_search_params(
        short_name,
        provider,
        bbox=bbox,
        track=track,
        frame=frame,
        direction=direction,
        cycle=cycle,
        polarization=polarization,
        start_datetime=start_datetime,
        end_datetime=end_datetime,
    )

    # Fetch all pages with optimized parameters for large result sets
    logger.info(f"Searching for NISAR products with {max_results=}, {max_workers=}")

    # Ensure we respect CMR rate limits while using parallel workers
    rate_limit_delay = 0.1 if max_workers > 2 else 0.0
    items = fetch_cmr_pages(
        _UMM_SEARCH_URL,
        params,
        max_workers=max_workers,
        rate_limit_delay=rate_limit_delay,
    )

    # Convert to NISARProduct objects
    products = []
    for item in items:
        try:
            product = NISARProduct.from_cmr_item(item, url_type=url_type)

            # Filter by product type if needed (server filtering may not work)
            if product.product_type != product_type:
                continue

            products.append(product)
        except (ValueError, KeyError) as e:
            logger.debug(f"Skipping item due to parsing error: {e}")

    # Sort by track, frame, and date
    products.sort(
        key=lambda p: (p.track or 0, p.frame or 0, p.direction or "", p.start_datetime)
    )

    return _apply_max_results(products, max_results)


def search_nisar_granules(
    short_name: Union[str, List[str]],
    provider: str = "ASF",
    max_results: Optional[int] = None,
    max_workers: int = 4,
    output_format: str = "json",  # noqa: ARG001  # kept for backward compatibility
) -> pd.DataFrame:
    """Search CMR and return a flat granule DataFrame for the catalog builders.

    Unlike :func:`search_nisar_products` (which returns typed
    :class:`NISARProduct` objects), this returns one row per granule with the
    raw fields the catalog pipeline needs: ``granule_id``, ``name`` and the
    CMR ``links`` list (``{"rel": ..., "href": ...}``). The granule *name* is
    parsed by ``GSLCFilename`` / ``GUNWFilename`` and the links by
    ``nisar_db.catalog._common.extract_urls`` downstream.

    Parameters
    ----------
    short_name : str or list of str
        CMR collection short name(s) to search.
    provider : str
        Data provider (default: "ASF").
    max_results : Optional[int]
        Cap on the number of returned rows; ``None`` (the default) or <= 0
        keeps the whole archive. See :func:`search_nisar_products` for why a
        cap costs the same query time as no cap.
    max_workers : int
        Concurrent CMR page workers.
    output_format : str
        Kept for backward compatibility; the echo10 ``granules.json`` endpoint
        is always used because its ``links`` carry the ``rel`` values the
        catalog extractor expects.

    Returns
    -------
    pd.DataFrame
        Columns ``granule_id``, ``name``, ``links``.

    """
    params = _build_search_params(short_name, provider)

    rate_limit_delay = 0.1 if max_workers > 2 else 0.0
    entries = fetch_cmr_pages(
        _JSON_SEARCH_URL,
        params,
        max_workers=max_workers,
        rate_limit_delay=rate_limit_delay,
    )

    rows = []
    for entry in _apply_max_results(entries, max_results):
        rows.append(
            {
                "granule_id": entry.get("id", ""),
                "name": entry.get("title", ""),  # atom "title" is the granule name
                "links": entry.get("links", []),
            }
        )

    df = pd.DataFrame(rows, columns=["granule_id", "name", "links"])
    logger.info(f"Found {len(df)} granules for {short_name}")
    return df
