"""NISAR Data Download Utility.

This script downloads NISAR data from CMR using either granule IDs or URLs.
It incorporates progress reporting and robust error handling.
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

from nisar_db.download import (
    download_earthdata_granule,
    download_from_url,
    download_s3_url,
)
from nisar_db.logging_setup import configure_logging

logger = configure_logging("download_nisar")


def main():
    """Parse arguments and download NISAR data from the command line."""
    parser = argparse.ArgumentParser(description="Download NISAR data from CMR")

    # Create mutually exclusive group for input type
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--granule-id", help="CMR granule ID to download")
    input_group.add_argument(
        "--granule-list", help="File containing list of granule IDs (one per line)"
    )
    input_group.add_argument("--url", help="Direct URL to download")
    input_group.add_argument(
        "--url-list", help="File containing list of URLs (one per line)"
    )
    input_group.add_argument("--s3", help="S3 URL to download using AWS CLI")

    # Output options
    parser.add_argument(
        "--output-dir",
        "-o",
        default="./downloads",
        help="Directory to save downloaded files (default: ./downloads)",
    )

    # Download options
    parser.add_argument(
        "--no-skip-existing",
        action="store_false",
        dest="skip_existing",
        help="Download files even if they already exist locally",
    )
    parser.add_argument(
        "--no-progress",
        action="store_false",
        dest="show_progress",
        help="Disable progress reporting during download",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Timeout in seconds for HTTP requests (default: 60)",
    )

    # AWS options for S3 downloads
    parser.add_argument(
        "--region",
        default="us-west-2",
        help="AWS region for S3 downloads (default: us-west-2)",
    )
    parser.add_argument("--profile", help="AWS profile to use for S3 downloads")

    args = parser.parse_args()

    # Create output directory
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    start_time = datetime.now()
    logger.info(f"Starting download at {start_time}")

    downloaded_files = []

    # Process based on input type
    if args.granule_id:
        # Download single granule
        logger.info(f"Downloading granule: {args.granule_id}")
        downloaded = download_earthdata_granule(
            args.granule_id,
            output_dir=output_dir,
            skip_existing=args.skip_existing,
            show_progress=args.show_progress,
            timeout=args.timeout,
        )
        downloaded_files.extend(downloaded)

    elif args.granule_list:
        # Download multiple granules from list
        with open(args.granule_list, "r") as f:
            granule_ids = [line.strip() for line in f if line.strip()]

        logger.info(f"Found {len(granule_ids)} granule IDs in {args.granule_list}")

        for i, granule_id in enumerate(granule_ids, 1):
            logger.info(f"Processing granule {i}/{len(granule_ids)}: {granule_id}")
            try:
                downloaded = download_earthdata_granule(
                    granule_id,
                    output_dir=output_dir,
                    skip_existing=args.skip_existing,
                    show_progress=args.show_progress,
                    timeout=args.timeout,
                )
                downloaded_files.extend(downloaded)
            except Exception:
                logger.exception(f"Error processing granule {granule_id}")

    elif args.url:
        # Download from direct URL
        logger.info(f"Downloading from URL: {args.url}")
        filepath = download_from_url(
            args.url,
            output_dir=output_dir,
            skip_existing=args.skip_existing,
            show_progress=args.show_progress,
            timeout=args.timeout,
        )
        if filepath:
            downloaded_files.append(filepath)

    elif args.url_list:
        # Download from multiple URLs
        with open(args.url_list, "r") as f:
            urls = [line.strip() for line in f if line.strip()]

        logger.info(f"Found {len(urls)} URLs in {args.url_list}")

        for i, url in enumerate(urls, 1):
            logger.info(f"Processing URL {i}/{len(urls)}")
            try:
                filepath = download_from_url(
                    url,
                    output_dir=output_dir,
                    skip_existing=args.skip_existing,
                    show_progress=args.show_progress,
                    timeout=args.timeout,
                )
                if filepath:
                    downloaded_files.append(filepath)
            except Exception:
                logger.exception(f"Error processing URL {url}")

    elif args.s3:
        # Download from S3 URL
        logger.info(f"Downloading from S3: {args.s3}")
        filepath = download_s3_url(
            args.s3, output_dir=output_dir, region=args.region, profile=args.profile
        )
        if filepath:
            downloaded_files.append(filepath)

    # Print summary
    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()

    logger.info("=" * 60)
    logger.info("Download Summary:")
    logger.info(f"- Files downloaded: {len(downloaded_files)}")
    logger.info(f"- Output directory: {output_dir}")
    logger.info(f"- Total time: {duration:.1f} seconds")
    logger.info("=" * 60)

    if not downloaded_files:
        logger.warning("No files were downloaded")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
