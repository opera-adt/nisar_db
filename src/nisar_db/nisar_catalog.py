#!/usr/bin/env python3
"""
NISAR Catalog Creation Tool

This script creates a catalog of NISAR products, including:
- GSLC (Geocoded Single Look Complex)
- GUNW (Geocoded Unwrapped Interferogram)

The catalog includes:
- Searchable DuckDB databases
- JSON files for easy consumption by applications

The JSON files are similar to those produced by burst_db.
"""

import argparse
import logging
import sys
from pathlib import Path

from nisar_db.catalog.create_gslc_catalog import main as create_gslc_catalog
from nisar_db.catalog.create_gunw_catalog import main as create_gunw_catalog

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("create_nisar_catalog")


def main():
    """Main function to parse arguments and create catalogs."""
    parser = argparse.ArgumentParser(description="Create catalogs of NISAR products")

    # Output options
    parser.add_argument("--output-dir", "-o", default="catalog",
                      help="Directory to save JSON files (default: ./catalog)")

    # Database options
    parser.add_argument("--gslc-db", default="gslc_catalog.duckdb",
                      help="Path to the GSLC DuckDB database file (default: ./gslc_catalog.duckdb)")
    parser.add_argument("--gunw-db", default="gunw_catalog.duckdb",
                      help="Path to the GUNW DuckDB database file (default: ./gunw_catalog.duckdb)")

    # Product selection
    parser.add_argument("--gslc", action="store_true",
                      help="Create GSLC catalog")
    parser.add_argument("--gunw", action="store_true",
                      help="Create GUNW catalog")
    parser.add_argument("--all", action="store_true",
                      help="Create catalogs for all product types")

    # Search options
    parser.add_argument("--max-results", type=int, default=25000,
                      help="Maximum number of results to return from CMR search (default: 25000)")

    args = parser.parse_args()

    # Default to all if no product types specified
    if not (args.gslc or args.gunw or args.all):
        args.all = True

    # Create output directory
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Set system arguments for subcommands
    success = True

    # Create GSLC catalog
    if args.gslc or args.all:
        logger.info("Creating GSLC catalog...")
        sys.argv = [
            "create_gslc_catalog.py",
            f"--db-path={args.gslc_db}",
            f"--output-dir={args.output_dir}",
            f"--max-results={args.max_results}"
        ]
        result = create_gslc_catalog()
        if result != 0:
            success = False
            logger.error("Failed to create GSLC catalog")

    # Create GUNW catalog
    if args.gunw or args.all:
        logger.info("Creating GUNW catalog...")
        sys.argv = [
            "create_gunw_catalog.py",
            f"--db-path={args.gunw_db}",
            f"--output-dir={args.output_dir}",
            f"--max-results={args.max_results}"
        ]
        result = create_gunw_catalog()
        if result != 0:
            success = False
            logger.error("Failed to create GUNW catalog")

    if success:
        logger.info(f"Successfully created NISAR catalogs in {output_dir}")
        return 0
    else:
        logger.error("Failed to create one or more NISAR catalogs")
        return 1


if __name__ == "__main__":
    sys.exit(main())