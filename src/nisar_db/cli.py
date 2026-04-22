"""Command-line interface for nisar_db tools."""

import click
import sys
from pathlib import Path

# Add the parent directory to sys.path so we can import from scripts
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Import the main functions from scripts
from create_frame_to_bound import main as create_frame_to_bound_main
from create_gslc_catalog import main as create_gslc_catalog_main
from create_consistent_gslc_catalog import main as create_consistent_gslc_catalog_main


@click.group()
def cli_app():
    """Create/interact with OPERA's NISAR frame database."""
    pass


@cli_app.command(name="create-frame-to-bound")
@click.option(
    "--nisar-gpkg",
    type=click.Path(exists=True, dir_okay=False),
    required=True,
    help="Path to the NISAR TrackFrame GeoPackage file.",
)
@click.option(
    "--output",
    type=click.Path(dir_okay=False),
    default="opera-nisar-disp-frame-to-bounds.json",
    help="Output JSON filename (will also create a .json.zip).",
)
def create_frame_to_bound_cmd(nisar_gpkg, output):
    """Create a frame-to-bound JSON file for NISAR frames.

    Extracts NISAR frames that intersect the OPERA North America polygon
    and writes their bounding boxes.
    """
    create_frame_to_bound_main(nisar_gpkg, output)


@cli_app.command(name="create-catalog")
@click.option(
    "--input",
    "input_file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Text file with one GSLC S3 path or granule ID per line.",
)
@click.option(
    "--output",
    type=click.Path(dir_okay=False),
    default=None,
    help="Output CSV path. Defaults to <input_stem>_catalog.csv.",
)
@click.option(
    "--na-only",
    is_flag=True,
    default=False,
    help="Filter to OPERA North America frames (requires --nisar-gpkg).",
)
@click.option(
    "--nisar-gpkg",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="NISAR TrackFrame GeoPackage, required when --na-only is set.",
)
def create_catalog_cmd(input_file, output, na_only, nisar_gpkg):
    """Generate a structured catalog from a list of NISAR GSLC files.

    Parses a file of GSLC paths/granule IDs into a structured DataFrame and CSV.
    """
    create_gslc_catalog_main(input_file, output, na_only, nisar_gpkg)


@cli_app.command(name="create-consistent")
@click.option(
    "--input",
    "input_file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Input GSLC catalog CSV from create-catalog.",
)
@click.option(
    "--output",
    type=click.Path(dir_okay=False),
    default=None,
    help="Output JSON path. Defaults to <input_stem>_consistent.json.",
)
@click.option(
    "--blackout-file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Blackout dates JSON file (optional).",
)
@click.option(
    "--min-dates",
    type=int,
    default=3,
    help="Minimum number of dates per frame to include in consistent set.",
)
def create_consistent_cmd(input_file, output, blackout_file, min_dates):
    """Create a consistent GSLC catalog for NISAR processing.

    Builds a consistent set of GSLC files that ensures complete coverage
    across all frames and dates.
    """
    create_consistent_gslc_catalog_main(input_file, output, blackout_file, min_dates)


if __name__ == "__main__":
    cli_app()