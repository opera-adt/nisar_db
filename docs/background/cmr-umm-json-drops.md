# Background: CMR's UMM-JSON results are incomplete and mislabelled

Since October 2026, NASA's Common Metadata Repository (CMR) returns unreliable
`granules.umm_json` search results. It leaves some granules out while still
counting them in the `CMR-Hits` header, and under other concept ids it serves
a different granule's record. The same search in atom-JSON (`granules.json`)
returns every granule under its own name. The bug is on the CMR side and also
affects other ASF collections, such as `SENTINEL-1_BURSTS` (`C2709161906-ASF`),
which is how Vertex comes to show existing bursts as missing.

nisar_db works around it. Every UMM-JSON search is checked against the
atom-JSON listing, and every record that is missing or carries the wrong name
is fetched again by concept id.

## Three ways a record goes wrong

| Failure | What UMM-JSON does | What still returns the right record |
|---|---|---|
| Paging skip | Leaves the granule out of a paged search, but returns it when asked for its concept id | UMM-JSON with `concept_id[]` |
| Never returned | Counts the granule in `CMR-Hits` but returns zero records, however it is asked | `search/concepts/<id>` (native UMM-G), atom-JSON |
| Mislabelled | Returns a record under the granule's concept id, but the record is another granule's, so that other granule appears twice | `search/concepts/<id>`, atom-JSON |

For example, concept id `G4246696294-ASF` is
`NISAR_L2_PR_GUNW_023_055_D_066_...` in atom-JSON and from the concepts
endpoint. UMM-JSON serves the record of
`NISAR_L2_PR_GUNW_023_059_A_038_...` under it. A list keyed by granule name
then holds track 059 frame 038 twice and has no track 055 frame 066.

`echo10` is not a reliable fallback: in a batch search it returned only 403 of
3,607 never-returned NISAR granules. The concepts endpoint returned the right
record in every case checked.

## Scope

Full-archive walks on 2026-10-21. The counts changed from one run to the next
during the day, so treat them as a snapshot.

| Collection | Granules | Left out of UMM-JSON | Mislabelled in UMM-JSON |
|---|---|---|---|
| `NISAR_L2_GUNW_PROVISIONAL_V1` | 89,911 | 6,024 | 3,470 to 12,600 |
| `NISAR_L2_GSLC_PROVISIONAL_V1` | 155,910 | 197 | 13,769 |
| `NISAR_L2_GSLC_BETA_V1` | 23,450 | 0 | not checked |

The dropped GUNWs are mostly composite release `P05023`. Of the 602 dropped
GUNWs already in the viewer's flag cache, 597 had an orbit type from an
earlier CMR lookup, so the problem appears recent.

CMR can also list one granule name under two concept ids, which is a
duplicate in CMR itself. None were found in the atom-JSON listings on
2026-10-21, but the search checks for them on every run.

## What nisar_db does about it

| Path | Endpoint | Status |
|---|---|---|
| Catalog builders (`create_gslc_catalog`, `create_gunw_catalog`) | `granules.json` | Never affected |
| `nisar-db search`, `search_nisar_products`, the weekly viewer's GSLC list, local search-and-rebuild | `granules.umm_json` | Recovered by `fetch_umm_granules` |
| QA orbit types (`scripts/collect_granule_qa.py`) | `granules.umm_json` by `granule_ur[]` | Recovered by `fetch_umm_by_granule_ur`; cached flags missing an orbit type are backfilled on the next run |

The recovery in [`search/cmr.py`](https://github.com/opera-adt/nisar_db/blob/main/src/nisar_db/search/cmr.py) runs in four steps:

1. Walk the search in UMM-JSON and in atom-JSON. Compare each UMM-JSON
   record's `GranuleUR` with the atom-JSON title of the same concept id, and
   drop the records whose names disagree.
2. Ask UMM-JSON for the missing and mislabelled ids, 100 per POST, and keep
   only the records whose names now agree. This recovers the paging skips.
3. Fetch whatever is still missing from `search/concepts/<id>`, one granule
   per request, with 4 workers. This recovers the never-returned and
   mislabelled granules.
4. Keep one record per granule name. When CMR lists a name under several
   concept ids, keep the most recently ingested one, which has the highest
   concept id.

The page walk also no longer ends at a short page while `CMR-Hits` says more
remain, and it warns whenever it comes up short of `CMR-Hits`. Before this
change, a page shortened by drops ended the walk and lost every later page.

## Reporting what CMR got wrong

The search keeps one record per granule, but it does not hide CMR's mistakes.
`CMRSearchReport` collects them for a report to CMR, and every search also
logs them as warnings.

| Field | Content |
|---|---|
| `left_out` | Concept ids UMM-JSON did not return |
| `mislabelled` | Concept id, and the other granule's name UMM-JSON served under it |
| `duplicate_names` | Granule name, and every concept id CMR lists under it |
| `unrecovered` | Concept ids no endpoint returned, usually deleted mid-search |

Write it from the command line with `--cmr-report`:

```bash
nisar-db search --product-type GUNW --max-results 0 --cmr-report gunw_cmr_report.json
```

A local viewer build that saves its catalog (see below) stores the GSLC and
GUNW reports in the catalog's `catalog.json`.

## Runtime cost

The extra cost is the atom-JSON listing, which every search pays, plus one
concepts-endpoint request per missing or mislabelled granule. It therefore
grows with how badly CMR is misbehaving.

| Search on 2026-10-21 | Concepts-endpoint lookups | Time |
|---|---|---|
| Full GUNW archive, before the fix (estimated) | none | ~1.5 min |
| Full GUNW archive, morning | 3,603 | 5.1 min |
| Full GSLC archive, before the fix (estimated) | none | ~2.7 min |
| Full GSLC archive, morning, before the name check | 4 | 4.1 min |
| Global viewer build (GSLC and GUNW), afternoon | ~32,000 | 33.7 min |

"Before the fix" is the UMM-JSON walk plus parsing from the same runs, which
is all the old code did. A global build spends most of its time on the
concepts endpoint. A local search-and-rebuild over one box stays small. If the
cost matters, the atom-JSON listing could run at the same time as the
UMM-JSON walk, and the concepts endpoint could take more workers, which CMR
may rate-limit.

## Building the viewer without CMR

A global search now takes half an hour, so a local build can save what it
found and later builds can reuse it:

```bash
# Search CMR once and save the catalog next to the page
python scripts/build_local_view.py --scope globe \
    --output .qa_helper_cache/views/globe-20261021T190000.html \
    --save-catalog .qa_helper_cache/catalogs/globe-20261021T190000

# Rebuild any scope from it, with no CMR search
python scripts/build_local_view.py --scope na --output na.html \
    --catalog .qa_helper_cache/catalogs/globe-20261021T190000

# Or have the viewer's own search button build from it
python scripts/qa_browse_server.py --cache-dir .qa_helper_cache \
    --catalog-dir .qa_helper_cache/catalogs/globe-20261021T190000
```

The page shows when its catalog was searched. A catalog saved from a box
only serves pages inside that box.

## How to check one granule

A left-out granule shows one hit and no records in UMM-JSON, and comes back
from the concepts endpoint:

```bash
id=G4245361810-ASF   # a dropped P05023 GUNW
curl -s "https://cmr.earthdata.nasa.gov/search/granules.umm_json?concept_id=$id"
# {"hits":1,"took":34,"items":[]}
curl -s "https://cmr.earthdata.nasa.gov/search/granules.json?concept_id=$id" | head -c 200
curl -s "https://cmr.earthdata.nasa.gov/search/concepts/$id" | head -c 200
```

## When to remove the workaround

Keep it after CMR fixes the bug. When nothing is wrong, it costs only the
atom-JSON listing, and it catches the bug if it comes back. The warnings
`CMR UMM-JSON left out or mislabelled N of M granules` and
`CMR UMM-JSON served another granule's record` in a search's log show whether
the bug is still present.
