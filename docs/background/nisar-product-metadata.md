# Background: NISAR product metadata

Where each value about a NISAR RSLC, GSLC, GUNW or GOFF granule can be found:
the file name, the CMR record, the QA files, and NASA's STAC catalog.

Everything below was checked on 2026-10-21 against 20 recent granules of each
`PROVISIONAL_V1` collection (composite release `P05023`, product version
`1.0.11`), one `_QA_STATS.h5` and `_QA_SUMMARY.csv` per product, and the
naming convention document JPL D-102255 Rev C (2025-10-19). Fields can change
with a new composite release, so recheck when the CRID changes.

The datasets inside each product file, with their shapes, types, units and
descriptions, are listed per product: [RSLC](../products/rslc.md),
[GSLC](../products/gslc.md), [GUNW](../products/gunw.md) and
[GOFF](../products/goff.md).

## Collections

| Product | What it is | CMR short name | CMR concept id | CMR-STAC collection | Granules (2026-10-21) |
|---|---|---|---|---|---|
| RSLC | L1 range-Doppler single look complex | `NISAR_L1_RSLC_PROVISIONAL_V1` | `C2853145197-ASF` | `NISAR_L1_RSLC_PROVISIONAL_V1_1` | 162,834 |
| GSLC | L2 geocoded single look complex | `NISAR_L2_GSLC_PROVISIONAL_V1` | `C2854332392-ASF` | `NISAR_L2_GSLC_PROVISIONAL_V1_1` | 160,370 |
| GUNW | L2 geocoded unwrapped interferogram | `NISAR_L2_GUNW_PROVISIONAL_V1` | `C2854335566-ASF` | `NISAR_L2_GUNW_PROVISIONAL_V1_1` | 92,770 |
| GOFF | L2 geocoded pixel offsets | `NISAR_L2_GOFF_PROVISIONAL_V1` | `C2854341702-ASF` | `NISAR_L2_GOFF_PROVISIONAL_V1_1` | 27,435 |

Each product also has a `BETA_V1` collection, such as `NISAR_L2_GUNW_BETA_V1`, which was not checked here.

## Which source has which value

`F` = file name, `C` = CMR `AdditionalAttributes` (or another UMM-G field
where named), `Q` = `QA_STATS.h5` group `science/LSAR/identification`,
`S` = CMR-STAC item properties. A dash means the source does not carry it.

| Value | File name | CMR | QA identification | STAC |
|---|---|---|---|---|
| Product type | `PROD` | `PRODUCT_TYPE`, `PRODUCT_TYPE_DESC` | `productType` | collection id |
| Processing level | `L` | `PROCESSING_LEVEL` | `productLevel` | – |
| Processing type (production, urgent, on-demand) | `PT` | `PRODUCTION_PIPELINE` | `processingType` | – |
| Cycle | `CYL`, plus `SCY` for pairs | – | – | – |
| Track (relative orbit) | `REL` | `TRACK_NUMBER` | `trackNumber` | – |
| Frame | `FRM` | `FRAME_NUMBER` | `frameNumber` | – |
| Track-direction-frame stack | from `REL_P_FRM` | `STACK_ID` | – | – |
| Pass direction | `P` | `ASCENDING_DESCENDING` | `orbitPassDirection` | – |
| Absolute orbit number | – | UMM `OrbitCalculatedSpatialDomains` | `absoluteOrbitNumber`, or `reference`/`secondary…` for pairs | – |
| Frequencies present | `MODE` (`00` = band missing) | `FREQUENCIES` | `listOfFrequencies` | – |
| Range bandwidth (MHz) | `MODE` | `FREQUENCY_{A,B}_RANGE_BANDWIDTH`, `RANGE_BANDWIDTH_CONCAT` | – | – |
| Polarizations | `POLE` (2 × 2 chars) or `PO` (2 chars, pairs) | `FREQUENCY_{A,B}_POLARIZATION`, `…_POLARIZATION_CONCAT` | – (QA data: `listOfPolarizations` per frequency) | – |
| Single or mixed mode | `S` (single-acquisition products) | – | `isMixedMode` | – |
| Start and end time | `StartDateTime`, `EndDateTime` (to the second) | UMM `TemporalExtent` | `zeroDopplerStartTime`, `zeroDopplerEndTime` (to the ns) | `start_datetime`, `end_datetime` |
| Reference and secondary times (pairs) | `Ref…`, `Sec…DateTime` | `REFERENCE_` / `SECONDARY_ZERO_DOPPLER_{START,END}_TIME` | `reference…` / `secondary…ZeroDopplerStartTime` | only the span |
| Composite release (CRID) | `CRID` | UMM `DataGranule.Identifiers` (`CRID`) | `compositeReleaseId` | – |
| Orbit and pointing accuracy | `A` | `ORBIT_TYPE` (orbit only, see below) | – | – |
| Full or partial frame | `C` | `FULL_FRAME` | `isFullFrame` | – |
| Joint observation | – | `JOINT_OBSERVATION` | `isJointObservation`, or `reference`/`secondaryIsJointObservation` | – |
| Dithered | – | – | `isDithered` | – |
| Urgent observation | `PT` = `UR` | – | `isUrgentObservation` | – |
| Processing center | `LOC` | `PROCESSING_CENTER` | `processingCenter` | – |
| Product counter | `CTR` | UMM `DataGranule.Identifiers` (`ProductCounter`) | – | – |
| Product version | – | `PRODUCT_VERSION` | `productVersion` | – |
| Software (ISCE3) version | – | UMM `DataGranule.Identifiers` (`SASVersionId`) | – | – |
| PGE name and version | – | UMM `PGEVersionClass` | – | – |
| Input granules | – | UMM `InputGranules` (L0 RRSD for RSLC, RSLCs for L2) | – | – |
| RSLC input radar configuration | – | `INPUT_RCID` (RSLC only) | – | – |
| Planned datatake and observation ids | – | – | `plannedDatatakeId`, `plannedObservationId` | – |
| Footprint | – | UMM `SpatialExtent` (polygon, plus land/ocean localities) | `boundingPolygon` (WKT) | item geometry |
| File size and MD5 | – | UMM `DataGranule.ArchiveAndDistributionInformation` | – | – |
| Product DOI | – | – | `productDoi` | – |
| Static layers location (L2) | – | – | `staticLayersDataAccess` | – |
| Look direction | – | UMM `Platforms` (`LookDirection`) | `lookDirection` | – |
| Input data exception | – | – | `hasInputDataException`, or `reference`/`secondary…` | – |
| RFI likelihood | – | – | – (QA data: `RFI/data/…/rfiLikelihood`, RSLC and GSLC) | – |

### Things that disagree, or look like they do

- **The accuracy letter is not the orbit type.** `ORBIT_TYPE` in CMR names the
  orbit ephemeris only, for example `MOE`. The file name's `A` is the lower
  fidelity of the orbit and the radar pointing, so a granule processed with
  `MOE` and near-real-time pointing is named `N`. Every sampled granule had
  `ORBIT_TYPE = MOE` and `A = N`.
- **RSLC's identification group uses short codes, while L2 uses words.** RSLC
  has `processingType = PR` and `processingCenter = J`. GSLC, GUNW and GOFF
  have `Nominal` and `JPL`.
- **Pair products have no top-level `isJointObservation`.** GUNW and GOFF
  carry `referenceIsJointObservation` and `secondaryIsJointObservation`. CMR's
  single `JOINT_OBSERVATION` is the only combined value.
- **nisar_db's file name parser uses its own field names.** In
  `nisar_db.filenames`, `instrument` holds `IL` (for example `L2`), `source`
  holds `S`, `orbits` holds `A`, `location` holds `LOC` and `version` holds
  `CTR`.

## File names

From JPL D-102255 Rev C.

Single-acquisition products (RSLC, GSLC):

```text
NISAR_IL_PT_PROD_CYL_REL_P_FRM_MODE_POLE_S_StartDateTime_EndDateTime_CRID_A_C_LOC_CTR.h5
NISAR_L2_PR_GSLC_033_118_A_010_0005_NADV_A_20261020T085338_20261020T085345_P05023_N_P_J_001.h5
```

Pair products (GUNW, GOFF):

```text
NISAR_IL_PT_PROD_CYL_REL_P_FRM_SCY_MODE_PO_RefStart_RefEnd_SecStart_SecEnd_CRID_A_C_LOC_CTR.h5
NISAR_L2_PR_GUNW_032_117_D_105_033_4000_SH_20261008T080800_20261008T080807_20261020T080800_20261020T080807_P05023_N_P_J_001.h5
```

| Field | Length | Meaning and values |
|---|---|---|
| `NISAR` | 5 | mission |
| `IL` | 2 | instrument (`L` = L-SAR, `S` = S-SAR) and level (`1`, `2`) |
| `PT` | 2 | processing type: `PR` production, `UR` urgent response, `OD` science on-demand |
| `PROD` | 4 | `RSLC`, `GSLC`, `GUNW`, `GOFF` (also `GCOV`, `RIFG`, `RUNW`, `ROFF`, `SME2`) |
| `CYL` | 3 | 12-day repeat cycle, from `001`; for pairs, the reference cycle |
| `REL` | 3 | relative orbit (track), `001`–`173` |
| `P` | 1 | `A` ascending, `D` descending |
| `FRM` | 3 | track frame, `001`–`176`; frames are about 240 × 240 km |
| `SCY` | 3 | pairs only: the secondary cycle |
| `MODE` | 4 | range bandwidth of frequency A then B, two digits each: `40`, `20`, `77`, `05`, or `00` when the band is missing (`4005` = 40 + 5 MHz, `0005` = only B at 5 MHz) |
| `POLE` | 4 | single-acquisition products: two characters per frequency, A then B. `SH` = HH, `SV` = VV, `DH` = HH/HV, `DV` = VV/VH, `CL` / `CR` = compact, `QP` = quad, `NA` = band missing, `XX` = off-nominal (read the metadata). `NADV` = no A, dual-pol V on B |
| `PO` | 2 | pairs: one code for the processed frequency, from the same list plus `QH`, `QV` (quasi-quad) and `QD` (HH/VV quasi-dual) |
| `S` | 1 | single-acquisition products: `A` single mode, `M` mixed mode |
| date-times | 15 each | `YYYYMMDDTHHMMSS`, UTC, zero-Doppler |
| `CRID` | 6 | composite release `EMMmmp`: environment (`P` production, `R` reprocessing, `S` science on-demand, `D`, `T`, `A`), major, minor and patch release |
| `A` | 1 | orbit and pointing accuracy, the lower of the two: `P` precise, `M` medium, `N` near-real-time, `F` forecast, `X` special request |
| `C` | 1 | coverage: `F` full (at least 75 % of the frame), `P` partial |
| `LOC` | 1 | processing location: `J` JPL (`I` recommended for ISRO) |
| `CTR` | 3 | product counter: `001`, raised when the same granule is regenerated under the same CRID |

## CMR records (UMM-G)

Every sampled record of all four products has the same top-level fields:
`GranuleUR` (the file name without `.h5`), `CollectionReference`,
`TemporalExtent`, `SpatialExtent`, `OrbitCalculatedSpatialDomains`,
`Platforms`, `PGEVersionClass`, `InputGranules`, `DataGranule`,
`ProviderDates`, `RelatedUrls`, `AdditionalAttributes` and
`MetadataSpecification` (UMM-G 1.6.6).

### Additional attributes

| Attribute | RSLC | GSLC | GUNW | GOFF | Example |
|---|---|---|---|---|---|
| `PRODUCT_TYPE`, `PRODUCT_TYPE_DESC` | ✓ | ✓ | ✓ | ✓ | `GUNW`, `Geocoded Unwrapped Interferogram` |
| `PROCESSING_LEVEL` | ✓ | ✓ | ✓ | ✓ | `L2` |
| `PRODUCTION_PIPELINE` | ✓ | ✓ | ✓ | ✓ | `PR` |
| `PROCESSING_CENTER` | ✓ | ✓ | ✓ | ✓ | `JPL` |
| `PRODUCT_VERSION` | ✓ | ✓ | ✓ | ✓ | `1.0.11` |
| `TRACK_NUMBER`, `FRAME_NUMBER` | ✓ | ✓ | ✓ | ✓ | `117`, `105` |
| `ASCENDING_DESCENDING` | ✓ | ✓ | ✓ | ✓ | `DESCENDING` |
| `STACK_ID` | ✓ | ✓ | ✓ | ✓ | `116_A_015` |
| `FREQUENCIES` | ✓ | ✓ | ✓ | ✓ | `A`; RSLC and GSLC also `B` |
| `FREQUENCY_A_POLARIZATION`, `…_CONCAT` | when A exists | when A exists | ✓ | ✓ | `HH`, `HH+HV` |
| `FREQUENCY_A_RANGE_BANDWIDTH` | when A exists | when A exists | ✓ | ✓ | `20`, `40`, `77` |
| `FREQUENCY_B_POLARIZATION`, `…_CONCAT`, `…_RANGE_BANDWIDTH` | ✓ | ✓ | – | – | `VV+VH`, `5` |
| `RANGE_BANDWIDTH_CONCAT` | ✓ | ✓ | ✓ | ✓ | `40+5` |
| `FULL_FRAME` | ✓ | ✓ | ✓ | ✓ | `TRUE` |
| `JOINT_OBSERVATION` | ✓ | ✓ | ✓ | ✓ | `FALSE` |
| `ORBIT_TYPE` | ✓ | ✓ | ✓ | ✓ | `MOE` |
| `INPUT_RCID` | ✓ | – | – | – | `156` |
| `REFERENCE_` / `SECONDARY_ZERO_DOPPLER_{START,END}_TIME` | – | – | ✓ | ✓ | `2026-10-08T05:36:39.000000Z` |

All values are strings, booleans included.

### Files listed in `RelatedUrls`

Each file is listed twice, once over HTTPS and once as an `s3://` path for
in-region direct access. Login needed means an Earthdata login.

| File (suffix after the granule name) | `Type` | Login needed | Products |
|---|---|---|---|
| `.h5` | `GET DATA` | yes | all |
| `_QA_STATS.h5`, `_QA_REPORT.pdf`, `_QA_SUMMARY.csv` | `VIEW RELATED INFORMATION` / `PRODUCT QUALITY ASSESSMENT` | yes | all |
| `_LATLON.png`, `_LATLON_thumbnail.png`, `_NATIVE.png` | `GET RELATED VISUALIZATION` | no | all |
| `_NATIVE_{A,B}_{pol}.png` | `GET RELATED VISUALIZATION` | no | RSLC, GSLC |
| `_LATLON.kml`, `_NATIVE.kml`, `_NATIVE_{A,B}_{pol}.kml` | `EXTENDED METADATA` | yes | all; per-polarization KMLs for RSLC and GSLC |
| `.met.json`, `.dataset.json`, `.context.json`, `.h5.iso.xml`, `.h5.log`, `.rc.yaml` | `EXTENDED METADATA` | yes | all |
| `s3credentials` endpoint | `VIEW RELATED INFORMATION` | yes | all |

The public `_LATLON.png` of a GUNW shows only the unwrapped phase. The other
layers are drawn only in `_QA_REPORT.pdf`; see the viewer's QA helper.

## QA files

### `_QA_SUMMARY.csv`

Pass/fail checks, with the columns `Tool, Check, Result, Threshold, Actual,
Notes`.

| Check | RSLC | GSLC | GUNW | GOFF |
|---|---|---|---|---|
| Able to open input NISAR file? | ✓ | ✓ | ✓ | ✓ |
| Coordinate grid metadata cubes are valid? | ✓ | ✓ | ✓ | ✓ |
| Passes all `identification` group checks? | ✓ | ✓ | ✓ | ✓ |
| % Cumulative NaN and Inf and fill and near-zero pixels under threshold? | ✓ | ✓ | ✓ | ✓ |
| % NaN pixels under threshold? | – | – | ✓ | ✓ |
| Calibration information LUTs are valid? | ✓ | ✓ | – | – |
| QA completed with no exceptions? | ✓ | ✓ | ✓ | ✓ |

### `_QA_STATS.h5`

Small files: 76 kB (GSLC) to 940 kB (RSLC). The groups below are under
`science/LSAR/`. `identification` holds the values in the table above. In the
layer lists, `{f}` is `frequencyA` or `frequencyB` and `{pol}` is a
polarization.

**Statistics per layer.** Every numeric layer carries `min_value`,
`mean_value`, `max_value`, `sample_stddev`, `percentNan`, `percentInf`,
`percentFill`, `percentNearZero`, `percentTotalInvalid`, and a 200-bin
histogram (`histogramBins`, `histogramDensity`), with units.

| Product | Layers in `QA/data` | Other groups |
|---|---|---|
| RSLC | `{f}/{pol}`: min, mean, max and standard deviation of the real and imaginary parts, backscatter and phase histograms, range power spectral density, azimuth power spectral density at near, mid and far range | `RFI/data/{f}/{pol}/rfiLikelihood`; `noiseEquivalentBackscatter/data/{f}/{pol}`; processing parameters of `absoluteRadiometricCalibration` and `pointTargetAnalyzer` |
| GSLC | `{f}/{pol}`: the same complex statistics and backscatter and phase histograms, without spectra | `RFI/data/{f}/{pol}/rfiLikelihood`; `pointTargetAnalyzer` processing parameters |
| GUNW | `{f}/wrappedInterferogram/{pol}/`: `wrappedInterferogram`, `coherenceMagnitude`. `{f}/unwrappedInterferogram/{pol}/`: `unwrappedPhase`, `coherenceMagnitude`, `ionospherePhaseScreen`, `ionospherePhaseScreenUncertainty`, `connectedComponents`. `{f}/pixelOffsets/{pol}/`: `alongTrackOffset`, `slantRangeOffset`, `correlationSurfacePeak` | – |
| GOFF | `{f}/pixelOffsets/{pol}/layer{n}/`: `alongTrackOffset`, `slantRangeOffset`, `correlationSurfacePeak`, `alongTrackOffsetVariance`, `slantRangeOffsetVariance`, `crossOffsetVariance` (one set per offset layer, three in the sample) | – |

GUNW's `connectedComponents` replaces the usual statistics with
`connectedComponentLabels`, `connectedComponentPercentages`,
`numValidConnectedComponents`, `percentPixelsInLargestCC` and
`percentPixelsWithNonZeroCC`.

Every product also has `QA/processing`, which records the QA software version
(`17.0.0` in the sample), its run time and its plotting settings, plus the
`sourceData/runConfigurationContents` the product was made with.

## NASA STAC catalog (CMR-STAC)

CMR serves the same collections as a STAC API:

```text
https://cmr.earthdata.nasa.gov/stac/ASF                                   # catalog
https://cmr.earthdata.nasa.gov/stac/ASF/collections/NISAR_L2_GUNW_PROVISIONAL_V1_1/items
https://cmr.earthdata.nasa.gov/stac/ASF/search?collections=NISAR_L2_GUNW_PROVISIONAL_V1_1&bbox=-125,32,-114,42&datetime=2026-10-01T00:00:00Z/..
```

`cloudstac` serves the same items for cloud-hosted collections:
`https://cmr.earthdata.nasa.gov/cloudstac/ASF`.

| Aspect | What the NISAR items carry (2026-10-21) |
|---|---|
| Item id | the granule name |
| Properties | `datetime`, `start_datetime`, `end_datetime`, and the `storage` extension, which records the S3 bucket `sds-n-cumulus-prod-nisar-products` in `us-west-2` |
| Assets | `data` (`.h5`, HTTPS and S3), `browse` and `thumbnail` (`_NATIVE.png`, `_LATLON.png`, `_LATLON_thumbnail.png`), `metadata` (the CMR XML record). RSLC and GSLC also list the per-polarization browse PNGs |
| Not in the items | track, frame, direction, polarization, mode, CRID, orbit type, coverage and every other attribute above, the QA files, and the KMLs |
| Search | STAC API core, item search, features, `fields`, `query`, `sort`, `context`, collection search; no `queryables` endpoint (404) |

So the STAC catalog finds granules by place, time and name, and hands back
their file links. Attribute filters such as track, frame or polarization still
need CMR's own search (`attribute[]=int,TRACK_NUMBER,117`) or the file name.

When asked for granules by id, CMR-STAC returned 30 of 30 sampled GUNWs that
CMR's UMM-JSON search never returns; see
[CMR UMM-JSON drops](cmr-umm-json-drops.md). Whether a paged STAC search also
returns them all was not tested.

## How nisar_db uses these

- **Catalogs:** the file name, through `nisar_db.filenames`.
- **Granule flags in the viewer:** CMR `JOINT_OBSERVATION`, `FULL_FRAME` and
  `ORBIT_TYPE`, plus `isMixedMode` and `isDithered` from `QA_STATS.h5`.
- **QA metrics in the viewer:** statistics from `QA_STATS.h5`.
- **QA images:** pulled out of `_QA_REPORT.pdf` by the QA helper. The helper
  handles GSLC and GUNW only.

[Viewer data sources](../data-sources.md) lists each flag and metric with its
exact path.
[Metadata for the viewer](viewer-metadata-candidates.md) lists what could
be added next.
