"""Command-line interface for nisar_db tools."""

from __future__ import annotations

from pathlib import Path

import click

from nisar_db.blackout import main as append_blackout_dates_cmd
from nisar_db.consistent_gslc import main as create_consistent_cmd
from nisar_db.frame_to_bound import main as create_frame_to_bound_cmd
from nisar_db.gslc_catalog import main as create_catalog_cmd
from nisar_db.processing_mode import main as label_processing_mode_cmd
from nisar_db.reference_dates import main as create_reference_dates_cmd
from nisar_db.search_nisar import main as search_nisar_main


@click.group()
def cli_app():
    """Create/interact with OPERA's NISAR frame database."""


# Each of these modules defines a fully-decorated click command as ``main``.
# Reuse them directly as subcommands rather than re-declaring their options.
cli_app.add_command(create_frame_to_bound_cmd, name="create-frame-to-bound")
cli_app.add_command(create_catalog_cmd, name="create-catalog")
cli_app.add_command(create_consistent_cmd, name="create-consistent")
cli_app.add_command(append_blackout_dates_cmd, name="append-blackout-dates")
cli_app.add_command(create_reference_dates_cmd, name="create-reference-dates")
cli_app.add_command(label_processing_mode_cmd, name="label-processing-mode")


def _run_argparse_main(module_name: str, prog: str, args: tuple[str, ...]) -> None:
    """Drive an argparse-based ``main()`` from a click passthrough command.

    The target module is imported lazily so heavy optional deps (duckdb,
    geopandas) are only required when the command actually runs, never for
    ``nisar-db --help``.
    """
    import importlib
    import sys

    module = importlib.import_module(module_name)
    sys.argv = [prog, *args]
    module.main()


_PASSTHROUGH = {
    "context_settings": {"ignore_unknown_options": True, "help_option_names": []}
}


@cli_app.command(name="create-nisar-catalog", **_PASSTHROUGH)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def create_nisar_catalog_cmd(args):
    """Build the GSLC/GUNW catalogs (DuckDB + JSON) from CMR.

    Passes all options through to nisar_db.nisar_catalog; run with --help
    for its full argument list.
    """
    _run_argparse_main("nisar_db.nisar_catalog", "create-nisar-catalog", args)


@cli_app.command(name="create-blackout-dates", **_PASSTHROUGH)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def create_blackout_dates_cmd(args):
    """Create the NISAR blackout-dates JSON.

    Passes all options through to nisar_db.catalog.create_blackout_dates;
    run with --help for its full argument list.
    """
    _run_argparse_main(
        "nisar_db.catalog.create_blackout_dates", "create-blackout-dates", args
    )


@cli_app.command(name="download", **_PASSTHROUGH)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def download_cmd(args):
    """Download NISAR granules/URLs from CMR.

    Passes all options through to nisar_db.download_cli; run with --help
    for its full argument list.
    """
    _run_argparse_main("nisar_db.download_cli", "download", args)


@cli_app.command(name="download-frame-db", context_settings={"show_default": True})
@click.option(
    "--output-dir",
    "-o",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path(),
    help="Directory to download the GeoPackage into.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Re-download even if the GeoPackage is already present.",
)
@click.option(
    "--granule-id",
    default=None,
    help="CMR concept id to fetch (default: the current TrackFrame database).",
)
def download_frame_db_cmd(output_dir: Path, force: bool, granule_id: str | None):
    """Download the global NISAR TrackFrame database GeoPackage.

    This is the --nisar-gpkg input to create-frame-to-bound, create-catalog and
    create-consistent. It is a public CMR granule; downloading needs Earthdata
    Login credentials in ~/.netrc.
    """
    from nisar_db.filenames import NISAR_DB_GRANULE_ID
    from nisar_db.geodb import get_trackframe_db

    path = get_trackframe_db(
        output_dir=output_dir,
        skip_existing=not force,
        granule_id=granule_id or NISAR_DB_GRANULE_ID,
    )
    click.echo(f"NISAR TrackFrame database: {path}")


@cli_app.command(name="search")
@click.option("--bbox", type=str, help="Bounding box as 'west,south,east,north'")
@click.option("--track", type=int, help="Track number")
@click.option("--frame", type=int, help="Frame number")
@click.option("--direction", type=str, help="Orbit direction (A/D)")
@click.option("--cycle", type=int, help="Cycle number")
@click.option(
    "--product-type",
    type=click.Choice(["GSLC", "GUNW"]),
    default="GSLC",
    help="Product type (default: GSLC)",
)
@click.option("--polarization", type=str, help="Polarization (e.g., HH)")
@click.option("--start-date", type=str, help="Start date (YYYY-MM-DD)")
@click.option("--end-date", type=str, help="End date (YYYY-MM-DD)")
@click.option(
    "--provider", type=str, default="ASF", help="Data provider (default: ASF)"
)
@click.option(
    "--max-results",
    type=int,
    default=100,
    help="Maximum results; 0 = the entire matching archive (default: 100)",
)
@click.option("--download", type=str, help="Download to this directory")
@click.option("--output-csv", type=str, help="Save results to CSV")
@click.option(
    "--s3-bucket",
    type=str,
    help="Search this S3 bucket instead of CMR (name or s3:// URI)",
)
@click.option(
    "--s3-prefix", type=str, help="Key prefix to scan, e.g. products/L2_L_GSLC/"
)
@click.option("--profile", type=str, help="AWS named profile for the S3 search")
@click.option(
    "--region", type=str, help="AWS region for the S3 search (default: us-west-2)"
)
@click.option(
    "--url-type",
    type=click.Choice(["https", "s3"]),
    default="https",
    help="URL type (default: https)",
)
def search_cmd(**kwargs):
    """Search for NISAR products.

    Search for NISAR GSLC and GUNW products in the CMR catalog.
    Results can be saved to CSV and/or downloaded.
    """
    import sys

    # ``search_nisar.main`` is argparse-based; rebuild argv to drive it.
    sys.argv = [sys.argv[0]]
    for key, value in kwargs.items():
        if value is not None:
            sys.argv.append(f"--{key.replace('_', '-')}")
            if not isinstance(value, bool):
                sys.argv.append(str(value))
    search_nisar_main()


@cli_app.command(name="build-s3-catalog")
@click.option("--bucket", required=True, help="S3 bucket name (or s3:// URI)")
@click.option("--prefix", default="", help="Key prefix, e.g. products/L2_L_GSLC/")
@click.option("--output", required=True, help="Catalog file (.parquet/.duckdb/.csv)")
@click.option("--profile", help="AWS named profile")
@click.option("--region", default="us-west-2", help="AWS region (default: us-west-2)")
@click.option(
    "--product-type",
    type=click.Choice(["GSLC", "GUNW"]),
    default="GSLC",
    help="Product type (default: GSLC)",
)
@click.option(
    "--max-workers", type=int, default=8, help="Parallel listing workers (default: 8)"
)
def build_s3_catalog_cmd(
    bucket, prefix, output, profile, region, product_type, max_workers
):
    """Scan an S3 bucket once and write a queryable NISAR product catalog.

    Enumerates every granule under the prefix (slow, one time) so later
    track/frame lookups via ``query-catalog`` are instant local filters.
    """
    from nisar_db.s3_catalog import build_s3_catalog

    out = build_s3_catalog(
        bucket,
        prefix,
        output,
        profile=profile,
        region=region,
        product_type=product_type,
        max_workers=max_workers,
    )
    click.echo(f"Catalog written to {out}")


@cli_app.command(name="query-catalog")
@click.argument("catalog", type=click.Path(exists=True))
@click.option("--track", type=int, help="Track number")
@click.option("--frame", type=int, help="Frame number")
@click.option("--direction", type=str, help="Orbit direction (A/D)")
@click.option("--cycle", type=int, help="Cycle number")
@click.option("--polarization", type=str, help="Polarization (e.g. DHDH)")
@click.option("--mode", type=str, help="Mode code (e.g. 4005)")
@click.option("--crid", type=str, help="Exact CRID / processing version")
@click.option("--crid-min", type=str, help="Keep crid >= this (latest-version filter)")
@click.option(
    "--product-type", type=click.Choice(["GSLC", "GUNW"]), help="Product type"
)
@click.option("--output-csv", type=str, help="Write matches to this CSV")
def query_catalog_cmd(catalog, output_csv, **filters):
    """Query a catalog built by ``build-s3-catalog`` (fast local filter)."""
    from nisar_db.s3_catalog import query_catalog

    df = query_catalog(catalog, **{k: v for k, v in filters.items() if v is not None})
    click.echo(f"{len(df)} matches")
    if len(df):
        cols = ["granule_id", "track", "frame", "direction", "mode", "crid", "size_gb"]
        click.echo(df[[c for c in cols if c in df.columns]].to_string(index=False))
    if output_csv:
        df.to_csv(output_csv, index=False)
        click.echo(f"Written to {output_csv}")


if __name__ == "__main__":
    cli_app()
