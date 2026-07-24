#!/usr/bin/env python3
"""
Create a catalog of NISAR GSLC products.

This script:
1. Searches for NISAR GSLC products in CMR
2. Extracts metadata from the search results
3. Stores the metadata in a DuckDB database
4. Generates JSON catalog files similar to burst_db

The JSON files can be used by applications to find NISAR GSLC products.
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Union

import duckdb
import pandas as pd

from nisar_db.filenames import GSLCFilename, NISARCollection
from nisar_db.search_nisar import search_nisar_granules

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("create_gslc_catalog")


def create_database(db_path: str) -> duckdb.DuckDBPyConnection:
    """
    Create or connect to a DuckDB database for storing GSLC metadata.

    Parameters
    ----------
    db_path : str
        Path to the DuckDB database file.

    Returns
    -------
    duckdb.DuckDBPyConnection
        Connection to the DuckDB database.
    """
    conn = duckdb.connect(db_path)

    # Create table for GSLC products if it doesn't exist
    conn.execute("""
    CREATE TABLE IF NOT EXISTS gslc_products (
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
    )
    """)

    return conn


def search_gslc_products(max_results: int = 25000) -> pd.DataFrame:
    """
    Search for NISAR GSLC products in CMR.

    Parameters
    ----------
    max_results : int, optional
        Maximum number of results to return.

    Returns
    -------
    pd.DataFrame
        DataFrame containing the search results.
    """
    logger.info("Searching for NISAR GSLC products in CMR...")

    results = search_nisar_granules(
        short_name=NISARCollection.GSLC_BETA_V1_SHORT_NAME,
        provider=NISARCollection.DEFAULT_PROVIDER,
        max_results=max_results,
        output_format="umm_json"
    )

    logger.info(f"Found {len(results)} GSLC products")
    return results


def extract_metadata(df: pd.DataFrame) -> pd.DataFrame:
    """
    Extract metadata from CMR search results.

    Parameters
    ----------
    df : pd.DataFrame
        DataFrame containing CMR search results.

    Returns
    -------
    pd.DataFrame
        DataFrame containing extracted metadata.
    """
    logger.info("Extracting metadata from search results...")

    metadata_list = []

    for _, row in df.iterrows():
        granule_id = row['granule_id']
        title = row['title']

        # Extract URLs
        urls = {}
        for link in row.get('links', []):
            rel = link.get('rel', '')
            if rel == 'http://esipfed.org/ns/fedsearch/1.1/data#':
                if link.get('href', '').startswith('s3://'):
                    urls['s3_url'] = link.get('href', '')
                else:
                    urls['url'] = link.get('href', '')
            elif rel == 'http://esipfed.org/ns/fedsearch/1.1/browse#':
                urls['browse_url'] = link.get('href', '')
            elif rel == 'http://esipfed.org/ns/fedsearch/1.1/metadata#':
                urls['metadata_url'] = link.get('href', '')

        # Try to extract filename information
        try:
            filename_obj = GSLCFilename.from_path(title)

            metadata = {
                'id': title.replace('.h5', ''),  # Use filename without extension as ID
                'granule_id': granule_id,
                **urls,
                **filename_obj.to_dataframe().iloc[0].to_dict(),
                'inserted_at': datetime.now(timezone.utc)
            }

            metadata_list.append(metadata)
        except Exception as e:
            logger.warning(f"Could not extract metadata from {title}: {e}")

    metadata_df = pd.DataFrame(metadata_list)
    logger.info(f"Extracted metadata from {len(metadata_df)} products")
    return metadata_df


def update_database(conn: duckdb.DuckDBPyConnection, metadata_df: pd.DataFrame) -> None:
    """
    Update the database with metadata.

    Parameters
    ----------
    conn : duckdb.DuckDBPyConnection
        Connection to the DuckDB database.
    metadata_df : pd.DataFrame
        DataFrame containing metadata.
    """
    logger.info("Updating database with metadata...")

    # Insert new rows
    conn.execute("""
    INSERT OR REPLACE INTO gslc_products
    SELECT * FROM metadata_df
    """)

    conn.commit()

    # Check how many rows were inserted
    count = conn.execute("SELECT COUNT(*) FROM gslc_products").fetchone()[0]
    logger.info(f"Database now contains {count} GSLC products")


def generate_catalog_json(conn: duckdb.DuckDBPyConnection, output_dir: str) -> None:
    """
    Generate catalog JSON files similar to burst_db.

    Parameters
    ----------
    conn : duckdb.DuckDBPyConnection
        Connection to the DuckDB database.
    output_dir : str
        Directory to save JSON files.
    """
    logger.info("Generating catalog JSON files...")

    # Create output directory if it doesn't exist
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    # Generate track catalog
    tracks_df = conn.execute("""
    SELECT DISTINCT track, pass_direction
    FROM gslc_products
    ORDER BY track::INTEGER, pass_direction
    """).fetchdf()

    tracks = {}
    for _, row in tracks_df.iterrows():
        track = row['track']
        pass_direction = row['pass_direction']

        if track not in tracks:
            tracks[track] = []

        tracks[track].append(pass_direction)

    # Save track catalog
    with open(os.path.join(output_dir, 'gslc_tracks.json'), 'w') as f:
        json.dump({
            'tracks': tracks,
            'generated_at': datetime.now(timezone.utc).isoformat()
        }, f, indent=2)

    # Generate frames catalog
    frames_df = conn.execute("""
    SELECT DISTINCT track, frame, pass_direction
    FROM gslc_products
    ORDER BY track::INTEGER, frame::INTEGER, pass_direction
    """).fetchdf()

    frames = {}
    for _, row in frames_df.iterrows():
        track = row['track']
        frame = row['frame']
        pass_direction = row['pass_direction']

        key = f"T{track}_{pass_direction}"
        if key not in frames:
            frames[key] = []

        frames[key].append(frame)

    # Save frames catalog
    with open(os.path.join(output_dir, 'gslc_frames.json'), 'w') as f:
        json.dump({
            'frames': frames,
            'generated_at': datetime.now(timezone.utc).isoformat()
        }, f, indent=2)

    # Generate dates catalog
    dates_df = conn.execute("""
    SELECT DISTINCT track, frame, pass_direction, date
    FROM gslc_products
    ORDER BY track::INTEGER, frame::INTEGER, pass_direction, date
    """).fetchdf()

    dates = {}
    for _, row in dates_df.iterrows():
        track = row['track']
        frame = row['frame']
        pass_direction = row['pass_direction']
        date = row['date']

        key = f"T{track}_F{frame}_{pass_direction}"
        if key not in dates:
            dates[key] = []

        dates[key].append(date)

    # Save dates catalog
    with open(os.path.join(output_dir, 'gslc_dates.json'), 'w') as f:
        json.dump({
            'dates': dates,
            'generated_at': datetime.now(timezone.utc).isoformat()
        }, f, indent=2)

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
        track = row['track']
        frame = row['frame']
        pass_direction = row['pass_direction']
        date = row['date']
        polarization = row['polarization']

        scene_id = f"T{track}_F{frame}_{pass_direction}"
        scene_key = f"{scene_id}_{date}"

        if scene_key not in scenes:
            scenes[scene_key] = {
                'scene_id': scene_id,
                'track': track,
                'frame': frame,
                'pass_direction': pass_direction,
                'date': date,
                'polarizations': {},
                'granule_ids': []
            }

        scenes[scene_key]['polarizations'][polarization] = {
            'id': row['id'],
            'granule_id': row['granule_id'],
            'url': row['url'],
            's3_url': row['s3_url'],
            'browse_url': row['browse_url'],
            'metadata_url': row['metadata_url']
        }

        if row['granule_id'] not in scenes[scene_key]['granule_ids']:
            scenes[scene_key]['granule_ids'].append(row['granule_id'])

    # Save scenes catalog
    with open(os.path.join(output_dir, 'gslc_scenes.json'), 'w') as f:
        json.dump({
            'scenes': list(scenes.values()),
            'generated_at': datetime.now(timezone.utc).isoformat()
        }, f, indent=2)

    logger.info(f"Generated catalog JSON files in {output_dir}")


def main():
    """Main function to create GSLC catalog."""
    parser = argparse.ArgumentParser(description="Create a catalog of NISAR GSLC products")

    parser.add_argument("--db-path", default="gslc_catalog.duckdb",
                        help="Path to the DuckDB database file")
    parser.add_argument("--output-dir", default="catalog",
                        help="Directory to save JSON files")
    parser.add_argument("--max-results", type=int, default=25000,
                        help="Maximum number of results to return from CMR search")

    args = parser.parse_args()

    # Create or connect to the database
    conn = create_database(args.db_path)

    try:
        # Search for GSLC products
        results_df = search_gslc_products(max_results=args.max_results)

        if len(results_df) > 0:
            # Extract metadata
            metadata_df = extract_metadata(results_df)

            # Update database
            update_database(conn, metadata_df)

            # Generate catalog JSON files
            generate_catalog_json(conn, args.output_dir)
        else:
            logger.warning("No GSLC products found")

        return 0
    except Exception as e:
        logger.error(f"Error creating GSLC catalog: {e}", exc_info=True)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())