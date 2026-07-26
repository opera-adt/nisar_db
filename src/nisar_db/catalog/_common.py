"""Shared helpers for building NISAR product catalogs (GSLC / GUNW).

The GSLC and GUNW catalog builders share the same overall pipeline:
connect to DuckDB, extract metadata from CMR search results, upsert rows,
then emit JSON catalogs. Only the DuckDB schema, the filename dataclass, and
the product-specific JSON layouts differ; everything else lives here.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from ..io_json import write_catalog_json  # re-exported for catalog builders

if TYPE_CHECKING:
    import duckdb

# CMR link "rel" values → catalog URL columns.
_DATA_REL = "http://esipfed.org/ns/fedsearch/1.1/data#"
_BROWSE_REL = "http://esipfed.org/ns/fedsearch/1.1/browse#"
_METADATA_REL = "http://esipfed.org/ns/fedsearch/1.1/metadata#"


def create_database(db_path: str, table: str, schema: str) -> duckdb.DuckDBPyConnection:
    """Connect to a DuckDB database, creating ``table`` from ``schema`` if absent.

    Parameters
    ----------
    db_path : str
        Path to the DuckDB database file.
    table : str
        Name of the products table.
    schema : str
        Column definitions placed inside ``CREATE TABLE (...)``.

    """
    import duckdb  # lazy: only this helper needs the heavy dependency

    conn = duckdb.connect(db_path)
    conn.execute(f"CREATE TABLE IF NOT EXISTS {table} (\n{schema}\n)")
    return conn


def extract_urls(links) -> dict:
    """Map a granule's CMR links to url / s3_url / browse_url / metadata_url."""
    urls: dict = {}
    for link in links or []:
        rel = link.get("rel", "")
        href = link.get("href", "")
        if rel == _DATA_REL:
            urls["s3_url" if href.startswith("s3://") else "url"] = href
        elif rel == _BROWSE_REL:
            urls["browse_url"] = href
        elif rel == _METADATA_REL:
            urls["metadata_url"] = href
    return urls


def extract_metadata(
    df: pd.DataFrame, filename_cls, logger: logging.Logger
) -> pd.DataFrame:
    """Extract catalog metadata rows from CMR search results.

    Parameters
    ----------
    df : pd.DataFrame
        CMR search results with ``granule_id``, ``name`` and ``links`` columns.
    filename_cls : type
        ``GSLCFilename`` or ``GUNWFilename`` used to parse the granule name.
    logger : logging.Logger
        Logger for progress / parse warnings.

    """
    logger.info("Extracting metadata from search results...")

    rows = []
    for _, row in df.iterrows():
        name = row["name"]
        try:
            filename_obj = filename_cls.from_path(name)
        except Exception as e:
            logger.warning(f"Could not extract metadata from {name}: {e}")
            continue

        rows.append(
            {
                "id": name.replace(".h5", ""),  # filename without extension as ID
                "granule_id": row["granule_id"],
                **extract_urls(row.get("links", [])),
                **filename_obj.to_dataframe().iloc[0].to_dict(),
                "inserted_at": datetime.now(timezone.utc),
            }
        )

    metadata_df = pd.DataFrame(rows)
    logger.info(f"Extracted metadata from {len(metadata_df)} products")
    return metadata_df


def update_database(
    conn: duckdb.DuckDBPyConnection,
    metadata_df: pd.DataFrame,  # noqa: ARG001  # read by duckdb replacement scan
    table: str,
    logger: logging.Logger,
) -> None:
    """Insert/replace ``metadata_df`` rows into ``table`` and log the row count."""
    logger.info("Updating database with metadata...")
    # duckdb resolves ``metadata_df`` from this frame's locals via replacement scan.
    conn.execute(f"INSERT OR REPLACE INTO {table} SELECT * FROM metadata_df")
    conn.commit()

    count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    logger.info(f"Database now contains {count} products in {table}")


def generate_track_frame_json(
    conn: duckdb.DuckDBPyConnection,
    table: str,
    output_dir: str,
    prefix: str,
) -> None:
    """Write the shared ``<prefix>_tracks.json`` and ``<prefix>_frames.json``."""
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    tracks_df = conn.execute(f"""
    SELECT DISTINCT track, pass_direction
    FROM {table}
    ORDER BY track::INTEGER, pass_direction
    """).fetchdf()

    tracks: dict = {}
    for _, row in tracks_df.iterrows():
        tracks.setdefault(row["track"], []).append(row["pass_direction"])
    write_catalog_json(output_dir, f"{prefix}_tracks.json", "tracks", tracks)

    frames_df = conn.execute(f"""
    SELECT DISTINCT track, frame, pass_direction
    FROM {table}
    ORDER BY track::INTEGER, frame::INTEGER, pass_direction
    """).fetchdf()

    frames: dict = {}
    for _, row in frames_df.iterrows():
        frames.setdefault(f"T{row['track']}_{row['pass_direction']}", []).append(
            row["frame"]
        )
    write_catalog_json(output_dir, f"{prefix}_frames.json", "frames", frames)
