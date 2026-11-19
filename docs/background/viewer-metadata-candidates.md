# Background: metadata worth showing in the viewer

The viewer now shows six granule flags and a handful of QA metrics (see
[Viewer data sources](../data-sources.md)). NISAR products carry much more.
This page lists the values worth adding, where each comes from, and what it
costs to collect, so the next viewer layer can be picked by value and cost.

The field-level details are in [NISAR product metadata](nisar-product-metadata.md)
and the per-product layouts ([RSLC](../products/rslc.md),
[GSLC](../products/gslc.md), [GUNW](../products/gunw.md),
[GOFF](../products/goff.md)). Everything here was checked on 2026-10-21 on
`P05023` granules.

## What each source costs

| Tier | Source | Cost per granule | Already collected by |
|---|---|---|---|
| 0 | File name | nothing | the catalogs (`nisar_db.filenames`) |
| 1 | CMR record | nothing extra: it comes with the search the viewer already runs | `catalog_flags` in `scripts/collect_granule_qa.py` (3 attributes) |
| 2 | `_QA_STATS.h5` (76–940 kB) and `_QA_SUMMARY.csv` (1–4 kB) | one small download with an Earthdata login, about 2–3 s, mostly login redirects | `scripts/collect_granule_qa.py`, weekly, cached in `catalog/granule_qa.json.gz` |
| 3 | Product `.h5` metadata | byte-range reads with an Earthdata login, 1–30 s for the full layout; the file itself is 0.7–20 GB and is never downloaded | the corner reads in the QA helper only |

Many tier-3 values are fixed for a composite release (CRID) and radar mode.
Reading them once per CRID and mode, instead of once per granule, makes them
nearly free.

## Tier 0 and 1: no extra requests

| Value | Products | Where | What it would show |
|---|---|---|---|
| Orbit and pointing accuracy | all | file name `A` | `N` means near-real-time pointing even when the orbit is `MOE`; separates products that may be reprocessed later |
| Product counter | all | file name `CTR` | counters above `001` mark granules regenerated under the same CRID |
| Single or mixed mode | RSLC, GSLC | file name `S` | the mixed-mode flag without reading the QA file |
| CRID, product version, ISCE3 version, PGE version | all | file name `CRID`; CMR `PRODUCT_VERSION`, `DataGranule.Identifiers` (`SASVersionId`), `PGEVersionClass` | colour frames by processing release; show where a stack mixes releases |
| Acquisition-to-archive latency | all | CMR `TemporalExtent` end, `DataGranule.ProductionDateTime`, `ProviderDates` (`Insert`) | how long after acquisition each granule was made and published. Samples: an RSLC was produced about 12 h after acquisition, a GUNW about 42 h after its secondary acquisition |
| Input granules | all | CMR `InputGranules` | link a GUNW or GOFF to the two RSLCs it was made from, and an RSLC to its L0 data |
| Land or ocean | all | CMR `SpatialExtent.GranuleLocalities` | filter out ocean-only granules |
| Stack id | all | CMR `STACK_ID` | check that track, direction and frame agree with the stack the processor used |
| Radar configuration | RSLC | CMR `INPUT_RCID` | group RSLCs by radar configuration |
| Absolute orbit | all | CMR `OrbitCalculatedSpatialDomains` | look up the orbit of a granule directly |

## Tier 2: the QA files

The weekly QA collection already downloads `_QA_STATS.h5` for every viewer
granule, so these cost only code.

| Value | Products | Where in `_QA_STATS.h5` | What it would show |
|---|---|---|---|
| Urgent observation | all | `identification/isUrgentObservation` | urgent-response acquisitions |
| Diagnostic mode | all | `identification/diagnosticModeFlag` (`0` science, `1`–`2` diagnostic) | acquisitions not in science mode |
| Input data exception | all | `identification/hasInputDataException`, or `reference…` / `secondary…` | granules made from flawed input |
| Planned datatake and observation | all | `identification/plannedDatatakeId`, `plannedObservationId`, or `reference…` / `secondary…` | group consecutive frames into one datatake; show a datatake as one strip along the track |
| QA pass or fail | all | `_QA_SUMMARY.csv`, `Result` column | one QA status per granule, with the failing check named |
| Invalid pixels | all | `percentTotalInvalid` of each layer | how much of the frame holds usable data |
| Mean backscatter | RSLC, GSLC | `QA/data/{f}/{pol}/backscatterHistogramDensity` | a per-granule backscatter value, for time series of wetness, snow or flooding at frame scale |
| RFI likelihood | RSLC | `RFI/data/{f}/{pol}/rfiLikelihood` | as the viewer does for GSLC |
| Wrapped coherence | GUNW | `QA/data/{f}/wrappedInterferogram/{pol}/coherenceMagnitude` | coherence before unwrapping, next to the unwrapped coherence already shown |
| Pixel offsets | GUNW, GOFF | `QA/data/{f}/pixelOffsets/…/alongTrackOffset`, `slantRangeOffset`: mean and standard deviation | a mean offset far from zero flags misregistration between the two acquisitions; a large spread flags motion (glaciers, landslides) or noise |
| Offset reliability | GUNW, GOFF | `…/correlationSurfacePeak` mean | how much to trust the offsets |
| Offset variance | GOFF | `…/layer{n}/alongTrackOffsetVariance`, `slantRangeOffsetVariance`, `crossOffsetVariance` | offset quality per layer |

## Tier 3: the product file's metadata

| Value | Products | Where in the `.h5` | What it would show |
|---|---|---|---|
| RFI mitigation applied | GSLC; GUNW and GOFF per acquisition | `metadata/processingInformation/parameters/rfiMitigationApplied`, or `…/reference/` and `…/secondary/rfiMitigationApplied` | replaces the viewer's `r` flag, which is now inferred per product type and CRID |
| RFI mitigation algorithm | RSLC | `metadata/processingInformation/algorithms/rfiMitigation` (`ST-EVD`, `FDNF` or `disabled`) | which mitigation ran |
| RFI detail | RSLC | `metadata/RFI/{f}/{pol}/…/hitCount`, `interferenceSignalRatio` | where along the track RFI hit, and how hard |
| Geolocation corrections applied | GSLC; GUNW and GOFF | GSLC: `…/parameters/{range,azimuth}IonosphericGeolocationCorrectionApplied`, `{dry,wet}TroposphericGeolocationCorrectionApplied`. GUNW, GOFF: `…/parameters/geocoding/…CorrectionApplied` | whether the geolocation was corrected for ionosphere and troposphere |
| Geometric accuracy | GSLC | `metadata/ceosAnalysisReadyData/geometricAccuracy/{bias,standardDeviation}/{x,y}` | geolocation error estimates; `nan` in the sample, so worth watching for when they are filled |
| Exact input RSLCs | GUNW, GOFF | `metadata/processingInformation/inputs/l1ReferenceSlcGranules`, `l1SecondarySlcGranules` | the input names, including their CRIDs |
| Unwrapping and ionosphere methods | GUNW | `…/algorithms/unwrapping/unwrappingAlgorithm` (`snaphu`), `…/algorithms/ionosphereEstimation/ionosphereAlgorithm`, `…/parameters/ionosphere/{high,low}BandBandwidth` | how each interferogram was unwrapped, and which ionosphere method ran |
| DEM and water mask | all L2 | `…/inputs/demSource`, `…/inputs/waterMaskSource` | provenance; changes between releases |
| Calibration | RSLC, GSLC | `metadata/calibrationInformation/{f}/{pol}/scaleFactor`, `commonDelay`, `faradayRotation` | the scale factor can change between releases, which breaks backscatter comparisons across them |
| Noise floor | RSLC, GSLC | `metadata/calibrationInformation/{f}/noiseEquivalentBackscatter/{pol}` | noise-equivalent backscatter per granule |

## Where to start

These fit the existing code with the least work, in order:

1. **Tier 0 and 1 provenance and latency.** Add the accuracy letter, the
   counter, CRID and versions, and latency to `catalog_flags`. The search
   already returns them.
2. **Tier 2 QA additions.** Extend `collect_granule_qa.py` with the datatake
   id, the urgent, diagnostic and input-exception flags, the QA summary
   result, and the GUNW and GOFF offset statistics. The files are already
   downloaded.
3. **Tier 3 once per CRID and mode.** Use the range reads of
   `scripts/document_product_layout.py` on one granule per CRID and mode, and
   attach the results to every granule sharing them. The exception is
   `rfiMitigationApplied`, which can vary per granule and needs a per-granule
   read, or a check that it does not vary.
