---
name: nisar-catalog
description: >
  Understand and work on the nisar_db catalog pipeline — building, updating, and
  explaining the NISAR GSLC/GUNW catalogs, frame-to-bound maps, consistent-GSLC
  lists, and blackout dates. Use when the user asks to update a catalog, regenerate
  JSON/DuckDB outputs, debug the CMR search, extend the schema, or explain the
  inputs/outputs/logic of any `nisar-db` command.
---

# nisar_db catalog pipeline

`nisar_db` builds OPERA's NISAR frame database and product catalogs. Everything is
exposed through the `nisar-db` CLI (defined in [cli.py](../../src/nisar_db/cli.py)).
Installing the package (`pip install -e .` or `pixi run build`) creates that command.

```
nisar-db create-catalog          Parse a NISAR GSLC file list into a structured CSV.
nisar-db create-frame-to-bound   Create a frame_to_bound JSON file for NISAR.
nisar-db create-consistent       Create the consistent-GSLC JSON for NISAR frames.
nisar-db create-nisar-catalog    Build the GSLC/GUNW catalogs (DuckDB + JSON) from CMR.
nisar-db create-blackout-dates   Create the NISAR blackout-dates JSON.
nisar-db append-blackout-dates   Manually append blackout windows for one frame.
nisar-db download                Download NISAR granules/URLs from CMR.
nisar-db search                  Search for NISAR products.
```

## How the CLI is wired (important before editing)

`cli_app` in [cli.py](../../src/nisar_db/cli.py) mixes two styles:

- **Native click commands** reused directly as subcommands: `create-frame-to-bound`,
  `create-catalog`, `create-consistent`, `append-blackout-dates`, and `search`. Their options are defined on
  each module's `main`.
- **Argparse pass-through commands**: `create-nisar-catalog`, `create-blackout-dates`,
  `download`. These forward all args to an argparse `main()` via `_run_argparse_main`,
  which rewrites `sys.argv` and calls the module. To see their real options, run the
  command with `--help` or read the target module's `argparse` block — **not** the
  click wrapper. Heavy deps (duckdb, geopandas) are imported lazily so `nisar-db
  --help` stays fast; keep that property when editing.

## The main catalog pipeline (`create-nisar-catalog`)

This is the command the daily GitHub Action runs and what "update the catalog"
usually means. Entry point: [nisar_catalog.py](../../src/nisar_db/nisar_catalog.py),
which just dispatches to the GSLC and/or GUNW builders.

Both builders share one pipeline in
[catalog/_common.py](../../src/nisar_db/catalog/_common.py):

1. **Search CMR** — `search_nisar_granules(...)` in
   [search_nisar.py](../../src/nisar_db/search_nisar.py) queries
   `https://cmr.earthdata.nasa.gov/search/granules.umm_json` for the collection
   short name (`NISAR_L2_GSLC_BETA_V1` / `NISAR_L2_GUNW_BETA_V1`), provider `ASF`,
   paging at `page_size=2000` up to `--max-results` (default 25000).
2. **Extract metadata** — `extract_metadata` parses each granule `title` with a
   filename dataclass (`GSLCFilename` / `GUNWFilename` in
   [filenames.py](../../src/nisar_db/filenames.py)) and pulls URLs from CMR links
   (`extract_urls`: data→url/s3_url, browse→browse_url, metadata→metadata_url). A
   title that fails to parse is logged and skipped, not fatal.
3. **Upsert into DuckDB** — `update_database` does `INSERT OR REPLACE INTO <table>`
   so re-runs are idempotent; `id` (filename without `.h5`) is the primary key.
   Tables: `gslc_products` / `gunw_products`; per-product `_SCHEMA` lives at the top
   of each builder module.
4. **Emit JSON** — `generate_catalog_json` writes the burst_db-style JSON files
   below; each file is `{<key>: ..., "generated_at": <utc iso>}`.

### Inputs / outputs at a glance

| Command | Input | Output |
|---|---|---|
| `create-nisar-catalog` | CMR (network); `--output-dir`, `--gslc-db`, `--gunw-db`, `--max-results`, `--gslc`/`--gunw`/`--all` | `<db>.duckdb` + JSON files in `--output-dir` |
| `create-catalog` | `--input` GSLC file list `.txt`; optional `--na-only`, `--nisar-gpkg` | `--output` CSV |
| `create-frame-to-bound` | `--nisar-gpkg` (NISAR_TrackFrame_L_*.gpkg) | `--output` JSON + `.json.zip`; `opera-nisar-disp-frames.gpkg` |
| `create-consistent` | `--catalog` CSV + `--nisar-gpkg` filtered frames | `--output` consistent JSON |
| `create-blackout-dates` | `--input-file` snow/monthly GeoJSON, or `--manual` | `--output-file` blackout JSON |
| `append-blackout-dates` | `--json-file` existing blackout JSON, `--frame`, repeatable `--period START END` | same file (or `--output`) + `.json.zip` |

### GSLC JSON outputs
- `gslc_tracks.json` — track → list of pass directions
- `gslc_frames.json` — `T{track}_{dir}` → list of frames
- `gslc_dates.json` — `T{track}_F{frame}_{dir}` → list of dates
- `gslc_scenes.json` — per scene: id, track/frame/direction, date, per-polarization
  URLs, granule_ids

### GUNW JSON outputs
- `gunw_tracks.json`, `gunw_frames.json` — same shape as GSLC
- `gunw_pairs.json` — `T{track}_F{frame}_{dir}` → list of `{ref_date, sec_date, pair}`
- `gunw_interferograms.json` — per ifg: scene_id, ref/sec date, per-polarization URLs

## Running / updating the catalog

```bash
# Full refresh (both products), what the daily Action does:
nisar-db create-nisar-catalog --output-dir catalog

# One product only:
nisar-db create-nisar-catalog --gslc --output-dir catalog
nisar-db create-nisar-catalog --gunw --output-dir catalog

# Under pixi:
pixi run create-nisar-catalog --output-dir catalog
```

The DuckDB files persist between runs and accumulate history (upsert); the JSON files
are regenerated fresh each run from the full table. To rebuild from scratch, delete
the `.duckdb` file first.

### Automated updates
[.github/workflows/update_nisar_catalog.yml](../../.github/workflows/update_nisar_catalog.yml)
runs daily at 00:00 UTC (and on `workflow_dispatch`). It calls the two builder
modules directly (`python -m nisar_db.catalog.create_gslc_catalog ...` /
`create_gunw_catalog`), then commits changed `catalog/*.json`. Note it invokes the
modules directly rather than the `create-nisar-catalog` wrapper — keep both working
when changing entry points.

## Where to make common changes

- **Add/rename a catalog field** → edit the `_SCHEMA` string and the JSON-building
  SQL in [create_gslc_catalog.py](../../src/nisar_db/catalog/create_gslc_catalog.py)
  / [create_gunw_catalog.py](../../src/nisar_db/catalog/create_gunw_catalog.py),
  and the parsing in the matching dataclass in
  [filenames.py](../../src/nisar_db/filenames.py). The DuckDB column order must
  match the `metadata_df` column order because upsert uses `SELECT *`.
- **Change CMR collection / provider / paging** → `NISARCollection` constants in
  [filenames.py](../../src/nisar_db/filenames.py) and the query in
  [search_nisar.py](../../src/nisar_db/search_nisar.py).
- **Change JSON grouping/shape** → `generate_catalog_json` in each builder and the
  shared `generate_track_frame_json` / `write_catalog_json` in
  [catalog/_common.py](../../src/nisar_db/catalog/_common.py).
- **Blackout logic** → [catalog/create_blackout_dates.py](../../src/nisar_db/catalog/create_blackout_dates.py)
  (three modes: snow-analysis default, `--monthly`, `--manual`).

## Gotchas

- CMR search hits the network and can be slow / rate-limited; `search_nisar` paces
  parallel workers deliberately. A run finding 0 products logs a warning and writes
  nothing — check the collection short name before assuming a code bug.
- `track`/`frame` are stored as VARCHAR; SQL sorts cast with `::INTEGER`. Keep that
  when adding ordered queries or numeric sorting breaks.
- Filename parsing tolerates 13–18 underscore fields (trailing CRID/orbits/coverage/
  location/version optional); a NISAR filename-format change shows up as skipped
  titles in the logs, not an error.

## Dev commands
```bash
pixi run lint     # ruff/pre-commit on all files
pixi run tests    # pytest
pixi run check    # mypy
```
