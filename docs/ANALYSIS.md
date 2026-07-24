# nisar_db vs burst_db — Analysis

Comparison of `burst_db` (mature reference) and `nisar_db` (NISAR analog, mid-refactor),
with the gap list needed to make `nisar_db` produce release/CI assets the way `burst_db` does.

- `burst_db`: https://github.com/opera-adt/burst_db
- `nisar_db`: https://github.com/opera-adt/nisar_db

---

## 1. burst_db — how the assets are produced

`burst_db` is the mature reference pattern. Everything funnels through **one installed CLI**
(`opera-db`), orchestrated by a `Makefile`, wrapped by `create-release.sh`, and the outputs
become **GitHub Release assets**. The release itself is cut manually via `create-release.sh`
+ `git push origin <tag>`; there is **no** `.github/workflows/*.yml` (only issue templates).

### CLI → module wiring (`src/burst_db/cli.py`, entry point `opera-db`)

| Command | Module | Produces |
|---|---|---|
| `create` | `build_frame_db.py` | `opera-s1-disp-<ver>.gpkg`, `*-2d.gpkg`, frame/burst geojson, frame-to-burst / burst-to-frame `.json.zip`, sqlite3 files |
| `make-burst-catalog` | `create_cslc_burst_catalog.py` | `opera-disp-s1-consistent-burst-ids-*.json` |
| `make-reference-dates` | `reference_dates.py` | `opera-disp-s1-reference-dates-*.json` |
| `create-blackout` | `create_blackout_dates_s1.py` | `opera-disp-s1-blackout-dates-*.json` |
| `intersect` / `lookup` | `query_frame_db.py` | query helpers |
| `urls-for-frame` | `query_consistent_bursts.py` | query helper |
| `historical fetch-*` | `query_historical_bursts.py` | query helper |

Supporting (non-CLI) modules: `frames.py`, `_esa_burst_db.py`, `_land_usgs.py`,
`_opera_north_america.py`, `create_2d_geojsons.py`, `utils.py`, `reconcile_and_label_db.py`,
plus a bundled `data/` dir (priority-rollout geojson, land/NA shapes, algorithm-parameter
overrides).

### Properties worth copying

- Every command lives in the installed package.
- `Makefile` targets are pure `opera-db …` calls — no `python some_script.py`, no `sys.path` hacks.
- Inputs (CMR survey, snow parquet) are external; outputs are dated filenames.

---

## 2. nisar_db — current state

`nisar_db` is the NISAR analog, mid-refactor, with **two asset-production paths that don't
share code cleanly**:

### Path A — Release assets (mirrors burst_db) via `Makefile`

- `opera-nisar-disp-frame-to-bounds-<date>.json` (+ `.json.zip`) ← `python create_frame_to_bound.py`
- `opera-nisar-disp-consistent-gslc-<date>.json` ← `python create_consistent_gslc_catalog.py`
- `gslc_catalog.csv` ← `python create_gslc_catalog.py`

### Path B — Daily web-app catalogs via `.github/workflows/update_nisar_catalog.yml`

Cron `0 0 * * *`, runs the **packaged** modules and commits `catalog/*.json` back to the repo:

- `python -m nisar_db.catalog.create_gslc_catalog` → `gslc_tracks/frames/dates/scenes.json` + `gslc_catalog.duckdb`
- `python -m nisar_db.catalog.create_gunw_catalog` → `gunw_tracks/frames/pairs/interferograms.json` + `gunw_catalog.duckdb`

### Packaged CLI (`src/nisar_db/cli.py`, entry point `nisar-db`)

`create-frame-to-bound`, `create-catalog`, `create-consistent`, `search`.

### Problems (the gap vs burst_db)

1. **Triplicated source of truth.** `create_frame_to_bound.py`, `create_gslc_catalog.py`,
   `create_consistent_gslc_catalog.py` exist as **byte-identical copies** in both repo root
   and `src/scripts/` (confirmed by md5), and are not real package modules.
2. **`sys.path` hacks instead of packaging.** The installed CLI does `sys.path.insert(...)`
   then `from create_frame_to_bound import main` — importing the **repo-root loose script**,
   so `nisar-db` only works from a git checkout, not from a `pip install`.
   `catalog/create_gslc_catalog.py` does the same hack.
3. **Makefile uses `python script.py`, not the CLI** — so CLI commands and Makefile assets
   are built by different code paths.
4. **CLI ↔ workflow ↔ README disagree on commands.** README documents `create-nisar-catalog`,
   `create-blackout-dates`, `create-catalog --na-only`; the CLI registers only 4 commands,
   none of which is blackout or nisar-catalog. Blackout logic lives in
   `src/nisar_db/catalog/create_blackout_dates.py`, `src/scripts/create_blackout_dates_*.py`,
   and inline in `nisar_workflow.py` — none wired to the CLI.
5. **~10 loose root scripts** are experiments/duplicates: `finalize_nisar_catalog.py`,
   `nisar_workflow.py`, `process_gslc_file.py`, `update_process_gslc_file.py`,
   `process_multiple_gslc.sh`, `quick_test_search.py`, `inspect_nisar_trackframe.py`,
   plus 8 overlapping scripts in `src/scripts/`.
6. **No release automation.** No `create-release.sh`; the one GitHub Action only handles
   Path B (daily catalog), not a tagged **release** of Path A assets.
7. **`duckdb`/`pandas` missing from `requirements.txt`** (the workflow `pip install`s them
   ad-hoc); the NISAR TrackFrame gpkg is an external input, none bundled.
8. **Bug:** `duckdb.DuckDBPy.Connection` type hint in both catalog modules should be
   `duckdb.DuckDBPyConnection`.

---

## 3. Needed modules, scripts, workflows

To bring `nisar_db` to `burst_db`-level "assets produced by a GitHub Action/release":

### Modules (consolidate into installed package `src/nisar_db/`)

- [ ] `frame_to_bound.py` — move root `create_frame_to_bound.py` in; delete root + `src/scripts` copies
- [ ] `gslc_catalog.py` — packaged `create_gslc_catalog.py` (drop `sys.path` hack)
- [ ] `consistent_gslc.py` — packaged `create_consistent_gslc_catalog.py`
- [ ] `catalog/create_gslc_catalog.py`, `catalog/create_gunw_catalog.py` — keep, remove `sys.path.insert`, fix `DuckDBPy.Connection` typo
- [ ] `catalog/create_blackout_dates.py` — single blackout module; delete `src/scripts` + `nisar_workflow.py` duplicates
- [ ] Reference-date equivalent (`reference_dates.py`) — **decide if DISP-NISAR needs reference-date resets** (parity with burst_db `make-reference-dates`)
- [ ] Keep supporting: `filenames.py`, `geodb.py`, `search_nisar.py`, `download_extended.py`, `parser.py`, `utils.py`

### CLI (`src/nisar_db/cli.py`)

- [ ] Register commands the README already advertises: `create-nisar-catalog` (GSLC+GUNW), `create-blackout-dates`, a `create-gslc-catalog`/`create-gunw-catalog` pair
- [ ] Replace all `sys.path.insert` + root-script imports with normal package imports

### Scripts to retire (root)

- [ ] `finalize_nisar_catalog.py`, `nisar_workflow.py`, `process_gslc_file.py`,
  `update_process_gslc_file.py`, `process_multiple_gslc.sh`, `quick_test_search.py`,
  `inspect_nisar_trackframe.py`, and the 3 root duplicates → fold useful bits into package or `notebooks/`
- [ ] Collapse `src/scripts/*` (11 files) — keep only genuinely-standalone tooling

### Makefile

- [ ] Rewrite targets to call `nisar-db …` (like burst_db calls `opera-db …`)

### Release automation

- [ ] Add `create-release.sh` (port from burst_db): tag → `pip install -e .` → run `make` in a dated dir → list assets → push tag

### Workflows (`.github/workflows/`)

- [ ] Keep/fix `update_nisar_catalog.yml` (Path B daily catalog) — move `duckdb`/`pandas` into `requirements.txt`
- [ ] Add `release.yml` — on tag `v*`, build Path-A assets and **upload as GitHub Release assets** (the piece that matches "assets produced by a GitHub Action")
- [ ] Optional: `ci.yml` (ruff + pytest + mypy — configured in `pyproject.toml`/`pixi.toml` but never run in CI)

### Packaging fixes

- [ ] Add `duckdb`, `pandas` (and geo deps) to `requirements.txt` / `environment.yml`
- [ ] Ensure `catalog/` and bundled data are included as package data so `pip install` works standalone

### Suggested order

1. CLI + packaging consolidation (unblocks Makefile and release workflow).
2. Makefile rewrite to `nisar-db …`.
3. `release.yml` + `create-release.sh`.
4. Delete duplicates / retire loose scripts.
