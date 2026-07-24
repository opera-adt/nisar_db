"""Command-line interface for nisar_db tools."""

import click

from nisar_db.consistent_gslc import main as create_consistent_cmd
from nisar_db.frame_to_bound import main as create_frame_to_bound_cmd
from nisar_db.gslc_catalog import main as create_catalog_cmd
from nisar_db.search_nisar import main as search_nisar_main


@click.group()
def cli_app():
    """Create/interact with OPERA's NISAR frame database."""
    pass


# Each of these modules defines a fully-decorated click command as ``main``.
# Reuse them directly as subcommands rather than re-declaring their options.
cli_app.add_command(create_frame_to_bound_cmd, name="create-frame-to-bound")
cli_app.add_command(create_catalog_cmd, name="create-catalog")
cli_app.add_command(create_consistent_cmd, name="create-consistent")


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
@click.option("--provider", type=str, default="ASF", help="Data provider (default: ASF)")
@click.option("--max-results", type=int, default=100, help="Maximum results (default: 100)")
@click.option("--download", type=str, help="Download to this directory")
@click.option("--output-csv", type=str, help="Save results to CSV")
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


if __name__ == "__main__":
    cli_app()
