# NISAR snow analysis

The upstream half of the blackout-dates pipeline: turn NOAA GEFS snow and
temperature fields into the per-frame window table that
`nisar-db create-blackout-dates` consumes.

**Full guide: [Tutorial: derive blackout dates from snow cover](../../docs/tutorials/snow-analysis.md)**
(rendered in the docs site under Tutorials). Background on the windows
themselves: [`docs/background/blackout-dates.md`](../../docs/background/blackout-dates.md).

These scripts live here rather than in the package because they pull a weather
archive and a plotting stack that `nisar_db` itself does not depend on. They
mirror `burst_db`'s `snow-analysis/` for DISP-S1, keyed on NISAR `frame_idx`
instead of the DISP-S1 `frame_id`.

| File | Role |
|---|---|
| `fetch_gefs.py` | Subset the remote NOAA GEFS analysis Zarr to a local store |
| `snow_month_filter.py` | The logic: aggregate weather, flag bad weeks, collapse them into per-frame windows |
| `derive_blackout_windows.py` | CLI wrapping the above into the snow-analysis GeoJSON/Parquet |
| `transfer_disp_s1_windows.py` | Shortcut: resample `burst_db`'s DISP-S1 windows onto NISAR frames, no weather download |
| `summarize_blackout_difference.py` | Report what the blackout filter cost a consistent-GSLC database |

## Quick start

```bash
pip install -r requirements.txt

python fetch_gefs.py --email you@example.com --outfile noaa_gefs.zarr

python derive_blackout_windows.py \
    --gefs-zarr noaa_gefs.zarr \
    --frames-gpkg ../../notebooks/opera-nisar-disp-frames.gpkg \
    --outfile nisar-snow-analysis.geojson

nisar-db create-blackout-dates \
    --input-file nisar-snow-analysis.geojson \
    --output-file nisar-blackout-dates.json
```

## Shortcut: skip the weather download

`transfer_disp_s1_windows.py` builds the same table out of the windows
`burst_db` already published for DISP-S1, resampled onto NISAR frames by
spatial overlap. Only `geopandas` is needed, and it runs in seconds:

```bash
python transfer_disp_s1_windows.py \
    --disp-s1-table https://raw.githubusercontent.com/opera-adt/burst_db/main/snow-analysis/opera-region4-snow-analysis.geojson \
    --frames-gpkg ../../notebooks/opera-nisar-disp-frames.gpkg \
    --outfile nisar-snow-analysis-from-disp-s1.geojson
```

NISAR frames outside the DISP-S1 frame set come out with no window, and the
windows are smoothed over the donors of each frame. See the
[tutorial](../../docs/tutorials/snow-analysis.md) for when that is good enough.

Every script takes `--help`; the tutorial explains the thresholds, the three
window strategies (conservative / median / aggressive), and how to check the
cost of the filter.
