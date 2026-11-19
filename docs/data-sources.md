# Viewer data sources

Where every value the [frame viewer](frame-viewer.md) shows comes from, and how
it is derived. Values are listed by where they live: per frame, per granule
(GSLC) or interferogram (GUNW), the granule flags, and the QA metrics.

Four sources feed the page:

| Source | What it is | Access |
|---|---|---|
| **TrackFrame database** | `NISAR_TrackFrame_L_*.gpkg`, one row per NISAR frame (`nisar-db download-frame-db`) | public download |
| **CMR** | NASA's Common Metadata Repository, `https://cmr.earthdata.nasa.gov/search/granules.umm_json`, collections `NISAR_L2_GSLC_PROVISIONAL_V1` and `NISAR_L2_GUNW_PROVISIONAL_V1`. UMM-JSON leaves some granules out of its results while counting them in `CMR-Hits` (seen October 2026), so every search is checked against the atom-JSON `granules.json` listing and the missing records are fetched from `search/concepts/<id>` ([details](background/cmr-umm-json-drops.md)) | public |
| **`QA_STATS.h5`** | the small (60-160 kB) statistics file the NISAR QA software writes next to every product: `https://nisar.asf.earthdatacloud.nasa.gov/NISAR/<collection>/<granule>/<granule>_QA_STATS.h5` | Earthdata login |
| **Repository files** | CalVal sites, rollout regions, blackout dates, the flag and QA caches under `catalog/` | in the repo |

The product HDF5 itself (several GB) is read only to learn the RFI mitigation
flag of a release not seen before (one product per release); see
[RFI mitigation](#granule-flags).

## Frames

| Value | Source | Derivation |
|---|---|---|
| `frame_idx` | TrackFrame database | row index of the frame in the database (the number OPERA uses) |
| track, frame, pass direction, outline | TrackFrame database | `track`, `frame`, `passDirection`, geometry; coordinates rounded to 1e-4 degrees |
| `isSNWG`, `isDNC` | TrackFrame database | as stored |
| `hasLand` (Land frames only) | TrackFrame database | `hasLand`, which is `fractionLand > 0`: any land at all |
| `isCalVal` (CalVal frames only) | `scripts/disp_s1_calval_sites.geojson` | frames covering at least 10 % of a DISP-S1 validation site (plus the Mexico City basin) |
| rollout options | rollout regions GeoJSON in the repo | regions the frame intersects |
| blackout months, windows | `catalog/opera-nisar-disp-blackout-dates.json` | per-frame snow windows from `scripts/snow-analysis`, summarised by `blackout_summary` |
| reference (reset) dates | latest release's `opera-nisar-disp-reference-dates-*.json` | as published |
| `gslc_count`, `n_unique`, `n_duplicate`, `n_modes`, `n_full`, `n_partial` | CMR GSLC search | counted from the frame's granules; a unique acquisition is one (date, mode, coverage); the `_sel` variants recount under the chips and date range |
| consistent mode / coverage | CMR GSLC search, or the published consistent-GSLC catalog | `common_mode_coverage`: among science modes 4005 / 2005, full frame over partial (unless partials dominate), then most acquisitions, then mode priority; the published catalog wins when given |
| `gunw_count`, `gunw_pairs`, GUNW modes, polarizations, `dt` range | CMR GUNW search | counted from the frame's interferograms |
| GUNW network | CMR GUNW search | connected components of the reference-secondary graph, computed in the page |
| flag colourings (*all / some / none*) | granule flags below | share of the frame's granules (under the chips) whose flag is set; *not collected* when none has been read |
| QA colourings | QA metrics below | median, worst decile or share past a threshold over the frame's granules under the chips |

## Granules and interferograms

Read from the granule name (GSLC and GUNW ids are the CMR `GranuleUR`), split on
`_`:

| Field | GSLC position | GUNW position | Example |
|---|---|---|---|
| cycle | 4 | 4 (reference), 8 (secondary) | `031` |
| track | 5 | 5 | `155` |
| direction | 6 | 6 | `D` |
| frame | 7 | 7 | `084` |
| mode | 8 | 9 | `4005`, `4000` |
| polarization | 9 | 10 | `DHDH`, `SH` |
| start time | 11 | 11 (reference), 13 (secondary) | `20260928T231125` |
| composite release (CRID) | 13 | 15 | `P05023` |
| coverage (`F` full / `P` partial) | 15 | 17 | `F` |
| product version | last | last | `001` |

## Granule flags

Six flags per granule, keyed `j f o r m d` in `catalog/granule_flags.json.gz`
(booleans as 0 / 1). Collected by `scripts/collect_granule_qa.py
--flags-output` and by the local search & rebuild.

| Key | Flag | Source | GSLC | GUNW |
|---|---|---|---|---|
| `j` | joint observation | `QA_STATS.h5`; also CMR `JOINT_OBSERVATION` | `science/LSAR/identification/isJointObservation` | `referenceIsJointObservation` OR `secondaryIsJointObservation` (same group) |
| `f` | full frame | `QA_STATS.h5`; also CMR `FULL_FRAME` and the name's coverage field | `science/LSAR/identification/isFullFrame` | same |
| `o` | orbit type | CMR `ORBIT_TYPE` | the granule's orbit (`MOE`, `NOE`, `FOE`, `POE`) | the **reference** acquisition's orbit (see below) |
| `r` | RFI mitigation applied | inferred per product type and CRID | `0` for every GSLC read so far | `1` for every GUNW read so far |
| `m` | mixed mode | `QA_STATS.h5` | `science/LSAR/identification/isMixedMode` | same |
| `d` | dithered | `QA_STATS.h5` | `science/LSAR/identification/isDithered` | same |

Notes:

- **Free with the search.** `j`, `f` and `o` are in every CMR record, and `r`
  follows from the release, so a local rebuild shows those four for every
  granule without downloading anything. `m` and `d` need the `QA_STATS.h5`
  download and read *not read* until then.
- **RFI mitigation (`r`)** is recorded only inside the product
  (`.../GSLC/metadata/processingInformation/parameters/rfiMitigationApplied`;
  for a GUNW the `reference/` and `secondary/` copies, both required). It is set
  by the processing configuration, and every (product type, CRID) group read so
  far agrees on one value, so a granule inherits its group's value. A group not
  seen before is learned from one product read; a group whose cached granules
  disagree is not inherited.
- **GUNW orbit type (`o`).** CMR records only the reference acquisition's orbit.
  GUNW pairs read from the product before (`.../GUNW/metadata/orbit/{reference,secondary}/orbitType`)
  carry both as `REF/SEC` when they differ (6 of ~10,000 checked); the viewer
  labels them *orbit ref/sec* and the others *ref. orbit*. Reading the
  secondary back is in `TODO.md`.
- **Checked** against the product reads of the earlier collector: 14,800
  re-read globe granules and stratified samples over all 16 (type, mode,
  polarization, CRID) groups matched on every flag; CMR `j` / `f` matched on
  1,175 granules and `o` on 2,032.

## QA metrics

Keyed in `catalog/granule_qa.json.gz`, all from `QA_STATS.h5`. A granule
withdrawn from the archive (the file returns 404) is cached as empty.

**GUNW**, from the frequency-A co-pol (`HH`, else `VV`) group
`science/LSAR/QA/data/frequencyA/unwrappedInterferogram/<pol>/`:

| Key | Metric | Dataset(s) under the group | Derivation |
|---|---|---|---|
| `cm` / `ca` | coherence median / mean | `coherenceMagnitude/histogramBins`, `histogramDensity` | from the histogram (the QA software's own `mean_value` reads near zero for some granules, so it is not used) |
| `v` | valid unwrapped pixels (%) | `connectedComponents/percentPixelsWithNonZeroCC`, `unwrappedPhase/percentNan` | `100 * nonZeroCC / (100 - percentNan)`: share of the data area, not of the raster |
| `l` | largest connected component (%) | `connectedComponents/percentPixelsInLargestCC`, `unwrappedPhase/percentNan` | same scaling |
| `n` | connected components | `connectedComponents/numValidConnectedComponents` | as stored |
| `im` / `imd` | ionosphere phase screen mean / median (rad) | `ionospherePhaseScreen/histogram*` | from the histogram |
| `is` | ionosphere phase screen spread (rad) | `ionospherePhaseScreen/histogram*` | standard deviation from the histogram |
| `iu` | ionosphere uncertainty (rad) | `ionospherePhaseScreenUncertainty/histogram*` | mean from the histogram |

**GSLC**:

| Key | Metric | Dataset(s) | Derivation |
|---|---|---|---|
| `rl` | RFI likelihood | `science/LSAR/RFI/data/frequency*/<pol>/rfiLikelihood` | the largest over frequencies and polarizations; documented as 0-1 but up to ~1e31 in the archive, so stored as reported and coloured on a log scale |

**Both**:

| Key | Value | Dataset |
|---|---|---|
| `qv` | QA software version | `science/LSAR/QA/processing/QASoftwareVersion` |

Histogram statistics weight each bin centre by `density * bin width`; the
median interpolates inside the bin where the cumulative weight crosses one
half (`hist_stats` in `scripts/collect_granule_qa.py`).

Values the products carry that the viewer does not show yet, with what each
costs to collect, are listed in
[Metadata for the viewer](background/viewer-metadata-candidates.md).

## Browse and QA images

| Image | Source | Access |
|---|---|---|
| browse image | `https://nisar.asf.earthdatacloud.nasa.gov/BROWSE/<collection>/<granule>/<granule>_LATLON.png` | public |
| QA report layers | pages of `<granule>_QA_REPORT.pdf`, cut into layers by the local helper | Earthdata login, through `scripts/qa_browse_server.py` |
