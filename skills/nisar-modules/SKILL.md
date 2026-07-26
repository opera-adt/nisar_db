---
name: nisar-modules
description: >
  Map / index of every module in the nisar_db package — the file tree plus each
  module's purpose and key public symbols. Use to quickly find where a function,
  class, or feature lives (search, S3 catalog, filename parsing, geometry, JSON
  writers, CLI) before reading or editing code. Complements the nisar-catalog
  skill, which explains the catalog pipeline in depth.
---

# nisar_db module map

Package root: [src/nisar_db/](../../src/nisar_db/). Import surface is re-exported
from [__init__.py](../../src/nisar_db/__init__.py). The CLI (`nisar-db`) is wired
in [cli.py](../../src/nisar_db/cli.py). For the catalog pipeline in depth, see the
**nisar-catalog** skill.

## Tree

```
src/nisar_db/
├── __init__.py            # public API re-exports (see below)
├── cli.py                 # `nisar-db` click group: all subcommands
├── search/                # NISAR product search (CMR + S3 backends)
│   ├── models.py          #   NISARProduct, ProductType, UrlType
│   ├── cmr.py             #   CMR search backends
│   ├── s3.py              #   S3-bucket search backend
│   ├── frames.py          #   products -> DataFrame, downloads
│   └── cli.py             #   argparse `main()` for `nisar-db search`
├── search_nisar.py        # BACK-COMPAT FACADE -> re-exports nisar_db.search.*
├── s3_catalog.py          # scan-once/query-many S3 catalog (+ geometry join)
├── catalog/               # CMR -> DuckDB + JSON catalog builders
│   ├── _common.py         #   shared DuckDB/JSON helpers
│   ├── create_gslc_catalog.py
│   ├── create_gunw_catalog.py
│   └── create_blackout_dates.py
├── nisar_catalog.py       # dispatcher for GSLC/GUNW builders (create-nisar-catalog)
├── gslc_catalog.py        # parse a GSLC file list -> structured CSV (create-catalog)
├── consistent_gslc.py     # consistent-GSLC-per-frame JSON (create-consistent)
├── blackout.py            # per-frame blackout / reference-date filtering + writers
├── reference_dates.py     # derive reset epochs (create-reference-dates)
├── processing_mode.py     # historical/forward labels (label-processing-mode)
├── frame_to_bound.py      # NISAR frame -> bbox JSON + simplified GeoJSON
├── filenames.py           # GSLCFilename / GUNWFilename parsers + NISARCollection
├── geodb.py               # TrackFrame gpkg fetch + OPERA NA polygon + geometry joins
├── modes.py               # acquisition-mode constants + dominant_value vote
├── io_json.py             # write_zipped_json / write_catalog_json
├── logging_setup.py       # configure_logging for CLI entry points
├── download.py            # EarthData / URL / S3 file downloaders
├── download_cli.py        # argparse `main()` for `nisar-db download`
├── parser.py              # s3 path list -> DataFrame (GSLCFilename/GUNWFilename)
└── utils.py               # parts_str + datetime format constants
```

## Top-level import surface (`from nisar_db import ...`)

| Symbol | Defined in |
|---|---|
| `NISARProduct`, `ProductType`, `UrlType` | [search/models.py](../../src/nisar_db/search/models.py) |
| `search_nisar_products` | [search/cmr.py](../../src/nisar_db/search/cmr.py) |
| `search_s3_products`, `parse_s3_uri` | [search/s3.py](../../src/nisar_db/search/s3.py) |
| `products_to_dataframe`, `download_products` | [search/frames.py](../../src/nisar_db/search/frames.py) |
| `build_s3_catalog`, `query_catalog`, `products_to_catalog_df`, `catalog_to_gdf` | [s3_catalog.py](../../src/nisar_db/s3_catalog.py) |

> `search_nisar.py` is a **facade**: `from nisar_db.search_nisar import X` still works,
> but the implementation lives in `nisar_db.search.*`. Edit the subpackage module,
> not the facade.

## Module index (key public symbols)

### Search
- **[search/models.py](../../src/nisar_db/search/models.py)** — `ProductType`,
  `UrlType`, and `NISARProduct` (the product model). Build via
  `NISARProduct.from_cmr_item(item, url_type)` or `from_s3_key(bucket, key, size=)`.
  Metadata extractors (static, take a UMM dict): `extract_urls_from_metadata`,
  `extract_temporal_from_metadata`, `extract_bbox_from_metadata`,
  `extract_attributes_from_metadata` (track/frame/direction/cycle/pol/**full_frame**/
  **joint_observation**/**crid**); instance wrappers `extract_urls`, `extract_fields`.
- **[search/cmr.py](../../src/nisar_db/search/cmr.py)** — `search_nisar_products(...)`
  (indexed track/frame search, spans BETA+PROVISIONAL collections),
  `search_nisar_granules(...)` (raw UMM DataFrame for the catalog builders),
  `fetch_cmr_pages(...)`.
- **[search/s3.py](../../src/nisar_db/search/s3.py)** — `search_s3_products(...)`
  (boto3 + profile, parallel prefix fan-out, filename-parse + filter), `parse_s3_uri`.
- **[search/frames.py](../../src/nisar_db/search/frames.py)** —
  `products_to_dataframe`, `download_products`.
- **[search/cli.py](../../src/nisar_db/search/cli.py)** — argparse `main()` behind
  `nisar-db search` (CMR by default; `--s3-bucket` switches to the S3 backend).

### S3 catalog (scan-once / query-many)
- **[s3_catalog.py](../../src/nisar_db/s3_catalog.py)** — `build_s3_catalog(...)`
  (enumerate a bucket once → `.parquet`/`.duckdb`/`.csv`), `query_catalog(...)`
  (fast local filter, incl. `crid_min` latest-version filter),
  `products_to_catalog_df`, `catalog_to_gdf(catalog, nisar_frames, add_na_flag=)`
  (join to frame geometry + `is_north_america`). CLI: `build-s3-catalog`,
  `query-catalog`.

### CMR → DuckDB + JSON catalog builders
- **[nisar_catalog.py](../../src/nisar_db/nisar_catalog.py)** — dispatcher
  (`create-nisar-catalog`).
- **[catalog/_common.py](../../src/nisar_db/catalog/_common.py)** —
  `create_database`, `extract_urls`, `extract_metadata`, `update_database`,
  `generate_track_frame_json`.
- **[catalog/create_gslc_catalog.py](../../src/nisar_db/catalog/create_gslc_catalog.py)**
  / **[create_gunw_catalog.py](../../src/nisar_db/catalog/create_gunw_catalog.py)** —
  `search_*_products`, `generate_catalog_json`, `main`; `_SCHEMA` at top of file.
- **[catalog/create_blackout_dates.py](../../src/nisar_db/catalog/create_blackout_dates.py)**
  — snow / monthly / manual blackout JSON.

### Frame products / geometry
- **[gslc_catalog.py](../../src/nisar_db/gslc_catalog.py)** — `parse_gslc_list`
  (GSLC file list → CSV w/ mode_family/common_mode/is_full), `filter_north_america`.
- **[consistent_gslc.py](../../src/nisar_db/consistent_gslc.py)** —
  `select_consistent_acquisitions`, `build_frame_idx_map`, `make_consistent_gslc_json`.
- **[blackout.py](../../src/nisar_db/blackout.py)** — `is_excluded`, `apply_blackout`,
  `create_blackout_dates_json`, `create_reference_dates_json`;
  manual editing via `append_blackout_period`, `append_blackout_dates_json`,
  `load_blackout_json` (CLI `append-blackout-dates`).
- **[reference_dates.py](../../src/nisar_db/reference_dates.py)** —
  `calculate_reference_dates` (interval-based, or month-based when a blackout
  file supplies the anchor months; both need the consistent JSON and emit only
  dates present in it), `find_blacked_out_references` (guard against an
  unfiltered stack), `build_desired_month_map_from_blackout`,
  `pick_month_based_on_snow`, `load_consistent_json`, `EVENT_DATES_BY_FRAME`
  (CLI `create-reference-dates`). Writes via `blackout.create_reference_dates_json`.
- **[processing_mode.py](../../src/nisar_db/processing_mode.py)** —
  `assign_processing_modes` (historical/forward/no_run per sensing time),
  `add_processing_modes`, `identify_time_groups`, `get_processing_mode_summary`,
  `find_frames_with_changed_mode` (release-to-release reconciliation)
  (CLI `label-processing-mode`).
- **[frame_to_bound.py](../../src/nisar_db/frame_to_bound.py)** —
  `build_frame_to_bound`, `write_frame_geometries_geojson` (simplified, zipped
  frame polygons behind `--geojson`).
- **[geodb.py](../../src/nisar_db/geodb.py)** — `get_trackframe_db` /
  `load_trackframe_db` (fetch the global TrackFrame GeoPackage from CMR),
  `get_opera_na_shape` (OPERA NA polygon), `filter_frames_to_na`,
  `convert_to_gdf`.
- **[modes.py](../../src/nisar_db/modes.py)** — `dominant_value` + mode-family constants.

### Filenames / IO / util / downloads
- **[filenames.py](../../src/nisar_db/filenames.py)** — `GSLCFilename` / `GUNWFilename`
  (`.from_path`, `.track`, `.frame`, `.date`, `.scene_id`, `.to_dataframe`),
  `NISARCollection` (collection short names, `GSLC_SHORT_NAMES`/`GUNW_SHORT_NAMES`).
- **[io_json.py](../../src/nisar_db/io_json.py)** — `write_zipped_json`,
  `write_catalog_json`.
- **[logging_setup.py](../../src/nisar_db/logging_setup.py)** — `configure_logging`.
- **[utils.py](../../src/nisar_db/utils.py)** — `parts_str`, `_DT_FMT`, `_DATE_FMT`.
- **[download.py](../../src/nisar_db/download.py)** — `download_earthdata_granule`,
  `download_from_url`, `download_s3_url`. CLI in
  [download_cli.py](../../src/nisar_db/download_cli.py).
- **[parser.py](../../src/nisar_db/parser.py)** — `parse_gslc`, `parse_gunw`,
  `parse_s3_files_to_dataframe`.

## Finding things fast

| Looking for… | Go to |
|---|---|
| Parse a NISAR granule filename | `filenames.py` (`GSLCFilename`/`GUNWFilename`) |
| Search CMR by track/frame (fast, indexed) | `search/cmr.py` `search_nisar_products` |
| List/search an S3 bucket by profile | `search/s3.py` `search_s3_products` |
| Build a reusable index of an S3 bucket | `s3_catalog.py` `build_s3_catalog` / `query_catalog` |
| Attach frame geometry to a catalog | `s3_catalog.py` `catalog_to_gdf`; `geodb.py` |
| Get the global TrackFrame GeoPackage | `geodb.py` `get_trackframe_db` (CLI `download-frame-db`) |
| CMR → DuckDB + JSON catalog | `catalog/` + `nisar_catalog.py` |
| Decide when a frame's reference epoch resets | `reference_dates.py` `calculate_reference_dates` |
| Label batches historical/forward, diff two releases | `processing_mode.py` |
| Simplified frame polygons for a web map | `frame_to_bound.py` `write_frame_geometries_geojson` |
| CLI command wiring | `cli.py`; per-command `main()` in the target module |
| Collection short names / provider | `filenames.py` `NISARCollection` |

## Keeping this index current

When you add/rename/move a public module or symbol, update the tree and the module
index above. To regenerate the raw symbol list:

```bash
python - <<'PY'
import ast, pathlib
for f in sorted(pathlib.Path("src/nisar_db").rglob("*.py")):
    if "__pycache__" in str(f): continue
    t = ast.parse(f.read_text())
    syms = [n.name for n in t.body
            if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and not n.name.startswith("_")]
    print(f.relative_to("src/nisar_db"), "->", ", ".join(syms))
PY
```
