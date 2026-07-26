"""Create a catalog of NISAR GUNW products.

This script:
1. Searches for NISAR GUNW products in CMR
2. Extracts metadata from the search results
3. Stores the metadata in a DuckDB database
4. Generates JSON catalog files similar to burst_db

The JSON files can be used by applications to find NISAR GUNW products.
"""

import argparse
import sys

import pandas as pd

from nisar_db.catalog._common import (
    create_database,
    extract_metadata,
    generate_track_frame_json,
    update_database,
    write_catalog_json,
)
from nisar_db.filenames import GUNWFilename, NISARCollection
from nisar_db.logging_setup import configure_logging
from nisar_db.search_nisar import search_nisar_granules

logger = configure_logging("create_gunw_catalog")

TABLE = "gunw_products"

_SCHEMA = """
    id VARCHAR PRIMARY KEY,
    mission VARCHAR,
    instrument VARCHAR,
    processing_type VARCHAR,
    product VARCHAR,
    cycle1 VARCHAR,
    relative_orbit VARCHAR,
    track VARCHAR,
    pass_direction VARCHAR,
    track_frame VARCHAR,
    frame VARCHAR,
    cycle2 VARCHAR,
    mode VARCHAR,
    polarization VARCHAR,
    reference_start_datetime TIMESTAMP,
    reference_end_datetime TIMESTAMP,
    secondary_start_datetime TIMESTAMP,
    secondary_end_datetime TIMESTAMP,
    ref_date VARCHAR,
    sec_date VARCHAR,
    date VARCHAR,
    scene_id VARCHAR,
    crid VARCHAR,
    orbits VARCHAR,
    coverage VARCHAR,
    location VARCHAR,
    version VARCHAR,
    granule_id VARCHAR,
    url VARCHAR,
    s3_url VARCHAR,
    browse_url VARCHAR,
    metadata_url VARCHAR,
    inserted_at TIMESTAMP
"""


def search_gunw_products(max_results: int | None = None) -> pd.DataFrame:
    """Search for NISAR GUNW products in CMR."""
    logger.info("Searching for NISAR GUNW products in CMR...")

    results = search_nisar_granules(
        short_name=NISARCollection.GUNW_PROVISIONAL_V1_SHORT_NAME,
        provider=NISARCollection.DEFAULT_PROVIDER,
        max_results=max_results,
        output_format="umm_json",
    )

    logger.info(f"Found {len(results)} GUNW products")
    return results


def generate_catalog_json(conn, output_dir: str) -> None:
    """Generate GUNW catalog JSON files similar to burst_db."""
    logger.info("Generating catalog JSON files...")

    # Shared tracks/frames catalogs
    generate_track_frame_json(conn, TABLE, output_dir, prefix="gunw")

    # Generate interferogram pairs catalog
    pairs_df = conn.execute("""
    SELECT DISTINCT
        track,
        frame,
        pass_direction,
        ref_date,
        sec_date,
        date
    FROM gunw_products
    ORDER BY track::INTEGER, frame::INTEGER, pass_direction, ref_date, sec_date
    """).fetchdf()

    pairs: dict[str, list[dict]] = {}
    for _, row in pairs_df.iterrows():
        key = f"T{row['track']}_F{row['frame']}_{row['pass_direction']}"
        pairs.setdefault(key, []).append(
            {
                "ref_date": row["ref_date"],
                "sec_date": row["sec_date"],
                "pair": row["date"],
            }
        )
    write_catalog_json(output_dir, "gunw_pairs.json", "pairs", pairs)

    # Generate interferograms catalog
    ifgs_df = conn.execute("""
    SELECT
        track,
        frame,
        pass_direction,
        ref_date,
        sec_date,
        date,
        polarization,
        id,
        granule_id,
        url,
        s3_url,
        browse_url,
        metadata_url
    FROM gunw_products
    ORDER BY track::INTEGER, frame::INTEGER, pass_direction,
             ref_date, sec_date, polarization
    """).fetchdf()

    ifgs = {}
    for _, row in ifgs_df.iterrows():
        track = row["track"]
        frame = row["frame"]
        pass_direction = row["pass_direction"]
        ref_date = row["ref_date"]
        sec_date = row["sec_date"]
        date = row["date"]
        polarization = row["polarization"]

        scene_id = f"T{track}_F{frame}_{pass_direction}"
        ifg_key = f"{scene_id}_{date}"

        if ifg_key not in ifgs:
            ifgs[ifg_key] = {
                "scene_id": scene_id,
                "track": track,
                "frame": frame,
                "pass_direction": pass_direction,
                "ref_date": ref_date,
                "sec_date": sec_date,
                "date": date,
                "polarizations": {},
                "granule_ids": [],
            }

        ifgs[ifg_key]["polarizations"][polarization] = {
            "id": row["id"],
            "granule_id": row["granule_id"],
            "url": row["url"],
            "s3_url": row["s3_url"],
            "browse_url": row["browse_url"],
            "metadata_url": row["metadata_url"],
        }

        if row["granule_id"] not in ifgs[ifg_key]["granule_ids"]:
            ifgs[ifg_key]["granule_ids"].append(row["granule_id"])

    write_catalog_json(
        output_dir, "gunw_interferograms.json", "interferograms", list(ifgs.values())
    )

    logger.info(f"Generated catalog JSON files in {output_dir}")


def main():
    """Create the GUNW catalog (DuckDB + JSON) from the command line."""
    parser = argparse.ArgumentParser(
        description="Create a catalog of NISAR GUNW products"
    )

    parser.add_argument(
        "--db-path",
        default="gunw_catalog.duckdb",
        help="Path to the DuckDB database file",
    )
    parser.add_argument(
        "--output-dir", default="catalog", help="Directory to save JSON files"
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=0,
        help="Cap on CMR results; 0 (the default) keeps the whole archive",
    )

    args = parser.parse_args()

    # Create or connect to the database
    conn = create_database(args.db_path, TABLE, _SCHEMA)

    try:
        results_df = search_gunw_products(max_results=args.max_results)

        if len(results_df) > 0:
            metadata_df = extract_metadata(results_df, GUNWFilename, logger)
            update_database(conn, metadata_df, TABLE, logger)
            generate_catalog_json(conn, args.output_dir)
        else:
            logger.warning("No GUNW products found")
    except Exception:
        logger.exception("Error creating GUNW catalog")
        return 1
    else:
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
