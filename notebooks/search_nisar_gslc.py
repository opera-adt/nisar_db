#!/usr/bin/env python3
"""Example script to search for NISAR GSLC data in EarthData.

This script demonstrates how to:
1. Search for NISAR GSLC data using CMR API
2. Download selected granules using nisar_db.download
3. Get the North America shape polygon for filtering
"""

import argparse
import json
import requests
import sys
from pathlib import Path
import pandas as pd

# Add the parent directory to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nisar_db.download import download_earthdata_granule
from nisar_db.geodb import get_opera_na_shape


def search_nisar_gslc(
    start_date=None, end_date=None, max_results=10, download_path=None
):
    """Search for NISAR GSLC data in EarthData CMR.

    Parameters
    ----------
    start_date : str, optional
        Start date in YYYY-MM-DD format
    end_date : str, optional
        End date in YYYY-MM-DD format
    max_results : int, optional
        Maximum number of results to return, by default 10
    download_path : str, optional
        Path to download the data to. If None, data is not downloaded.

    Returns
    -------
    list
        List of granule IDs
    """
    # CMR API endpoint
    cmr_url = "https://cmr.earthdata.nasa.gov/search/granules.json"

    # Build query parameters
    params = {
        "provider": "ASF",  # Alaska Satellite Facility
        "short_name": "NISAR_L2_PR_GSLC",  # NISAR GSLC product
        "page_size": max_results,
        "sort_key": "-start_date",  # Most recent first
    }

    if start_date:
        params["temporal[start]"] = f"{start_date}T00:00:00Z"
    if end_date:
        params["temporal[end]"] = f"{end_date}T23:59:59Z"

    print(f"Searching for NISAR GSLC data with parameters: {params}")

    # Make the request
    response = requests.get(cmr_url, params=params, timeout=30)
    response.raise_for_status()

    # Parse the response
    results = response.json()

    if "feed" not in results or "entry" not in results["feed"]:
        print("No results found")
        return []

    entries = results["feed"]["entry"]
    print(f"Found {len(entries)} GSLC products")

    # Extract granule IDs and details
    granule_info = []
    for entry in entries:
        granule_id = entry.get("id", "")
        title = entry.get("title", "")
        time_start = entry.get("time_start", "")
        time_end = entry.get("time_end", "")

        granule_info.append({
            "granule_id": granule_id,
            "title": title,
            "time_start": time_start,
            "time_end": time_end,
        })

    # Convert to DataFrame for display
    df = pd.DataFrame(granule_info)
    print("\nGranule information:")
    print(df)

    # Download data if requested
    if download_path:
        download_dir = Path(download_path)
        download_dir.mkdir(exist_ok=True, parents=True)

        for idx, granule in enumerate(granule_info):
            print(f"\nDownloading granule {idx+1}/{len(granule_info)}: {granule['title']}")
            files = download_earthdata_granule(
                granule["granule_id"], output_dir=download_dir, skip_existing=True
            )
            print(f"Downloaded files: {files}")

    # Return granule IDs
    return [g["granule_id"] for g in granule_info]


def get_north_america_shape():
    """Get the North America shape and display basic information."""
    print("Getting North America shape...")
    na_shape = get_opera_na_shape()

    # Print basic information about the shape
    print(f"North America shape type: {na_shape.geom_type}")
    print(f"North America shape area: {na_shape.area:.2f}")
    print(f"North America shape bounds: {na_shape.bounds}")

    return na_shape


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Search for NISAR GSLC data")
    parser.add_argument("--start-date", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end-date", help="End date (YYYY-MM-DD)")
    parser.add_argument(
        "--max-results", type=int, default=10, help="Maximum number of results"
    )
    parser.add_argument(
        "--download", help="Download granules to this directory", default=None
    )
    parser.add_argument(
        "--get-na-shape",
        action="store_true",
        help="Get North America shape"
    )

    args = parser.parse_args()

    if args.get_na_shape:
        na_shape = get_north_america_shape()

    granule_ids = search_nisar_gslc(
        start_date=args.start_date,
        end_date=args.end_date,
        max_results=args.max_results,
        download_path=args.download,
    )