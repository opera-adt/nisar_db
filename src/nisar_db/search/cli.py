"""Command-line interface for searching NISAR products (CMR or S3 backend)."""

from __future__ import annotations

import argparse
import logging
from datetime import datetime

from .cmr import search_nisar_products
from .frames import download_products, products_to_dataframe
from .models import UrlType
from .s3 import search_s3_products


def main():
    """Command line interface for searching NISAR products."""
    parser = argparse.ArgumentParser(description="Search for NISAR products")
    parser.add_argument(
        "--bbox", type=str, help="Bounding box as 'west,south,east,north'"
    )
    parser.add_argument("--track", type=int, help="Track number")
    parser.add_argument("--frame", type=int, help="Frame number")
    parser.add_argument(
        "--direction", type=str, choices=["A", "D"], help="Orbit direction (A/D)"
    )
    parser.add_argument("--cycle", type=int, help="Cycle number")
    parser.add_argument(
        "--product-type",
        type=str,
        choices=["GSLC", "GUNW"],
        default="GSLC",
        help="Product type (default: GSLC)",
    )
    parser.add_argument("--polarization", type=str, help="Polarization (e.g., HH)")
    parser.add_argument("--start-date", type=str, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end-date", type=str, help="End date (YYYY-MM-DD)")
    parser.add_argument(
        "--provider", type=str, default="ASF", help="Data provider (default: ASF)"
    )
    parser.add_argument(
        "--max-results", type=int, default=100, help="Maximum results (default: 100)"
    )
    parser.add_argument("--download", type=str, help="Download to this directory")
    parser.add_argument("--output-csv", type=str, help="Save results to CSV")
    parser.add_argument(
        "--url-type",
        type=str,
        choices=["https", "s3"],
        default="https",
        help="URL type (default: https)",
    )

    # S3-bucket search backend (instead of CMR). When --s3-bucket is given,
    # the bucket is listed with boto3 and keys are parsed into products.
    parser.add_argument(
        "--s3-bucket",
        type=str,
        help="Search this S3 bucket instead of CMR (name or s3:// URI)",
    )
    parser.add_argument(
        "--s3-prefix",
        type=str,
        default="",
        help="Key prefix to scan, e.g. products/L2_L_GSLC/",
    )
    parser.add_argument(
        "--profile", type=str, help="AWS named profile for the S3 search"
    )
    parser.add_argument(
        "--region",
        type=str,
        default="us-west-2",
        help="AWS region for the S3 search (default: us-west-2)",
    )

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

    # Search for products: S3 bucket backend if requested, else CMR.
    if args.s3_bucket:
        products = search_s3_products(
            bucket=args.s3_bucket,
            prefix=args.s3_prefix,
            profile=args.profile,
            region=args.region,
            product_type=args.product_type,
            track=args.track,
            frame=args.frame,
            direction=args.direction,
            cycle=args.cycle,
            polarization=args.polarization,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
            max_results=args.max_results,
        )
    else:
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
        print(
            df[["title", "track", "frame", "direction", "cycle", "date"]].to_string(
                index=False
            )
        )

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
    main()
