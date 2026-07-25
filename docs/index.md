# NISAR_DB

Frame-database and product-catalog generation for **OPERA DISP-NISAR**.

`nisar_db` builds the auxiliary databases that the DISP-NISAR processing system
needs to decide *which* NISAR GSLC acquisitions stack into a consistent displacement time
series, *where* on to process, and *when* to exclude or reset. It is the
NISAR counterpart of [`burst_db`](https://github.com/opera-adt/burst_db), which
does the same job for Sentinel-1 (DISP-S1).

The two projects deliberately mirror each other. Wherever a concept already
exists for Sentinel-1, `nisar_db` reproduces it with the same JSON shapes and the
same operational meaning, adapted to the fact that **NISAR is frame-based** (there
are no sub-frame burst IDs).

| Concept | Sentinel-1 (`burst_db`) | NISAR (`nisar_db`) |
|---|---|---|
| Smallest spatial unit | burst ID | frame |
| "Which acquisitions are safe to stack" | consistent **burst** database | consistent **mode** (GSLC) database |
| Seasonal exclusions | blackout dates JSON | blackout dates JSON |
| Reference-epoch resets | reference dates JSON | reference (reset) dates JSON |
| Batch labels | processing modes (historical/forward) | processing modes (historical/forward) |
| What invalidates a stack | frame's burst list changed | frame's winning mode/coverage changed |
| Geographic footprint | frame-to-burst / bounds | frame-to-bound |

## What lives here

- **[Background → Consistent mode](background/consistent-mode.md)** — the core
  selection logic. How, for every frame over North America, `nisar_db` picks a
  single dominant acquisition mode + coverage so the resulting stack is
  spatially and temporally consistent.
- **[Background → Blackout dates](background/blackout-dates.md)** — per-frame
  date ranges (e.g. seasonal snow) whose acquisitions are excluded from the
  consistent stack.
- **[Background → Reference (reset) dates](background/reference-dates.md)** —
  per-frame epochs at which the InSAR reference is reset, to avoid forming
  very long temporal-baseline interferograms.
- **[Background → Processing modes](background/processing-modes.md)** — which
  acquisitions form a complete batch the processor can run now (`historical`)
  versus one still accumulating (`forward`), and which frames changed
  definition since the last release.
- **[Tutorials → Build a consistent-mode database](tutorials/consistent-mode-database.md)**
  — a step-by-step recipe anyone can follow to produce the consistent-GSLC
  database from scratch.
- **[Tutorials → Derive blackout dates from snow cover](tutorials/snow-analysis.md)**
  — the upstream analysis: NOAA GEFS snow and temperature into per-frame
  blackout windows, and what those windows cost the stack.

## Install

=== "pip"

    ```bash
    pip install nisar-db
    ```

=== "conda"

    ```bash
    conda install -c conda-forge -c opera-adt nisar_db
    ```

Or from a checkout, for development:

=== "pixi"

    ```bash
    git clone https://github.com/opera-adt/nisar_db
    cd nisar_db
    pixi install
    pixi shell
    pixi run build
    ```

=== "conda env"

    ```bash
    git clone https://github.com/opera-adt/nisar_db
    cd nisar_db
    conda env create          # runtime dependencies only
    conda activate nisar-db-env
    python -m pip install .
    # contributors: add the tooling extras
    python -m pip install -e ".[dev]"
    ```

Installing the package creates the `nisar-db` command line tool:

```console
$ nisar-db --help
Usage: nisar-db [OPTIONS] COMMAND [ARGS]...

  Create/interact with OPERA's NISAR frame database.

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
  download-frame-db       Download the global NISAR TrackFrame database.
  label-processing-mode   Add historical/forward processing-mode labels.
  query-catalog           Query a catalog built by build-s3-catalog.
  search                  Search for NISAR products.
```

## Release assets

`make all` builds the full DISP-NISAR asset set, and pushing a `v*` tag attaches
it to a GitHub Release:

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

`burst_db`'s burst-level assets (`burst-id-geometries-simple`, `burst-to-frame`,
`frame-to-burst`) have no counterpart: NISAR frames are defined by the mission
rather than assembled from bursts, so `frame-to-bounds` carries everything the
processor needs to know about a frame.

## Serve these docs locally

```bash
pip install -e ".[docs]"
mkdocs serve
```

Then open <http://127.0.0.1:8000>.
