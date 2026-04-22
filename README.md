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
  create-catalog          Generate a structured catalog from a list of NISAR GSLC files
  create-consistent       Create a consistent GSLC catalog for NISAR processing
  create-frame-to-bound   Create a frame-to-bound JSON file for NISAR frames
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

For operational processing, a consistent catalog of GSLC files is needed. The `nisar-db create-consistent` command builds this:

```bash
nisar-db create-consistent --input gslc_catalog.csv --output consistent_gslc_catalog.json
```

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
from nisar_db.download import download_earthdata_granule

# Parse a GSLC filename
gslc = GSLCFilename.from_path("path/to/NISAR_L2_PR_GSLC_123_456_D_789_4000_HH_20250101T120000_20250101T120100.h5")
print(gslc.date)       # Access date
print(gslc.scene_id)   # Get scene identifier (track/frame/direction)

# Download data from EarthData (requires .netrc credentials)
files = download_earthdata_granule("granule_id", output_dir="downloads")

# Work with geographic data
na_shape = get_opera_na_shape()  # Get the OPERA North America shape
```