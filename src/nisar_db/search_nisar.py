"""Search for NISAR GSLC and GUNW products from CMR.

This module provides functions to search for NISAR products using the NASA CMR API.
It supports both GSLC (Ground Slant-Looking Complex) and GUNW (Geocoded Unwrapped
Interferogram) products.

Examples
--------
Command line:
$ python -m nisar_db.search_nisar --track 76 --frame 22 --direction A --product-type GSLC

Python:
>>> from nisar_db.search_nisar import search_nisar_products
>>> products = search_nisar_products(
...     bbox=(40.62, 13.56, 40.72, 13.64),
...     product_type="GSLC"
... )
"""

from __future__ import annotations

import argparse
# import json  # Unused import
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import pandas as pd
import requests
from tqdm.auto import tqdm

from .download import download_earthdata_granule
from .filenames import NISARCollection

__all__ = ["search_nisar_products", "download_products", "NISARProduct", "ProductType"]

logger = logging.getLogger(__name__)


class ProductType(str, Enum):
    """NISAR product types."""

    GSLC = "GSLC"
    GUNW = "GUNW"


class UrlType(str, Enum):
    """URL types for NISAR products."""

    HTTPS = "https"
    S3 = "s3"


@dataclass
class NISARProduct:
    """NISAR product metadata from CMR search results."""

    granule_id: str
    title: str
    product_type: ProductType
    filename: str
    url: str
    url_type: UrlType = UrlType.HTTPS
    start_datetime: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    end_datetime: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    bbox: Optional[Tuple[float, float, float, float]] = None
    track: Optional[int] = None
    frame: Optional[int] = None
    direction: Optional[str] = None
    cycle: Optional[int] = None
    polarization: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_cmr_item(cls, item: Dict[str, Any], url_type: UrlType = UrlType.HTTPS) -> "NISARProduct":
        """Create a NISARProduct from a CMR item."""
        umm = item.get("umm", {})
        granule_id = item.get("meta", {}).get("concept-id") or item.get("id", "")
        title = umm.get("GranuleUR", "")

        # Determine product type from title
        product_type = ProductType.GSLC if "GSLC" in title else ProductType.GUNW

        # Extract spatial information
        bbox = None
        if "spatial" in umm and "horizontalSpatialDomain" in umm["spatial"]:
            geom = umm["spatial"]["horizontalSpatialDomain"].get("geometry", {})
            if "boundingRectangles" in geom and geom["boundingRectangles"]:
                rect = geom["boundingRectangles"][0]
                bbox = (
                    rect["westBoundingCoordinate"],
                    rect["southBoundingCoordinate"],
                    rect["eastBoundingCoordinate"],
                    rect["northBoundingCoordinate"],
                )

        # Extract temporal information
        start_dt = datetime.now(timezone.utc)
        end_dt = datetime.now(timezone.utc)
        if "temporalExtent" in umm and "rangeDateTime" in umm["temporalExtent"]:
            range_dt = umm["temporalExtent"]["rangeDateTime"]
            if "beginningDateTime" in range_dt:
                start_dt = datetime.fromisoformat(range_dt["beginningDateTime"].replace("Z", "+00:00"))
            if "endingDateTime" in range_dt:
                end_dt = datetime.fromisoformat(range_dt["endingDateTime"].replace("Z", "+00:00"))

        # Extract track, frame, direction information from additional attributes
        track = None
        frame = None
        direction = None
        cycle = None
        polarization = None

        if "AdditionalAttributes" in umm:
            for attr in umm["AdditionalAttributes"]:
                if attr["Name"] == "TRACK_NUMBER":
                    track = int(attr["Values"][0])
                elif attr["Name"] == "FRAME_NUMBER":
                    frame = int(attr["Values"][0])
                elif attr["Name"] == "ASCENDING_DESCENDING":
                    direction = "A" if attr["Values"][0] == "ASCENDING" else "D"
                elif attr["Name"] == "CYCLE_NUMBER":
                    cycle = int(attr["Values"][0])
                elif attr["Name"] == "POLARIZATION":
                    polarization = attr["Values"][0]

        # Extract URLs
        url = ""
        for link in umm.get("relatedUrls", []):
            if link.get("type") == "GET DATA" and link.get("subtype") in ["AMAZON S3", "HTTPS"]:
                url_subtype = link.get("subtype", "")
                if (url_type == UrlType.HTTPS and url_subtype == "HTTPS") or (
                    url_type == UrlType.S3 and url_subtype == "AMAZON S3"
                ):
                    url = link.get("url", "")
                    break

        # Fallback to any URL if the preferred type is not found
        if not url and umm.get("relatedUrls"):
            for link in umm.get("relatedUrls", []):
                if link.get("type") == "GET DATA":
                    url = link.get("url", "")
                    break

        # Extract filename from URL
        filename = url.split("/")[-1] if url else title

        return cls(
            granule_id=granule_id,
            title=title,
            product_type=product_type,
            filename=filename,
            url=url,
            url_type=url_type,
            start_datetime=start_dt,
            end_datetime=end_dt,
            bbox=bbox,
            track=track,
            frame=frame,
            direction=direction,
            cycle=cycle,
            polarization=polarization,
            metadata=umm,
        )

    @property
    def track_frame_id(self) -> str:
        """Get track frame ID in format 'XXX_D_YYY'."""
        if self.track is None or self.frame is None or self.direction is None:
            return ""
        return f"{self.track:03d}_{self.direction}_{self.frame:03d}"

    @property
    def date(self) -> str:
        """Get date in YYYY-MM-DD format."""
        return self.start_datetime.strftime("%Y-%m-%d")


def fetch_cmr_pages(url: str, params: Dict[str, Any], max_workers: int = 1, rate_limit_delay: float = 0.5) -> List[Dict[str, Any]]:
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
        logger.debug(f"Fetching page 1 to determine total pages")
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
        max_workers = min(max_workers, total_pages - 1, 4)  # Never use more than 4 workers

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
            from concurrent.futures import ThreadPoolExecutor

            def fetch_page(page_num):
                """Helper function to fetch a single page."""
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
                except Exception as e:
                    logger.error(f"Error fetching page {page_num}: {e}")
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
                        logger.debug("Received fewer items than page size, may have reached the end")

        return all_items

    except Exception as e:
        logger.error(f"Error determining total pages: {e}")
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
            except Exception as page_error:
                logger.error(f"Error fetching page {page_num}: {page_error}")
                break

        return all_items


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
    short_name: Optional[str] = None,
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

    # Determine short_name based on product_type if not provided
    if short_name is None:
        if product_type == ProductType.GSLC:
            short_name = NISARCollection.GSLC_BETA_V1_SHORT_NAME
        else:
            short_name = NISARCollection.GUNW_PR_SHORT_NAME

    # Set up search URL and params
    search_url = "https://cmr.earthdata.nasa.gov/search/granules.umm_json"
    params: Dict[str, Any] = {
        "short_name": short_name,
        "provider": provider,
        "page_size": 2000,  # Use maximum CMR page size of 2000 for efficiency
    }

    # Add bounding box for spatial filtering
    if bbox is not None:
        west, south, east, north = bbox
        params["bounding_box"] = f"{west},{south},{east},{north}"

    # Add temporal filtering
    if start_datetime is not None or end_datetime is not None:
        start_str = start_datetime.isoformat() if start_datetime is not None else ""
        end_str = end_datetime.isoformat() if end_datetime is not None else ""
        params["temporal"] = f"{start_str},{end_str}"

    # Add attribute filters
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

    # Fetch all pages with optimized parameters for large result sets
    logger.info(f"Searching for NISAR products with max_results={max_results}, max_workers={max_workers}")

    # Ensure we respect CMR rate limits while using parallel workers
    rate_limit_delay = 0.1 if max_workers > 2 else 0.0
    items = fetch_cmr_pages(search_url, params, max_workers=max_workers, rate_limit_delay=rate_limit_delay)

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
    products.sort(key=lambda p: (
        p.track or 0,
        p.frame or 0,
        p.direction or "",
        p.start_datetime
    ))

    return products


def download_products(
    products: List[NISARProduct],
    output_dir: Union[str, Path],
    skip_existing: bool = True,
    max_workers: int = 4,
) -> List[str]:
    """Download NISAR products.

    Parameters
    ----------
    products : List[NISARProduct]
        List of NISAR products to download.
    output_dir : Union[str, Path]
        Output directory.
    skip_existing : bool
        Skip files that already exist.
    max_workers : int
        Maximum number of concurrent downloads.

    Returns
    -------
    List[str]
        List of downloaded file paths.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    downloaded_files = []

    with tqdm(total=len(products), desc="Downloading") as pbar:
        # Use ThreadPoolExecutor for concurrent downloads
        if max_workers > 1 and len(products) > 1:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_product = {
                    executor.submit(
                        download_earthdata_granule,
                        product.granule_id,
                        output_dir=output_dir,
                        skip_existing=skip_existing
                    ): product for product in products
                }

                for future in tqdm(future_to_product, desc="Downloading", leave=False):
                    product = future_to_product[future]
                    try:
                        files = future.result()
                        downloaded_files.extend(files)
                    except Exception as e:
                        logger.error(f"Error downloading {product.title}: {e}")
                    pbar.update(1)
        else:
            # Sequential download
            for product in products:
                try:
                    files = download_earthdata_granule(
                        product.granule_id,
                        output_dir=output_dir,
                        skip_existing=skip_existing
                    )
                    downloaded_files.extend(files)
                except Exception as e:
                    logger.error(f"Error downloading {product.title}: {e}")
                pbar.update(1)

    return downloaded_files


def products_to_dataframe(products: List[NISARProduct]) -> pd.DataFrame:
    """Convert a list of NISAR products to a pandas DataFrame.

    Parameters
    ----------
    products : List[NISARProduct]
        List of NISAR products.

    Returns
    -------
    pd.DataFrame
        DataFrame with product information.
    """
    data = []
    for product in products:
        data.append({
            "granule_id": product.granule_id,
            "title": product.title,
            "product_type": product.product_type.value,
            "filename": product.filename,
            "url": product.url,
            "start_datetime": product.start_datetime,
            "end_datetime": product.end_datetime,
            "track": product.track,
            "frame": product.frame,
            "direction": product.direction,
            "cycle": product.cycle,
            "polarization": product.polarization,
            "track_frame_id": product.track_frame_id,
            "date": product.date,
        })

    return pd.DataFrame(data)


def main():
    """Command line interface for searching NISAR products."""
    parser = argparse.ArgumentParser(description="Search for NISAR products")
    parser.add_argument("--bbox", type=str, help="Bounding box as 'west,south,east,north'")
    parser.add_argument("--track", type=int, help="Track number")
    parser.add_argument("--frame", type=int, help="Frame number")
    parser.add_argument("--direction", type=str, choices=["A", "D"], help="Orbit direction (A/D)")
    parser.add_argument("--cycle", type=int, help="Cycle number")
    parser.add_argument(
        "--product-type",
        type=str,
        choices=["GSLC", "GUNW"],
        default="GSLC",
        help="Product type (default: GSLC)"
    )
    parser.add_argument("--polarization", type=str, help="Polarization (e.g., HH)")
    parser.add_argument("--start-date", type=str, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end-date", type=str, help="End date (YYYY-MM-DD)")
    parser.add_argument("--provider", type=str, default="ASF", help="Data provider (default: ASF)")
    parser.add_argument("--max-results", type=int, default=100, help="Maximum results (default: 100)")
    parser.add_argument("--download", type=str, help="Download to this directory")
    parser.add_argument("--output-csv", type=str, help="Save results to CSV")
    parser.add_argument("--url-type", type=str, choices=["https", "s3"], default="https", help="URL type (default: https)")

    args = parser.parse_args()

    # Convert bbox string to tuple
    bbox = None
    if args.bbox:
        bbox = tuple(float(x) for x in args.bbox.split(","))

    # Convert dates to datetime objects
    start_datetime = None
    end_datetime = None
    if args.start_date:
        start_datetime = datetime.fromisoformat(args.start_date)
    if args.end_date:
        end_datetime = datetime.fromisoformat(args.end_date)

    # Search for products
    products = search_nisar_products(
        bbox=bbox,
        track=args.track,
        frame=args.frame,
        direction=args.direction,
        cycle=args.cycle,
        product_type=args.product_type,
        polarization=args.polarization,
        start_datetime=start_datetime,
        end_datetime=end_datetime,
        url_type=UrlType(args.url_type),
        provider=args.provider,
        max_results=args.max_results,
    )

    # Print results
    print(f"Found {len(products)} products")
    if products:
        df = products_to_dataframe(products)
        print(df[["title", "track", "frame", "direction", "cycle", "date"]].to_string(index=False))

        # Save to CSV if requested
        if args.output_csv:
            df.to_csv(args.output_csv, index=False)
            print(f"Results saved to {args.output_csv}")

        # Download if requested
        if args.download:
            downloaded = download_products(products, args.download)
            print(f"Downloaded {len(downloaded)} files to {args.download}")


if __name__ == "__main__":
    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    # Configure requests to use netrc for authentication
    try:
        import netrc
    except ImportError:
        pass

    main()