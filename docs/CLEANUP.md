# nisar_db — Cleanup Checklist

Concrete de-duplication and tidy-up actions. See `ANALYSIS.md` for the full rationale.
Do this **before** the packaging/CLI refactor so the refactor starts from one copy of each file.

---

## A. Duplicate files (byte-identical — verified by md5)

Each of these exists in **both** the repo root and `src/scripts/`. Pick the packaged home,
delete the other two copies (root + `src/scripts`), and re-point imports.

| File | root | src/scripts | md5 |
|---|---|---|---|
| `create_frame_to_bound.py` | ✔ | ✔ | `a959a4a1…` |
| `create_gslc_catalog.py` | ✔ | ✔ | `acc93eb2…` |
| `create_consistent_gslc_catalog.py` | ✔ | ✔ | `aeab1140…` |

- [ ] Move real logic into `src/nisar_db/` (e.g. `frame_to_bound.py`, `gslc_catalog.py`, `consistent_gslc.py`).
- [ ] Delete the root copy and the `src/scripts` copy.
- [ ] Update `src/nisar_db/cli.py` to import from the package (remove `sys.path.insert`).

## B. Loose root scripts to retire

Fold anything still useful into package modules or `notebooks/`, then delete:

- [ ] `finalize_nisar_catalog.py`
- [ ] `nisar_workflow.py`  *(contains a 3rd copy of blackout logic — discard, keep `catalog/create_blackout_dates.py`)*
- [ ] `process_gslc_file.py`
- [ ] `update_process_gslc_file.py`
- [ ] `process_multiple_gslc.sh`
- [ ] `quick_test_search.py`  *(→ move to `tests/` if worth keeping)*
- [ ] `inspect_nisar_trackframe.py`  *(→ `notebooks/` or a `nisar-db inspect` subcommand)*

## C. `src/scripts/` overlap (11 files)

Keep only genuinely standalone tooling; delete the rest as duplicates of packaged modules.

- [ ] `create_frame_to_bound.py`, `create_gslc_catalog.py`, `create_consistent_gslc_catalog.py` — dupes of section A → delete
- [ ] `create_blackout_dates_cli.py`, `create_blackout_dates_simple.py` — consolidate into `catalog/create_blackout_dates.py` (+ CLI command)
- [ ] `create_nisar_catalog.py`, `create_nisar_catalog_from_database.py`, `create_nisar_catalog_from_trackdb.py`, `create_nisar_catalog_simple.py`, `create_nisar_catalog_with_trackframe.py` — **5 variants**; pick one, delete the rest, wire to `create-nisar-catalog` command
- [ ] `complete_nisar_catalog.py`, `download_nisar.py` — verify against `src/nisar_db/download*.py`; delete if redundant

## D. Housekeeping

- [ ] Remove committed `__pycache__/` (root + anywhere) and add to `.gitignore` if missing.
  - stray `.pyc` seen: `create_consistent_gslc_catalog.cpython-{312,314}.pyc`, `create_frame_to_bound.cpython-314.pyc`, `create_gslc_catalog.cpython-314.pyc`
- [ ] `src/nisar_db.egg-info/` should not be committed — add to `.gitignore`.
- [ ] Decide whether `notebooks/downloads/`, `notebooks/granules/`, and the committed
  `NISAR_TrackFrame_L_20250909.gpkg` belong in git (large/data artifacts).
- [ ] Reconcile README with the actual CLI (README advertises `create-nisar-catalog`,
  `create-blackout-dates` that don't exist yet).

## E. Bug fixes to fold in during cleanup

- [ ] `src/nisar_db/catalog/create_gslc_catalog.py` and `create_gunw_catalog.py`:
  `duckdb.DuckDBPy.Connection` → `duckdb.DuckDBPyConnection`.
- [ ] Add `duckdb`, `pandas` to `requirements.txt` / `environment.yml`
  (currently `pip install`ed ad-hoc inside the workflow).

---

## Definition of done

- One copy of each module, all under `src/nisar_db/`.
- `pip install .` then `nisar-db --help` works from a clean checkout (no `sys.path` hacks).
- Repo root contains no loose `*.py` scripts (only config + `README`/`Makefile`).
- `README` command list matches `cli.py`.
- No `__pycache__/` or `*.egg-info/` tracked in git.
