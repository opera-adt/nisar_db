# Background: reference (reset) dates

DISP-NISAR measures displacement *relative to a reference epoch*. If a single
reference were used forever, the processor would keep forming interferograms with
an ever-growing temporal baseline against the very first acquisition — which
decorrelates and becomes unusable. To avoid this, each frame periodically
**resets** its reference to a newer date and starts a fresh batch.

A **reference-date change** (also called a *reset date*) is one of those epochs.
The reference-dates database lists, per frame, the dates at which the InSAR
reference is reset. Frames not listed simply keep their default reference (the
first acquisition).

This mirrors `burst_db`'s `opera-disp-s1-reference-dates-*.json` and its
`opera-db make-reference-dates` step. As the Sentinel-1 documentation puts it,
the reference-date list *"indicates to the processing system that we should start
outputting data with respect to a new reference, to avoid attempting to form very
long temporal-baseline interferograms."* NISAR follows the same convention.

## Why a reset happens

Two operational triggers, both inherited from the Sentinel-1 workflow:

- **Batch length** — after enough acquisitions accumulate (`burst_db` groups
  Sentinel-1 in batches of ~15), a new reference caps interferogram baselines to
  a manageable span.
- **Deformation events / data gaps** — a large earthquake or a long acquisition
  gap makes pre-event/post-event pairs meaningless, so the reference is reset at
  the event. In `burst_db` this is the `EVENT_DATES_BY_FRAME` table (e.g.
  Ridgecrest, `2019-07-06`); NISAR carries per-frame event dates the same way.

## Where this lives in the code

| Piece | Location |
|---|---|
| Derivation + CLI | [`reference_dates.py`](https://github.com/opera-adt/nisar_db/blob/main/src/nisar_db/reference_dates.py) |
| JSON writer | `create_reference_dates_json` in [`blackout.py`](https://github.com/opera-adt/nisar_db/blob/main/src/nisar_db/blackout.py) |
| Re-exported for back-compat | [`consistent_gslc.py`](https://github.com/opera-adt/nisar_db/blob/main/src/nisar_db/consistent_gslc.py) |
| Sentinel-1 reference (for parity) | `burst_db`'s [`reference_dates.py`](https://github.com/opera-adt/burst_db/blob/main/src/burst_db/reference_dates.py) |

## Every reference date is a real acquisition

Both rules emit sensing times drawn from the [consistent-GSLC
stack](consistent-mode.md): a reference date always names an acquisition the
frame actually has. The processor snaps a reset to the first acquisition on or
after the requested date anyway (`_compute_reference_dates` in `disp_s1`), so a
date with no data behind it is silently skipped -- which makes it invisible when
a frame's reference never resets as intended. Emitting acquisition times keeps
the database self-checking: the list *is* the set of epochs the processor will
use, and it stops at the end of the archive instead of projecting a calendar
into years that have no acquisitions.

Consequently `--consistent-json` is required for both rules, and a frame with no
consistent acquisitions is dropped rather than given dates it cannot honour.

This is also what keeps blacked-out dates out of the reference list, and it is
why there is no blackout filter in `reference_dates.py`. The blackout is applied
once, upstream, when the consistent-GSLC stack is built:

```bash
nisar-db make-consistent-gslc ... --blackout-file nisar-blackout-dates.json
```

A stack built that way contains no blacked-out acquisitions, so no rule reading
it can pick one -- and, just as importantly, a blacked-out acquisition cannot
count towards the `--min-acquisitions` that opens the next epoch. `burst_db`
draws the line in the same place (`create_cslc_burst_catalog.py` filters; its
`reference_dates.py` does not). Check `metadata.blackout_file` in the consistent
JSON to confirm which stack you have; if it is `null`, the blackout was never
applied.

As a backstop, passing `--blackout-file` to `create-reference-dates` re-checks
the result with `find_blacked_out_references` and fails if any reference date
landed in a window -- the symptom of deriving dates from an unfiltered stack.

## The JSON shape

Per-frame, keyed by `frame_idx`; each value lists the reset dates in order:

```json
{
  "metadata": {
    "generation_time": "2026-07-24T12:00:00",
    "description": "Per-frame NISAR reference date changes. Each date marks a reset of the InSAR reference epoch (e.g. after a major earthquake or a data gap)."
  },
  "data": {
    "5827": ["2026-07-08T06:31:00"],
    "5830": ["2025-09-14T06:31:02", "2026-09-08T06:31:01"]
  }
}
```

## Producing it

`nisar-db create-reference-dates` derives the dates and writes the JSON plus its
`.json.zip`. Which of the two rules applies depends on whether a blackout file
is supplied:

```bash
# Month-based: anchor each frame on a snow-free month
nisar-db create-reference-dates \
  --consistent-json opera-nisar-disp-consistent-gslc-2026-07-25.json \
  --blackout-file opera-nisar-disp-blackout-dates-2026-07-25.json \
  --output opera-nisar-disp-reference-dates-2026-07-25.json

# Interval-based: follow each frame's own acquisition history
nisar-db create-reference-dates \
  --consistent-json opera-nisar-disp-consistent-gslc-2026-07-25.json \
  --interval 1.0 --min-acquisitions 15
```

In both cases the consistent JSON should already be blackout-filtered; see
above.

The month-based rule maps a frame's blackout count onto a reference month --
none to November, up to five windows to September, more than that to July --
because a frame that spends half the year under snow cannot carry a winter
reference. It then takes, once per year, the first acquisition on or after the
1st of that month; years whose anchor falls past the last acquisition contribute
nothing. The interval-based rule opens a new epoch once `--interval` years
have passed *and* `--min-acquisitions` acquisitions have accumulated, so sparse
frames keep a single reference rather than a string of unusably short batches.
Frames listed in `EVENT_DATES_BY_FRAME` reset on their event date regardless.

Note the month-based rule has no `--min-acquisitions` equivalent: it resets every
year the frame has data past its anchor month, however few acquisitions came in
between.

```mermaid
flowchart LR
    F["Blackout dates"] --> H["make-consistent-gslc<br/>--blackout-file"]
    H --> A["Consistent-mode stack<br/>(per frame, sorted dates)"]
    A --> B["reset rule<br/>batch length / events<br/>or snow-free month anchor"]
    B --> G["snap to first acquisition<br/>on or after the reset"]
    G --> C["{frame_idx: [reset dates]}"]
    C --> D["create_reference_dates_json"]
    D --> E["opera-nisar-disp-reference-dates-*.json"]
```

The writer is also usable directly when the dates come from elsewhere:

```python
from nisar_db.blackout import create_reference_dates_json

refs = {
    "5827": ["2026-01-15"],           # reference reset after a data gap
    "5830": ["2025-12-01", "2026-06-01"],
}
create_reference_dates_json(refs, output="nisar-reference-dates.json")
```

## How the processor uses it

The three databases work together and are all keyed by the same `frame_idx`:

1. **[Consistent mode](consistent-mode.md)** fixes *which* acquisitions form the
   stack for a frame.
2. **[Blackout dates](blackout-dates.md)** removes seasonally unusable
   acquisitions from that stack.
3. **Reference dates** tell the processor *when to restart* the reference within
   the surviving stack.

Together they define, for every North America frame, a clean sequence of
acquisitions partitioned into reference-anchored batches — ready for DISP-NISAR
displacement processing.
