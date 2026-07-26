#!/usr/bin/env python
"""Prepare the three artifacts the OPERA NISAR-DB viewer and release build consume.

Chains the existing ``nisar_db`` steps into one reproducible run:

1. ``opera-nisar-disp-frames.gpkg`` (+ ``opera-nisar-disp-frame-to-bounds.json``)
   -- North America frames carrying the ``frame_idx`` key, from the global NISAR
   TrackFrame GeoPackage.
2. ``gslc_catalog.duckdb`` -- one row per GSLC granule found in the S3 bucket,
   including a ``production_datetime`` column (the S3 object's ``LastModified``:
   granule names carry no production time, so for a forward-processing bucket the
   delivery time is the available proxy for when the product was produced).
3. ``opera-nisar-disp-consistent-gslc-<YYYYMMDD>.json`` (+ ``.json.zip``) -- the
   consistent-mode database, selected from the catalog with the frames from (1).

``gslc_catalog.csv`` is written alongside as the intermediate between (2) and (3).

The S3 scan in step 2 enumerates the whole prefix and takes minutes; pass
``--skip-catalog`` to reuse an existing DuckDB store when iterating on step 3.

Examples
--------
Full run into ``notebooks/``, fetching the TrackFrame database from CMR::

    python scripts/prepare_viewer_inputs.py --profile saml-pub

Same, reusing a TrackFrame GeoPackage already on disk::

    python scripts/prepare_viewer_inputs.py \\
        --nisar-gpkg notebooks/NISAR_TrackFrame_L_20250909.gpkg \\
        --profile saml-pub

Rebuild only the consistent-mode database from an existing catalog::

    python scripts/prepare_viewer_inputs.py \\
        --nisar-gpkg notebooks/NISAR_TrackFrame_L_20250909.gpkg \\
        --skip-frames --skip-catalog

Then render the viewer::

    python scripts/generate_scope_viewer.py \\
        --frames-gpkg notebooks/opera-nisar-disp-frames.gpkg \\
        --gslc-db notebooks/gslc_catalog.duckdb \\
        --consistent-json notebooks/opera-nisar-disp-consistent-gslc-20260724.json
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from nisar_db.consistent_gslc import make_consistent_gslc_json
from nisar_db.frame_to_bound import build_frame_to_bound
from nisar_db.geodb import get_trackframe_db
from nisar_db.gslc_catalog import parse_gslc_list, write_catalog_csv
from nisar_db.io_json import write_zipped_json
from nisar_db.logging_setup import configure_logging
from nisar_db.s3_catalog import build_s3_catalog

logger = configure_logging("prepare_viewer_inputs")

DEFAULT_BUCKET = "nisar-ops-rs-fwd"
DEFAULT_PREFIX = "products/L2_L_GSLC/"

FRAMES_GPKG = "opera-nisar-disp-frames.gpkg"
FRAME_TO_BOUNDS_JSON = "opera-nisar-disp-frame-to-bounds.json"
GSLC_DUCKDB = "gslc_catalog.duckdb"
GSLC_CSV = "gslc_catalog.csv"
GSLC_FILE_LIST = "gslc_files.txt"


def prepare_frames(nisar_gpkg: Path, outdir: Path) -> Path:
    """Filter the global TrackFrame GeoPackage to North America.

    Writes ``opera-nisar-disp-frame-to-bounds.json`` (+ ``.json.zip``) and the
    ``opera-nisar-disp-frames.gpkg`` side output that every later step keys on.

    Returns
    -------
    Path
        The written frames GeoPackage.

    """
    logger.info(f"Building frame-to-bound from {nisar_gpkg}")
    result, frames_gdf = build_frame_to_bound(nisar_gpkg_path=nisar_gpkg)

    json_path = outdir / FRAME_TO_BOUNDS_JSON
    zip_path = write_zipped_json(json_path, result)
    logger.info(f"Wrote {json_path} and {zip_path} ({len(result['data'])} frames)")

    # frame_idx is the JSON key; carrying it in the GPKG lets later steps join
    # granules back to the frame-to-bounds entries.
    frames_gdf = frames_gdf.copy()
    frames_gdf["frame_idx"] = frames_gdf.index
    gpkg_path = outdir / FRAMES_GPKG
    frames_gdf.to_file(gpkg_path, driver="GPKG")
    logger.info(f"Wrote {gpkg_path} ({len(frames_gdf)} frames)")

    return gpkg_path


def prepare_gslc_catalog(
    outdir: Path,
    bucket: str,
    prefix: str,
    profile: str | None,
    region: str,
    max_workers: int,
) -> Path:
    """Scan the GSLC S3 bucket into ``gslc_catalog.duckdb`` (table ``products``)."""
    db_path = outdir / GSLC_DUCKDB
    build_s3_catalog(
        bucket,
        prefix,
        db_path,
        profile=profile,
        region=region,
        product_type="GSLC",
        max_workers=max_workers,
    )
    return db_path


def catalog_db_to_csv(db_path: Path, outdir: Path) -> Path:
    """Parse the DuckDB granule list into the catalog CSV ``create-consistent`` reads.

    Reuses :func:`nisar_db.gslc_catalog.parse_gslc_list` on the granule S3 URLs so
    the CSV is byte-for-byte the shape ``nisar-db create-gslc-csv`` produces.
    """
    import duckdb

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        urls = con.execute("SELECT url FROM products ORDER BY url").fetchdf()["url"]
    finally:
        con.close()

    file_list = outdir / GSLC_FILE_LIST
    file_list.write_text("\n".join(urls) + "\n")
    logger.info(f"Wrote {file_list} ({len(urls):,} granules)")

    df, failed = parse_gslc_list(file_list)
    if failed:
        logger.warning(f"Could not parse {len(failed)} granule names, e.g. {failed[0]}")

    csv_path = outdir / GSLC_CSV
    write_catalog_csv(df, csv_path)
    n_pairs = df[["track", "frame"]].drop_duplicates().shape[0]
    logger.info(f"Wrote {csv_path} ({len(df):,} rows, {n_pairs:,} track/frame pairs)")
    return csv_path


def main(argv: list[str] | None = None) -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--nisar-gpkg",
        type=Path,
        default=None,
        help=(
            "Global NISAR TrackFrame GeoPackage (NISAR_TrackFrame_L_YYYYMMDD.gpkg). "
            "Omit to download it from CMR into --outdir."
        ),
    )
    parser.add_argument(
        "--outdir",
        type=Path,
        default=Path("notebooks"),
        help="Directory to write all artifacts into.",
    )
    parser.add_argument(
        "--date",
        default=datetime.now(timezone.utc).strftime("%Y%m%d"),
        help="Date stamp for the consistent-GSLC filename (YYYYMMDD).",
    )
    parser.add_argument("--bucket", default=DEFAULT_BUCKET, help="GSLC S3 bucket.")
    parser.add_argument("--prefix", default=DEFAULT_PREFIX, help="GSLC key prefix.")
    parser.add_argument("--profile", default=None, help="AWS named profile.")
    parser.add_argument("--region", default="us-west-2", help="AWS region.")
    parser.add_argument(
        "--max-workers", type=int, default=8, help="Parallel S3 listing workers."
    )
    parser.add_argument(
        "--blackout-file",
        type=Path,
        default=None,
        help="Optional blackout-dates JSON to drop excluded acquisitions.",
    )
    parser.add_argument(
        "--skip-frames",
        action="store_true",
        help=f"Reuse the existing {FRAMES_GPKG} instead of rebuilding it.",
    )
    parser.add_argument(
        "--skip-catalog",
        action="store_true",
        help=f"Reuse the existing {GSLC_DUCKDB} instead of rescanning S3.",
    )
    args = parser.parse_args(argv)

    args.outdir.mkdir(parents=True, exist_ok=True)

    frames_gpkg = args.outdir / FRAMES_GPKG
    if args.skip_frames:
        logger.info(f"Reusing {frames_gpkg}")
    else:
        nisar_gpkg = args.nisar_gpkg or get_trackframe_db(output_dir=args.outdir)
        frames_gpkg = prepare_frames(nisar_gpkg, args.outdir)

    db_path = args.outdir / GSLC_DUCKDB
    if args.skip_catalog:
        logger.info(f"Reusing {db_path}")
    else:
        db_path = prepare_gslc_catalog(
            args.outdir,
            args.bucket,
            args.prefix,
            args.profile,
            args.region,
            args.max_workers,
        )

    csv_path = catalog_db_to_csv(db_path, args.outdir)

    consistent_path = args.outdir / f"opera-nisar-disp-consistent-gslc-{args.date}.json"
    make_consistent_gslc_json(
        catalog_csv=csv_path,
        nisar_gpkg=frames_gpkg,
        output=consistent_path,
        blackout_file=args.blackout_file,
    )

    logger.info("Prepared:")
    for path in (frames_gpkg, db_path, csv_path, consistent_path):
        logger.info(f"  {path}")


if __name__ == "__main__":
    main()
