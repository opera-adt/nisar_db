"""CMR (NASA Common Metadata Repository) search backends.

Two entry points:

- :func:`search_nisar_products` returns typed :class:`NISARProduct` objects from
  the UMM-G endpoint (used by the ``search`` CLI and downstream tooling).
- :func:`search_nisar_granules` returns a flat granule DataFrame from the
  echo10 ``granules.json`` endpoint (used by the catalog builders, which parse
  the granule *title* and CMR link ``rel`` values themselves).
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

    """
    # First determine how many pages we need to fetch
    params_copy = params.copy()
    params_copy["page_num"] = 1

    try:
        logger.debug("Fetching page 1 to determine total pages")
        response = requests.get(url, params=params_copy, timeout=30)
        response.raise_for_status()
        response_json = response.json()

        # Determine total hits and pages
        total_hits = 0
        page_size = params.get("page_size", 10)

        # Handle UMM JSON format
        if "items" in response_json:
            first_page_items = response_json["items"]
            # Check for CMR-Hits header which gives total results
            if "CMR-Hits" in response.headers:
                total_hits = int(response.headers["CMR-Hits"])
        # Handle atom format
        elif "feed" in response_json and "entry" in response_json["feed"]:
            first_page_items = response_json["feed"]["entry"]
            # Some feed formats include a total element
            if "hits" in response_json.get("feed", {}):
                total_hits = int(response_json["feed"]["hits"])
        else:
            first_page_items = []

        # If we couldn't determine total hits, make a conservative estimate
        if total_hits == 0 and first_page_items:
            # Just assume there might be more pages if we got a full page
            if len(first_page_items) >= page_size:
                total_hits = page_size * 5  # Conservative estimate
            else:
                total_hits = len(first_page_items)

        total_pages = (total_hits + page_size - 1) // page_size
        logger.debug(f"Estimated total pages: {total_pages} (from {total_hits} hits)")

        # No need for parallel fetching if only one page
        if total_pages <= 1:
            return first_page_items

        # Adjust max_workers to not exceed total pages and be reasonable
        max_workers = min(
            max_workers, total_pages - 1, 4
        )  # Never use more than 4 workers

        all_items = first_page_items.copy()  # Start with items from first page

        # For small numbers of pages, sequential is simpler and less risky
        if total_pages <= 3 or max_workers <= 1:
            # Sequential fetching for remaining pages
            for page_num in range(2, total_pages + 1):
                logger.debug(f"Fetching page {page_num}/{total_pages} sequentially")
                params_copy["page_num"] = page_num

                # Add rate limiting delay
                if rate_limit_delay > 0:
                    time.sleep(rate_limit_delay)

                response = requests.get(url, params=params_copy, timeout=30)
                response.raise_for_status()
                response_json = response.json()

                # Extract items based on format
                if "items" in response_json:
                    items = response_json["items"]
                elif "feed" in response_json and "entry" in response_json["feed"]:
                    items = response_json["feed"]["entry"]
                else:
                    items = []

                all_items.extend(items)

                # Check if we've reached the end
                if not items or len(items) < page_size:
                    break
        else:
            # Parallel fetching for remaining pages using ThreadPoolExecutor
            def fetch_page(page_num):
                """Fetch a single page of CMR results."""
                logger.debug(f"Fetching page {page_num}/{total_pages} in parallel")
                params_page = params.copy()
                params_page["page_num"] = page_num

                # Add rate limiting delay based on thread ID to spread out requests
                if rate_limit_delay > 0:
                    time.sleep(rate_limit_delay * ((page_num - 2) % max_workers))

                try:
                    response = requests.get(url, params=params_page, timeout=30)
                    response.raise_for_status()
                    response_json = response.json()

                    # Extract items based on format
                    if "items" in response_json:
                        return response_json["items"]
                    elif "feed" in response_json and "entry" in response_json["feed"]:
                        return response_json["feed"]["entry"]
                    else:
                        return []
                except Exception:
                    logger.exception(f"Error fetching page {page_num}")
                    return []

            # Fetch remaining pages in parallel
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                # Submit all page fetches
                future_to_page = {
                    executor.submit(fetch_page, page_num): page_num
                    for page_num in range(2, total_pages + 1)
                }

                # Collect results as they complete
                for future in as_completed(future_to_page):
                    page_items = future.result()
                    all_items.extend(page_items)

                    # Check if we got fewer items than expected
                    if not page_items or len(page_items) < page_size:
                        logger.debug("Received fewer items than page size")

        return all_items  # noqa: TRY300  # success path of a large try block

    except Exception:
        logger.exception("Error determining total pages")
        # Fall back to sequential fetching with unknown total
        logger.debug("Falling back to sequential fetching")

        all_items = []
        page_num = 1

        while True:
            try:
                params["page_num"] = page_num

                # Add rate limiting delay
                if page_num > 1 and rate_limit_delay > 0:
                    time.sleep(rate_limit_delay)

                response = requests.get(url, params=params, timeout=30)
                response.raise_for_status()
                response_json = response.json()

                # Handle UMM JSON format
                if "items" in response_json:
                    items = response_json["items"]
                # Handle atom format
                elif "feed" in response_json and "entry" in response_json["feed"]:
                    items = response_json["feed"]["entry"]
                else:
                    items = []

                all_items.extend(items)

                # Check if we've reached the end
                if not items or len(items) < params.get("page_size", 10):
                    break

                page_num += 1
            except Exception:
                logger.exception(f"Error fetching page {page_num}")
                break

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
    max_results: int = 10000,  # Increased default to 10000
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
    max_results : int
        Maximum number of results to return.
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
            if len(products) >= max_results:
                break
        except (ValueError, KeyError) as e:
            logger.debug(f"Skipping item due to parsing error: {e}")

    # Sort by track, frame, and date
    products.sort(
        key=lambda p: (p.track or 0, p.frame or 0, p.direction or "", p.start_datetime)
    )

    return products


def search_nisar_granules(
    short_name: Union[str, List[str]],
    provider: str = "ASF",
    max_results: int = 25000,
    max_workers: int = 4,
    output_format: str = "json",  # noqa: ARG001  # kept for backward compatibility
) -> pd.DataFrame:
    """Search CMR and return a flat granule DataFrame for the catalog builders.

    Unlike :func:`search_nisar_products` (which returns typed
    :class:`NISARProduct` objects), this returns one row per granule with the
    raw fields the catalog pipeline needs: ``granule_id``, ``title`` and the
    CMR ``links`` list (``{"rel": ..., "href": ...}``). The granule *title* is
    parsed by ``GSLCFilename`` / ``GUNWFilename`` and the links by
    ``nisar_db.catalog._common.extract_urls`` downstream.

    Parameters
    ----------
    short_name : str or list of str
        CMR collection short name(s) to search.
    provider : str
        Data provider (default: "ASF").
    max_results : int
        Cap on the number of returned rows.
    max_workers : int
        Concurrent CMR page workers.
    output_format : str
        Kept for backward compatibility; the echo10 ``granules.json`` endpoint
        is always used because its ``links`` carry the ``rel`` values the
        catalog extractor expects.

    Returns
    -------
    pd.DataFrame
        Columns ``granule_id``, ``title``, ``links``.

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
    for entry in entries[:max_results]:
        rows.append(
            {
                "granule_id": entry.get("id", ""),
                "title": entry.get("title", ""),
                "links": entry.get("links", []),
            }
        )

    df = pd.DataFrame(rows, columns=["granule_id", "title", "links"])
    logger.info(f"Found {len(df)} granules for {short_name}")
    return df
