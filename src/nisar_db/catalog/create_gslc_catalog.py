"""Create a catalog of NISAR GSLC products.

This script:
1. Searches for NISAR GSLC products in CMR
2. Extracts metadata from the search results
3. Stores the metadata in a DuckDB database
4. Generates JSON catalog files similar to burst_db

The JSON files can be used by applications to find NISAR GSLC products.
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
from nisar_db.filenames import GSLCFilename, NISARCollection
from nisar_db.logging_setup import configure_logging
from nisar_db.search_nisar import search_nisar_granules

logger = configure_logging("create_gslc_catalog")

TABLE = "gslc_products"

_SCHEMA = """
    id VARCHAR PRIMARY KEY,
    mission VARCHAR,
    instrument VARCHAR,
    processing_type VARCHAR,
    product VARCHAR,
    cycle VARCHAR,
    relative_orbit VARCHAR,
    track VARCHAR,
    pass_direction VARCHAR,
    track_frame VARCHAR,
    frame VARCHAR,
    mode VARCHAR,
    polarization VARCHAR,
    source VARCHAR,
    start_datetime TIMESTAMP,
    end_datetime TIMESTAMP,
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


def search_gslc_products(max_results: int = 25000) -> pd.DataFrame:
    """Search for NISAR GSLC products in CMR."""
    logger.info("Searching for NISAR GSLC products in CMR...")

    results = search_nisar_granules(
        short_name=NISARCollection.GSLC_BETA_V1_SHORT_NAME,
        provider=NISARCollection.DEFAULT_PROVIDER,
        max_results=max_results,
        output_format="umm_json",
    )

    logger.info(f"Found {len(results)} GSLC products")
    return results


def generate_catalog_json(conn, output_dir: str) -> None:
    """Generate GSLC catalog JSON files similar to burst_db."""
    logger.info("Generating catalog JSON files...")

    # Shared tracks/frames catalogs
    generate_track_frame_json(conn, TABLE, output_dir, prefix="gslc")

    # Generate dates catalog
    dates_df = conn.execute("""
    SELECT DISTINCT track, frame, pass_direction, date
    FROM gslc_products
    ORDER BY track::INTEGER, frame::INTEGER, pass_direction, date
    """).fetchdf()

    dates: dict[str, list[str]] = {}
    for _, row in dates_df.iterrows():
        key = f"T{row['track']}_F{row['frame']}_{row['pass_direction']}"
        dates.setdefault(key, []).append(row["date"])
    write_catalog_json(output_dir, "gslc_dates.json", "dates", dates)

    # Generate scenes catalog
    scenes_df = conn.execute("""
    SELECT
        track,
        frame,
        pass_direction,
        date,
        polarization,
        id,
        granule_id,
        url,
        s3_url,
        browse_url,
        metadata_url
    FROM gslc_products
    ORDER BY track::INTEGER, frame::INTEGER, pass_direction, date, polarization
    """).fetchdf()

    scenes = {}
    for _, row in scenes_df.iterrows():
        track = row["track"]
        frame = row["frame"]
        pass_direction = row["pass_direction"]
        date = row["date"]
        polarization = row["polarization"]

        scene_id = f"T{track}_F{frame}_{pass_direction}"
        scene_key = f"{scene_id}_{date}"

        if scene_key not in scenes:
            scenes[scene_key] = {
                "scene_id": scene_id,
                "track": track,
                "frame": frame,
                "pass_direction": pass_direction,
                "date": date,
                "polarizations": {},
                "granule_ids": [],
            }

        scenes[scene_key]["polarizations"][polarization] = {
            "id": row["id"],
            "granule_id": row["granule_id"],
            "url": row["url"],
            "s3_url": row["s3_url"],
            "browse_url": row["browse_url"],
            "metadata_url": row["metadata_url"],
        }

        if row["granule_id"] not in scenes[scene_key]["granule_ids"]:
            scenes[scene_key]["granule_ids"].append(row["granule_id"])

    write_catalog_json(output_dir, "gslc_scenes.json", "scenes", list(scenes.values()))

    logger.info(f"Generated catalog JSON files in {output_dir}")


def main():
    """Create the GSLC catalog (DuckDB + JSON) from the command line."""
    parser = argparse.ArgumentParser(
        description="Create a catalog of NISAR GSLC products"
    )

    parser.add_argument(
        "--db-path",
        default="gslc_catalog.duckdb",
        help="Path to the DuckDB database file",
    )
    parser.add_argument(
        "--output-dir", default="catalog", help="Directory to save JSON files"
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=25000,
        help="Maximum number of results to return from CMR search",
    )

    args = parser.parse_args()

    # Create or connect to the database
    conn = create_database(args.db_path, TABLE, _SCHEMA)

    try:
        results_df = search_gslc_products(max_results=args.max_results)

        if len(results_df) > 0:
            metadata_df = extract_metadata(results_df, GSLCFilename, logger)
            update_database(conn, metadata_df, TABLE, logger)
            generate_catalog_json(conn, args.output_dir)
        else:
            logger.warning("No GSLC products found")
    except Exception:
        logger.exception("Error creating GSLC catalog")
        return 1
    else:
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
