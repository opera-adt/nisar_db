#!/usr/bin/env python3
"""
Script to retrieve all available NISAR GSLC products efficiently.
This optimized version is designed to handle potentially 10,000+ products.
"""

import sys
import time
import logging
import argparse
import concurrent.futures
import requests
from pathlib import Path
from datetime import datetime

import pandas as pd

# Add the src directory to sys.path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from nisar_db.search_nisar import search_nisar_products, ProductType, NISARProduct, UrlType
from nisar_db.filenames import NISARCollection

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("get_all_nisar_gslc")


def main():
    """Main function to retrieve all NISAR GSLC products."""
    parser = argparse.ArgumentParser(description="Retrieve all available NISAR GSLC products")
    parser.add_argument(
        "--output", "-o",
        default="all_nisar_gslc.csv",
        help="Output CSV file name (default: all_nisar_gslc.csv)"
    )
    parser.add_argument(
        "--max-results", "-m",
        type=int,
        default=25000,  # Increased to get all 23,450 GSLC products
        help="Maximum number of results to retrieve (default: 25000)"
    )
    parser.add_argument(
        "--workers", "-w",
        type=int,
        default=4,
        help="Maximum number of concurrent workers (default: 4)"
    )
    parser.add_argument(
        "--provider", "-p",
        default="ASF",
        help="Provider to search (default: ASF)"
    )
    args = parser.parse_args()

    start_timestamp = time.time()
    logger.info(f"Starting search for NISAR GSLC products (max_results={args.max_results}, workers={args.workers})")

    # Use the correct collection name
    short_name = NISARCollection.GSLC_BETA_V1_SHORT_NAME
    logger.info(f"Using collection: {short_name}")

    # Try direct JSON search first (which has been confirmed to work)
    logger.info("Trying direct JSON search first...")

    # CMR API endpoint - use JSON format which is known to work
    cmr_url = "https://cmr.earthdata.nasa.gov/search/granules.json"

    # Build query parameters
    params = {
        "provider": args.provider,
        "short_name": short_name,
        "page_size": 2000,  # Maximum CMR page size
        "sort_key": "-start_date",
    }

    logger.info(f"Direct search parameters: {params}")

    try:
        # First make a request to get the total hits
        first_params = params.copy()
        first_params["page_num"] = 1
        response = requests.get(cmr_url, params=first_params, timeout=60)
        response.raise_for_status()

        # Get total hits from CMR header
        total_hits = int(response.headers.get("CMR-Hits", "0"))
        page_size = params.get("page_size", 2000)
        total_pages = (total_hits + page_size - 1) // page_size

        logger.info(f"CMR has {total_hits} total hits across {total_pages} pages")

        # Function to fetch a specific page
        def fetch_page(page_num):
            page_params = params.copy()
            page_params["page_num"] = page_num
            try:
                # Add a small delay to avoid overwhelming the server
                if page_num > 1:
                    time.sleep(0.2 * (page_num % args.workers))

                logger.info(f"Fetching page {page_num}/{total_pages}")
                response = requests.get(cmr_url, params=page_params, timeout=60)
                response.raise_for_status()
                page_results = response.json()

                if "feed" in page_results and "entry" in page_results["feed"]:
                    return page_results["feed"]["entry"]
                else:
                    logger.warning(f"No entries found in page {page_num}")
                    return []
            except Exception as e:
                logger.error(f"Error fetching page {page_num}: {e}")
                return []

        # Get first page results (already fetched)
        first_page_results = response.json()
        entries = []
        if "feed" in first_page_results and "entry" in first_page_results["feed"]:
            entries.extend(first_page_results["feed"]["entry"])
            logger.info(f"Retrieved {len(entries)} granules from first page")

        # Fetch remaining pages in parallel
        if total_pages > 1:
            # Use at most args.workers workers, but no more than pages needed
            max_workers = min(args.workers, total_pages - 1)
            logger.info(f"Fetching remaining {total_pages - 1} pages using {max_workers} workers")

            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                # Submit all page fetches (starting from page 2)
                future_to_page = {
                    executor.submit(fetch_page, page_num): page_num
                    for page_num in range(2, total_pages + 1)
                }

                # Collect results as they complete
                completed = 0
                for future in concurrent.futures.as_completed(future_to_page):
                    page_num = future_to_page[future]
                    page_entries = future.result()
                    entries.extend(page_entries)

                    completed += 1
                    if completed % 5 == 0 or completed == total_pages - 1:
                        logger.info(f"Progress: {completed}/{total_pages - 1} pages, {len(entries)} total granules so far")

        logger.info(f"Direct JSON search found {len(entries)} granules")

        # Convert to product objects
        products = []
        for entry in entries:
            try:
                # Create a simplified product object
                title = entry.get("title", "")
                granule_id = entry.get("id", "")

                # Extract track, frame, etc from title
                # Example: NISAR_L2_PR_GSLC_010_164_D_076_2005_QPDH_A_20260120T140558_20260120T140633_X05010_N_F_J_001
                parts = title.split("_")

                # Handle title parsing more carefully
                track = None
                frame = None
                direction = None

                try:
                    # Find the position of "GSLC" marker to anchor our parsing
                    gslc_pos = -1
                    for i, part in enumerate(parts):
                        if part == "GSLC" or part == "PR_GSLC":
                            gslc_pos = i
                            break

                    # If found, parse based on relative positions
                    if gslc_pos >= 0 and gslc_pos + 3 < len(parts):
                        try:
                            track = int(parts[gslc_pos + 1])
                        except (ValueError, IndexError):
                            track = None

                        try:
                            direction = parts[gslc_pos + 3]
                        except (ValueError, IndexError):
                            direction = None

                        try:
                            frame = int(parts[gslc_pos + 4])
                        except (ValueError, IndexError):
                            frame = None
                except Exception:
                    # If any error occurs, use None values
                    track = None
                    frame = None
                    direction = None

                # Extract dates from title
                start_time = None
                end_time = None
                for i, part in enumerate(parts):
                    if len(part) == 15 and part.startswith("20") and "T" in part:
                        start_time = datetime.strptime(part, "%Y%m%dT%H%M%S")
                        if i + 1 < len(parts) and len(parts[i+1]) == 15 and "T" in parts[i+1]:
                            end_time = datetime.strptime(parts[i+1], "%Y%m%dT%H%M%S")
                        break

                if not start_time:
                    start_time = datetime.now()
                    end_time = datetime.now()

                # Get URL
                url = ""
                if "links" in entry:
                    for link in entry["links"]:
                        if "rel" in link and "data" in link["rel"]:
                            url = link.get("href", "")
                            break

                # Create the product object
                product = NISARProduct(
                    granule_id=granule_id,
                    title=title,
                    product_type=ProductType.GSLC,
                    filename=title + ".h5" if not url else url.split("/")[-1],
                    url=url,
                    start_datetime=start_time,
                    end_datetime=end_time or start_time,
                    bbox=None,
                    track=track,
                    frame=frame,
                    direction=direction,
                    cycle=None,
                    polarization=None,
                    metadata=entry
                )

                products.append(product)

                # Limit to max_results
                if len(products) >= args.max_results:
                    break

            except Exception as e:
                logger.error(f"Error processing entry: {e}")

        logger.info(f"Successfully processed {len(products)} products")

    except Exception as e:
        logger.error(f"Error in direct JSON search: {e}")
        logger.info("Falling back to standard search...")

        # Fallback to the standard search method
        products = search_nisar_products(
            product_type=ProductType.GSLC,
            provider=args.provider,
            short_name=short_name,
            max_results=args.max_results,
            max_workers=args.workers
        )

    # Extract fields and save to CSV
    if products:
        # Extract fields
        data = []
        for product in products:
            data.append({
                "granule_id": product.granule_id,
                "name": product.name,
                "track": product.track,
                "frame": product.frame,
                "direction": product.direction,
                "date": product.date,
                "start_datetime": product.start_datetime.isoformat(),
                "end_datetime": product.end_datetime.isoformat(),
                "polarization": product.polarization,
                "url": product.url,
                "track_frame_id": product.track_frame_id
            })

        df = pd.DataFrame(data)

        # Save to CSV
        df.to_csv(args.output, index=False)
        logger.info(f"Saved {len(df)} products to {args.output}")

        # Print summary
        logger.info("Summary of retrieved products:")
        logger.info(f"- Unique tracks: {len(df['track'].unique())}")
        logger.info(f"- Unique frames: {len(df['frame'].unique())}")
        logger.info(f"- Date range: {df['date'].min()} to {df['date'].max()}")

        # Print a few examples
        logger.info("\nExample products:")
        for i, row in df.head(3).iterrows():
            logger.info(f"{i+1}. {row['name']}")
            logger.info(f"   Track: {row['track']}, Frame: {row['frame']}, Date: {row['date']}")
    else:
        logger.warning("No NISAR GSLC products found!")

    elapsed_time = time.time() - start_timestamp
    logger.info(f"Total execution time: {elapsed_time:.2f} seconds")


if __name__ == "__main__":
    main()