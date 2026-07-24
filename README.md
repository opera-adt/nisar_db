# NISAR_DB

Frame database generation for OPERA products from NISAR.

## Installation

### Using conda

Follow the steps below to install `nisar_db` using conda environment.

1. Download source code:

```bash
git clone https://github.com/opera-adt/nisar_db
cd nisar_db
```

2. Install dependencies:

```bash
conda env create
```

3. Install via pip:

```bash
# run "pip install -e" to install in development mode
python -m pip install .
```

### Using pixi

Alternatively, you can use [pixi](https://pixi.sh) for a faster installation process:

```bash
# Install pixi if you don't have it
curl -fsSL https://pixi.sh/install.sh | bash

# Clone the repository and install with pixi
git clone https://github.com/opera-adt/nisar_db
cd nisar_db

# Create environment and activate it
pixi install
pixi shell

# Install the package
pixi run build
```

## Usage

Installing the package creates the `nisar-db` command line tool:

```bash
$ nisar-db --help
Usage: nisar-db [OPTIONS] COMMAND [ARGS]...

  Create/interact with OPERA's NISAR frame database.

Options:
  --help  Show this message and exit.

Commands:
  create-blackout-dates  Create the NISAR blackout-dates JSON.
  create-catalog         Parse a NISAR GSLC file list into a structured CSV.
  create-consistent      Create the consistent-GSLC JSON for NISAR frames.
  create-frame-to-bound  Create a frame_to_bound JSON file for NISAR.
  create-nisar-catalog   Build the GSLC/GUNW catalogs (DuckDB + JSON) from CMR.
  download               Download NISAR granules/URLs from CMR.
  search                 Search for NISAR products.
```

## Creating GSLC Catalogs

The `nisar-db create-catalog` CLI will create a structured CSV catalog from a list of NISAR GSLC files.

```bash
nisar-db create-catalog --input nisar_gslc_files.txt --output gslc_catalog.csv
```

You can filter the catalog to include only frames in North America:

```bash
nisar-db create-catalog --input nisar_gslc_files.txt --na-only --nisar-gpkg NISAR_TrackFrame_L_YYYYMMDD.gpkg
```

## Creating Frame-to-Bound JSON Files

The `nisar-db create-frame-to-bound` command creates a JSON mapping of NISAR frames to their geographic boundaries:

```bash
nisar-db create-frame-to-bound --nisar-gpkg NISAR_TrackFrame_L_YYYYMMDD.gpkg --output nisar-frame-to-bounds.json
```

This generates both an uncompressed JSON file and a compressed .json.zip file for use in DISP-NISAR processing.

## Creating Consistent GSLC Catalogs

For operational processing, a consistent catalog of GSLC files is needed. The `nisar-db create-consistent` command builds this from the GSLC catalog CSV (`create-catalog`) and the filtered frames GeoPackage (`create-frame-to-bound` writes `opera-nisar-disp-frames.gpkg`):

```bash
nisar-db create-consistent \
  --catalog gslc_catalog.csv \
  --nisar-gpkg opera-nisar-disp-frames.gpkg \
  --output consistent_gslc_catalog.json
```

## Creating NISAR Product JSON Catalogs

The `nisar-db create-nisar-catalog` command generates comprehensive JSON catalogs of NISAR products (queried from CMR) for use in web applications. These catalogs are similar to the ones produced by burst_db.

```bash
# Create catalogs for all product types (default)
nisar-db create-nisar-catalog --output-dir catalog

# Create only GSLC catalog
nisar-db create-nisar-catalog --gslc --output-dir catalog

# Create only GUNW catalog
nisar-db create-nisar-catalog --gunw --output-dir catalog
```

The script generates the following JSON files:

- **GSLC products**:
  - `gslc_tracks.json`: List of all tracks and their pass directions
  - `gslc_frames.json`: List of all frames for each track
  - `gslc_dates.json`: List of all acquisition dates for each frame
  - `gslc_scenes.json`: Detailed information about each GSLC scene

- **GUNW products**:
  - `gunw_tracks.json`: List of all tracks and their pass directions
  - `gunw_frames.json`: List of all frames for each track
  - `gunw_pairs.json`: List of all interferogram pairs for each frame
  - `gunw_interferograms.json`: Detailed information about each interferogram

The script also creates DuckDB databases (`gslc_catalog.duckdb` and `gunw_catalog.duckdb`) that can be used for efficient querying of the catalog.

## Creating Blackout Dates for NISAR Frames

The `nisar-db create-blackout-dates` command generates blackout date information for NISAR frames. Blackout dates indicate periods when data should not be processed, typically due to environmental conditions like snow cover or extreme weather that affect SAR data quality.

```bash
# Create blackout dates from snow analysis data (default input format)
nisar-db create-blackout-dates --input-file snow_analysis.geojson --output-file nisar-blackout-dates.json

# Create blackout dates from monthly data
nisar-db create-blackout-dates --input-file monthly_data.geojson --monthly --output-file nisar-blackout-dates.json

# Create manual blackout dates for predefined frames
nisar-db create-blackout-dates --manual --output-file nisar-manual-blackout-dates.json
```

The command supports three methods for creating blackout dates:

1. **Snow Analysis Data** (default): Uses a GeoJSON or Parquet file with snow cover analysis data, containing aggressive, median, and conservative blackout periods for each frame.
   
   ```bash
   nisar-db create-blackout-dates --input-file snow_analysis.geojson --max-default-duration 180
   ```

2. **Monthly Data** (`--monthly`): Uses a GeoJSON file with year, month, frame_id, and to_process fields, where to_process=0 indicates a blackout month.
   
   ```bash
   nisar-db create-blackout-dates --input-file monthly_data.geojson --monthly
   ```

3. **Manual Definition** (`--manual`): Creates blackout dates from predefined periods without requiring an input file.
   
   ```bash
   nisar-db create-blackout-dates --manual --start-year 2025 --end-year 2030
   ```

The output is a JSON file with the following structure:

```json
{
  "metadata": {
    "generation_time": "2026-04-22T14:30:25.123456",
    "input_file": "snow_analysis.geojson",
    "output_file": "nisar-blackout-dates-2026-04-22.json"
  },
  "blackout_dates": {
    "1001": [
      [
        "2025-11-01T00:00:00",
        "2026-05-31T23:59:59"
      ],
      [
        "2026-11-01T00:00:00",
        "2027-05-31T23:59:59"
      ]
    ],
    "1002": [
      [
        "2025-11-15T00:00:00",
        "2026-04-30T23:59:59"
      ]
    ]
  }
}
```

### Automated Catalog Updates with GitHub Actions

A GitHub Actions workflow is included to automatically update the catalogs daily:

```yaml
name: Update NISAR Catalog

on:
  schedule:
    # Run every day at 00:00 UTC
    - cron: '0 0 * * *'
  workflow_dispatch:
    # Allow manual triggering
```

The workflow searches for new NISAR products, updates the catalogs, and commits the changes to the repository.

## Frame database information

The frame-to-bound mapping has the structure:

```python
{
    "data": {
        "1": {
            "epsg": 32631,
            "is_land": False,
            "is_north_america": False,
            "xmin": 500160,
            "ymin": 78240,
            "xmax": 789960,
            "ymax": 322740
        }, ...
    },
    "metadata": {
        "version": "0.1.0", "margin": 5000.0, ...
    }
}
```

where the keys of the `data` dict are the frame IDs.

## Creating a new release

After making changes to the code, a new release can be created by running the following commands:

```bash
# For example, if the new version is 0.2.0
git tag v0.2.0
pip install -e .

# Setup in a new folder
mkdir -p release_020 && cd release_020
make -f ../Makefile
```

If you're using pixi, you can run the make commands directly:

```bash
pixi run make -f Makefile VERSION=0.2.0
```

## Development with pixi

The pixi.toml file includes several useful tasks for development:

```bash
# Run linting on all files
pixi run lint

# Run tests
pixi run tests

# Type check with mypy
pixi run check

# Run main tools
pixi run create-frame-to-bound --nisar-gpkg NISAR_TrackFrame_L_YYYYMMDD.gpkg --output nisar-frame-to-bounds.json
pixi run create-catalog --input nisar_gslc_files.txt --output gslc_catalog.csv
```

## Using Python API

You can use the Python API directly:

```python
from nisar_db.filenames import GSLCFilename, GUNWFilename
from nisar_db.geodb import convert_to_gdf, get_opera_na_shape
from nisar_db.download_extended import download_earthdata_granule, download_from_url
from nisar_db.catalog.create_gslc_catalog import search_gslc_products
from nisar_db.catalog.create_blackout_dates import manual_blackout_dates

# Parse a GSLC filename
gslc = GSLCFilename.from_path("path/to/NISAR_L2_GSLC_BETA_V1_123_456_D_789_4000_HH_20250101T120000_20250101T120100.h5")
print(gslc.date)       # Access date
print(gslc.scene_id)   # Get scene identifier (track/frame/direction)

# Download data from EarthData (requires .netrc credentials)
files = download_earthdata_granule("granule_id", output_dir="downloads")

# Search for GSLC products
gslc_products = search_gslc_products(max_results=1000)

# Create manual blackout dates
blackout_data = manual_blackout_dates(output_file="blackout_dates.json")

# Work with geographic data
na_shape = get_opera_na_shape()  # Get the OPERA North America shape
```