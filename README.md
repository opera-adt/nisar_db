# NISAR_DB

Frame database generation for OPERA products from NISAR.

## Documentation

Full documentation is published at **<https://opera-adt.github.io/nisar_db/>**, including:

- [Background: how consistent mode works](https://opera-adt.github.io/nisar_db/background/consistent-mode.html)
- [Background: blackout dates](https://opera-adt.github.io/nisar_db/background/blackout-dates.html)
- [Background: reference (reset) dates](https://opera-adt.github.io/nisar_db/background/reference-dates.html)
- [Tutorial: build a consistent-mode database](https://opera-adt.github.io/nisar_db/tutorials/consistent-mode-database.html)
- [Interactive NISAR frame viewer](https://opera-adt.github.io/nisar_db/frame-viewer.html)

To build and preview the docs locally:

```bash
pip install -e ".[docs]"
mkdocs serve   # then open http://127.0.0.1:8000
```

## Installation

### From PyPI

```bash
pip install nisar-db
```

### From conda

```bash
conda install -c conda-forge -c opera-adt nisar_db
```

`conda-forge` is needed for the dependencies; the `opera-adt` channel hosts the
`nisar_db` package itself.

### From source (conda environment)

Follow the steps below to install `nisar_db` from a checkout.

1. Download source code:

```bash
git clone https://github.com/opera-adt/nisar_db
cd nisar_db
```

2. Install dependencies:

```bash
conda env create   # runtime dependencies only
conda activate nisar-db-env
```

3. Install via pip:

```bash
# run "pip install -e" to install in development mode
python -m pip install .
```

The `environment.yml` deliberately carries only the runtime dependencies. The
development tooling lives in the pyproject extras, so contributors add:

```bash
python -m pip install -e ".[dev]"   # test + docs + lint + publish tooling
```

Individual extras are also available: `[test]`, `[docs]`, `[lint]`, `[publish]`.
Building the conda package additionally needs the conda-only tools:
`conda install -c conda-forge conda-build anaconda-client`.

### From source (pixi)

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
  append-blackout-dates   Manually append blackout windows for a single frame.
  build-s3-catalog        Scan an S3 bucket once and write a queryable catalog.
  create-blackout-dates   Create the NISAR blackout-dates JSON.
  create-catalog          Parse a NISAR GSLC file list into a structured CSV.
  create-consistent       Create the consistent-GSLC JSON for NISAR frames.
  create-frame-to-bound   Create a frame_to_bound JSON file for NISAR.
  create-nisar-catalog    Build the GSLC/GUNW catalogs (DuckDB + JSON) from CMR.
  create-reference-dates  Create the NISAR reference-dates JSON.
  download                Download NISAR granules/URLs from CMR.
  label-processing-mode   Add historical/forward processing-mode labels.
  query-catalog           Query a catalog built by build-s3-catalog.
  search                  Search for NISAR products.
```

## Release assets

`make all` builds the full set of DISP-NISAR assets, the frame-based
counterpart of the DISP-S1 assets released by
[burst_db](https://github.com/opera-adt/burst_db). The same set is attached to
a GitHub Release by `.github/workflows/release.yml` when a `v*` tag is pushed.

| Asset | Built by |
| --- | --- |
| `opera-nisar-disp-frame-to-bounds-{date}.json[.zip]` | `create-frame-to-bound` |
| `opera-nisar-disp-frame-geometries-simple-{version}.geojson.zip` | `create-frame-to-bound --geojson` |
| `opera-nisar-disp-blackout-dates-{date}.json[.zip]` | `create-blackout-dates` |
| `opera-nisar-disp-consistent-gslc-{date}.json[.zip]` | `create-consistent --blackout-file` |
| `opera-nisar-disp-consistent-gslc-no-blackout.json[.zip]` | `create-consistent` |
| `opera-nisar-disp-consistent-gslc-with-processing-mode-{date}.json[.zip]` | `label-processing-mode` |
| `opera-nisar-disp-reference-dates-{date}.json[.zip]` | `create-reference-dates` |
| `gslc_catalog.csv` | `create-catalog` |

burst_db's burst-level assets (`burst-id-geometries-simple`, `burst-to-frame`,
`frame-to-burst`) have no counterpart here: NISAR frames are defined by the
mission, not assembled from bursts, so `frame-to-bounds` carries what the
processor needs to know about a frame.

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

This generates both an uncompressed JSON file and a compressed .json.zip file for use in DISP-NISAR processing, plus `opera-nisar-disp-frames.gpkg` holding the full-resolution frame polygons.

Add `--geojson` to also write the polygons as a simplified, zipped GeoJSON -- the lightweight version for web maps and quick spatial joins, matching burst_db's `frame-geometries-simple` asset:

```bash
nisar-db create-frame-to-bound \
  --nisar-gpkg NISAR_TrackFrame_L_YYYYMMDD.gpkg \
  --output opera-nisar-disp-frame-to-bounds.json \
  --geojson opera-nisar-disp-frame-geometries-simple.geojson
```

`--simplify-tolerance` (default `0.1` degrees) controls how aggressively the polygons are thinned.

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

### Appending blackout dates for a single frame

The commands above regenerate a whole file. To add a one-off `[start, end]` window to an existing blackout JSON, use `nisar-db append-blackout-dates`:

```bash
nisar-db append-blackout-dates \
  --json-file nisar-blackout-dates.json \
  --frame 5827 \
  --period 2025-11-01 2026-05-31 \
  --period 2026-11-01 2027-05-31
```

`--period` is repeatable and takes an inclusive `START END` pair; bare dates are normalized to `T00:00:00` / `T23:59:59` so the end day is fully covered. The frame entry is created if it is missing, windows stay sorted by start date, and identical windows are not duplicated. The file is edited in place (`--output` writes elsewhere), the `.json.zip` sidecar is refreshed (`--no-zip` skips it), and every edit is recorded under `metadata.manual_edits`. Pass `--create` to start a new blackout JSON.

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

## Creating Reference Dates

The `nisar-db create-reference-dates` command decides when each frame's InSAR
reference epoch resets. Two strategies are available:

```bash
# Month-based: anchor each frame on a snow-free month picked from its blackouts
nisar-db create-reference-dates \
  --consistent-json opera-nisar-disp-consistent-gslc-2026-07-25.json \
  --blackout-file opera-nisar-disp-blackout-dates-2026-07-25.json \
  --output opera-nisar-disp-reference-dates-2026-07-25.json

# Interval-based: open a new epoch roughly yearly, once enough data has piled up
nisar-db create-reference-dates \
  --consistent-json opera-nisar-disp-consistent-gslc-2026-07-25.json \
  --interval 1.0 --min-acquisitions 15
```

With `--blackout-file`, a frame with no recorded snow anchors on November, one
with a moderate snow season on September, and a heavily snow-bound frame on
July. Without it, epochs follow each frame's actual acquisition history: a new
one opens after `--interval` years, but only once `--min-acquisitions`
acquisitions have accumulated, so sparse frames keep a single reference.

## Labeling Processing Modes

The `nisar-db label-processing-mode` command annotates a consistent-GSLC JSON
with which acquisitions form complete processing batches:

```bash
nisar-db label-processing-mode \
  --consistent-json opera-nisar-disp-consistent-gslc-2026-07-25.json \
  --previous-json opera-nisar-disp-consistent-gslc-2026-04-25.json \
  --output opera-nisar-disp-consistent-gslc-with-processing-mode-2026-07-25.json
```

Each `sensing_time_list` turns from a list into a `{sensing_time: label}` map:

- `historical_NN` -- part of a full `--batch-size` batch, processable now.
- `forward_NN` -- the trailing partial batch, reprocessed as new data arrives.
- `no_run` -- the frame has never accumulated a full batch.

`NN` increments after any gap longer than `--gap-threshold-years`, since a
stack cannot span it. Passing `--previous-json` also records, under
`metadata.frames_with_changed_mode`, the frames whose winning
`(common_mode, common_coverage)` flipped since the last release -- those cannot
reuse their existing stack.

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

## Publishing the package

Pushing a `v*` tag runs two workflows: `release.yml` (builds the Path-A data
assets and the GitHub Release) and `publish.yml` (builds the wheel/sdist and the
conda package, then pushes them to PyPI and anaconda.org).

```bash
git tag v0.2.0 && git push origin v0.2.0
```

`publish.yml` can also be run manually from the Actions tab to publish to
TestPyPI first (`pypi_target: testpypi`), which is the recommended dry run
before a real release. The conda package is always built and attached to the run
as an artifact, and only uploaded when a token is configured.

Repository secrets used by `publish.yml`:

| Secret | Purpose |
| --- | --- |
| `PYPI_API_TOKEN` | PyPI upload. Omit it to use [trusted publishing](https://docs.pypi.org/trusted-publishers/) via the `pypi` GitHub environment. |
| `TEST_PYPI_API_TOKEN` | Same, for TestPyPI. |
| `ANACONDA_API_TOKEN` | anaconda.org upload for the conda package. |

Everything can also be done locally from the same recipe/config the workflow
uses:

```bash
make dist                 # sdist + wheel into dist/, checked with twine
make publish-testpypi     # or: make publish-pypi
make conda-build          # noarch package into build-conda/
make publish-conda CONDA_CHANNEL=opera-adt
```

The conda recipe lives in [conda/meta.yaml](conda/meta.yaml). It builds from the
working tree and takes its version from `NISAR_DB_VERSION` (the `Makefile` sets
this from the latest git tag), since `setuptools_scm` cannot see the git history
inside the conda build sandbox.

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
from nisar_db.download import download_earthdata_granule, download_from_url
from nisar_db.catalog.create_gslc_catalog import search_gslc_products
from nisar_db.catalog.create_blackout_dates import manual_blackout_dates
from nisar_db.blackout import append_blackout_dates_json

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

# Append one more window for a single frame
append_blackout_dates_json("blackout_dates.json", 5827, [("2025-11-01", "2026-05-31")])

# Work with geographic data
na_shape = get_opera_na_shape()  # Get the OPERA North America shape
```
