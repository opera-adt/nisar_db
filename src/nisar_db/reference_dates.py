"""Derive per-frame InSAR reference (reset) dates for NISAR frames.

The NISAR counterpart of burst_db's ``reference_dates.py``.  Two strategies are
supported, mirroring the DISP-S1 workflow:

1. **Interval-based** (default) — read the consistent-GSLC JSON and place a new
   reference epoch roughly every ``interval_years``, provided at least
   ``min_acquisitions_per_batch`` acquisitions have accumulated since the last
   one.  Frames listed in :data:`EVENT_DATES_BY_FRAME` also reset on the given
   event date (e.g. a large earthquake), whether or not the interval has
   elapsed.
2. **Month-based** — when a blackout-dates JSON is supplied, reset on a
   snow-free month chosen from how heavily the frame is blacked out.  A frame
   that spends half the year under snow cannot carry a winter reference epoch.

Both strategies emit *acquisition* sensing times taken from the consistent-GSLC
JSON: a reference date always names an epoch the frame actually has data for.
The month-based rule therefore snaps each yearly anchor forward to the first
acquisition on or after it and stops at the end of the archive, rather than
projecting a calendar into years with no acquisitions.

Blacked-out acquisitions are kept out of reference dates by keeping them out of
the *stack*: :func:`nisar_db.consistent_gslc.make_consistent_gslc_json` applies
``--blackout-file`` when it builds the consistent-GSLC JSON, exactly as
``burst_db`` does.  This module only reads that stack; when a blackout file is
passed it re-checks the result via :func:`find_blacked_out_references` and fails
loudly if the stack it was given had not been filtered.

The dates are written by :func:`nisar_db.blackout.create_reference_dates_json`
as ``opera-nisar-disp-reference-dates-{date}.json[.zip]``.

Example:
-------
    nisar-db create-reference-dates \
        --consistent-json opera-nisar-disp-consistent-gslc-2026-07-25.json \
        --blackout-file opera-nisar-disp-blackout-dates-2026-07-25.json

"""

from __future__ import annotations

import json
import logging
from bisect import bisect_left
from datetime import datetime
from pathlib import Path

import click

from .blackout import create_reference_dates_json, is_excluded, load_blackout_json

logger = logging.getLogger(__name__)

__all__ = [
    "build_desired_month_map_from_blackout",
    "calculate_reference_dates",
    "find_blacked_out_references",
    "pick_month_based_on_snow",
]

#: Frames whose reference epoch must reset on a specific date regardless of the
#: acquisition count, keyed by ``frame_idx``.  Populated as deformation events
#: warrant it; NISAR's archive currently has none.
EVENT_DATES_BY_FRAME: dict[str, list[str]] = {}

#: Sensing times in the consistent-GSLC JSON carry no sub-second precision.
_TIME_FMT = "%Y-%m-%dT%H:%M:%S"


def load_consistent_json(path: str | Path) -> dict[str, dict]:
    """Read the ``data`` section of a consistent-GSLC JSON.

    Parameters
    ----------
    path : str or Path
        Path to ``opera-nisar-disp-consistent-gslc-*.json``.

    Returns
    -------
    dict
        ``{frame_idx: {"sensing_time_list": [...], ...}}``.

    """
    loaded = json.loads(Path(path).read_text())
    return loaded.get("data", loaded)


def pick_month_based_on_snow(num_blackouts: int) -> int:
    """Return the month (1-12) to anchor reference epochs on.

    More blackout windows means a longer snow season, so the safe window moves
    towards midsummer.

    Parameters
    ----------
    num_blackouts : int
        Number of blackout intervals recorded for the frame.

    Returns
    -------
    int
        Month number suitable for a reference acquisition.

    """
    if num_blackouts == 0:
        return 11
    if num_blackouts <= 5:
        return 9
    return 7


def build_desired_month_map_from_blackout(
    blackout_file: str | Path,
) -> dict[str, int]:
    """Map each frame to its snow-free reference month.

    Parameters
    ----------
    blackout_file : str or Path
        Blackout-dates JSON written by ``nisar-db create-blackout-dates``.

    Returns
    -------
    dict
        ``{frame_idx: month}`` for every frame present in the blackout file.

    """
    blackout_data = json.loads(Path(blackout_file).read_text())
    return {
        str(frame_idx): pick_month_based_on_snow(len(intervals))
        for frame_idx, intervals in blackout_data["blackout_dates"].items()
    }


def _sensing_times(frame_data: dict) -> list[datetime]:
    """Return a frame's acquisition times, sorted, so they can be bisected."""
    return sorted(
        datetime.strptime(t, _TIME_FMT) for t in frame_data["sensing_time_list"]
    )


def find_blacked_out_references(
    reference_dates: dict[str, list[str]],
    blackout_periods: dict[str, list[list[str]]],
) -> dict[str, list[str]]:
    """Return any reference dates that fall inside a frame's blackout window.

    Non-empty output means the consistent-GSLC JSON was built without
    ``--blackout-file``: the blackout filter belongs there, so a stack that
    still contains blacked-out acquisitions can hand one to a reference epoch.

    Parameters
    ----------
    reference_dates : dict
        ``{frame_idx: ["YYYY-MM-DDTHH:MM:SS", ...]}``.
    blackout_periods : dict
        ``{frame_idx: [[start, end], ...]}`` from the blackout-dates JSON.

    Returns
    -------
    dict
        The offending dates, keyed by ``frame_idx``; empty when the stack was
        filtered as expected.

    """
    offenders = {}
    for frame_idx, dates in reference_dates.items():
        blacked_out = [
            d
            for d in dates
            if is_excluded(
                frame_idx, datetime.strptime(d, _TIME_FMT).date(), blackout_periods
            )
        ]
        if blacked_out:
            offenders[frame_idx] = blacked_out
    return offenders


def _snap_to_yearly_anchors(
    sensing_times: list[datetime], month: int
) -> list[datetime]:
    """Return the acquisitions that open a new epoch on `month` each year.

    Each 1st-of-`month` anchor between the frame's first and last acquisition is
    moved forward to the first acquisition on or after it, so every returned
    date exists in the frame's stack.  Anchors that land on the same
    acquisition -- a frame whose only data arrives after several anchors have
    passed -- collapse to one reference.
    """
    references: list[datetime] = []
    for year in range(sensing_times[0].year, sensing_times[-1].year + 1):
        idx = bisect_left(sensing_times, datetime(year, month, 1))
        if idx == len(sensing_times):
            break
        acquisition = sensing_times[idx]
        if not references or acquisition != references[-1]:
            references.append(acquisition)
    return references


def _generate_month_based_dates(
    consistent_json_file: str | Path,
    desired_month_by_frame: dict[str, int],
) -> dict[str, list[str]]:
    """Reset each frame on the acquisition opening its snow-free month."""
    consistent_data = load_consistent_json(consistent_json_file)

    reference_dates: dict[str, list[str]] = {}
    n_no_acquisitions = 0
    n_never_reaches_month = 0
    for frame_idx, month in desired_month_by_frame.items():
        frame_data = consistent_data.get(str(frame_idx))
        sensing_times = _sensing_times(frame_data) if frame_data else []
        if not sensing_times:
            n_no_acquisitions += 1
            continue

        references = _snap_to_yearly_anchors(sensing_times, month)
        if not references:
            # The archive ends before the frame's first anchor month, so it
            # keeps its default reference until more data arrives.
            n_never_reaches_month += 1
            continue
        reference_dates[str(frame_idx)] = [d.strftime(_TIME_FMT) for d in references]

    logger.info(
        "Month-based references for %d frames (%d with no consistent "
        "acquisitions, %d whose archive ends before their anchor month)",
        len(reference_dates),
        n_no_acquisitions,
        n_never_reaches_month,
    )
    return reference_dates


def _generate_by_consistent(
    consistent_json_file: str | Path,
    interval_years: float,
    min_acquisitions_per_batch: int,
) -> dict[str, list[str]]:
    """Place reference epochs along each frame's actual acquisition history."""
    consistent_data = load_consistent_json(consistent_json_file)
    interval_days = int(interval_years * 365.25)

    reference_dates: dict[str, list[str]] = {}
    for frame_idx, frame_data in consistent_data.items():
        sensing_times = _sensing_times(frame_data)
        if not sensing_times:
            continue
        event_dates = {
            datetime.strptime(d, "%Y-%m-%d").date()
            for d in EVENT_DATES_BY_FRAME.get(str(frame_idx), [])
        }

        ref_dates: list[datetime] = []
        n_since_last_ref = 0
        for date in sensing_times:
            if not ref_dates:
                ref_dates.append(date)
                n_since_last_ref = 1
                continue

            n_since_last_ref += 1
            elapsed = (date - ref_dates[0]).days
            is_interval_passed = elapsed >= len(ref_dates) * interval_days
            is_event_date = date.date() in event_dates

            if not (is_interval_passed or is_event_date):
                continue

            if n_since_last_ref >= min_acquisitions_per_batch:
                ref_dates.append(date)
                n_since_last_ref = 0
            elif is_event_date:
                # An event invalidates the epoch even without a full batch:
                # move the open reference rather than opening a new short one.
                ref_dates[-1] = date
                n_since_last_ref = 0

        if 0 < n_since_last_ref < min_acquisitions_per_batch:
            logger.debug(
                "Frame %s has only %d acquisitions in its final batch",
                frame_idx,
                n_since_last_ref,
            )

        reference_dates[str(frame_idx)] = [d.strftime(_TIME_FMT) for d in ref_dates]

    return reference_dates


def calculate_reference_dates(
    consistent_json_file: str | Path | None = None,
    desired_month_by_frame: dict[str, int] | None = None,
    interval_years: float = 1.0,
    min_acquisitions_per_batch: int = 15,
) -> dict[str, list[str]]:
    """Return the reference-epoch reset dates for every NISAR frame.

    Every returned date is one of the frame's own sensing times, so a reference
    epoch always names an acquisition the processor will actually have.  Build
    the consistent-GSLC JSON with ``--blackout-file`` to keep blacked-out
    acquisitions out of that stack, and hence out of these dates.

    Parameters
    ----------
    consistent_json_file : str or Path
        Consistent-GSLC JSON to read acquisition histories from.  Required by
        both strategies.
    desired_month_by_frame : dict, optional
        ``{frame_idx: month}``.  When non-empty, the month-based strategy is
        used: each frame resets on the first acquisition on or after the 1st of
        `month`, once per year, for as long as the frame has data.  Frames
        absent from the consistent-GSLC JSON are dropped.
    interval_years : float
        Nominal spacing between reference epochs (interval-based strategy).
    min_acquisitions_per_batch : int
        Minimum acquisitions that must accumulate before a new epoch opens.

    Returns
    -------
    dict
        ``{frame_idx: ["YYYY-MM-DDTHH:MM:SS", ...]}``.

    Raises
    ------
    ValueError
        If no consistent-GSLC JSON is supplied.

    Example
    -------
    >>> calculate_reference_dates(  # doctest: +SKIP
    ...     "consistent-gslc.json", desired_month_by_frame={"5827": 7}
    ... )
    {'5827': ['2025-07-08T01:23:45', ...]}

    """
    if not consistent_json_file:
        raise ValueError(
            "Pass --consistent-json: reference dates are acquisition times taken "
            "from the consistent-GSLC JSON, so there is nothing to derive them "
            "from without it."
        )
    if desired_month_by_frame:
        return _generate_month_based_dates(consistent_json_file, desired_month_by_frame)
    return _generate_by_consistent(
        consistent_json_file,
        interval_years=interval_years,
        min_acquisitions_per_batch=min_acquisitions_per_batch,
    )


@click.command(name="create-reference-dates", context_settings={"show_default": True})
@click.option(
    "--consistent-json",
    "consistent_json_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Consistent-GSLC JSON supplying the acquisition times to reset on.",
)
@click.option(
    "--blackout-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Blackout-dates JSON. If given, use snow-free month-based references.",
)
@click.option(
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Output JSON path (default: nisar-reference-dates-{today}.json).",
)
@click.option(
    "--interval",
    type=float,
    default=1.0,
    help="Nominal interval between reference dates, in years.",
)
@click.option(
    "--min-acquisitions",
    type=int,
    default=15,
    help="Minimum acquisitions required before a new reference epoch opens.",
)
def main(
    consistent_json_file: Path | None,
    blackout_file: Path | None,
    output: Path | None,
    interval: float,
    min_acquisitions: int,
) -> None:
    """Create the NISAR reference-dates JSON."""
    if not consistent_json_file:
        raise click.UsageError(
            "--consistent-json is required: reference dates are acquisition "
            "times read from it."
        )

    desired_month_by_frame = None
    if blackout_file:
        desired_month_by_frame = build_desired_month_map_from_blackout(blackout_file)
        click.echo(
            f"Using {blackout_file} to pick a snow-free reference month for "
            f"{len(desired_month_by_frame)} frames"
        )

    reference_dates = calculate_reference_dates(
        consistent_json_file=consistent_json_file,
        desired_month_by_frame=desired_month_by_frame,
        interval_years=interval,
        min_acquisitions_per_batch=min_acquisitions,
    )

    if blackout_file:
        offenders = find_blacked_out_references(
            reference_dates, load_blackout_json(blackout_file)["blackout_dates"]
        )
        if offenders:
            sample = list(offenders.items())[:5]
            raise click.ClickException(
                f"{len(offenders)} frames got a reference date inside a blackout "
                f"window, e.g. {sample}. The blackout filter belongs to the "
                f"consistent-GSLC stack -- rebuild it with "
                f"'nisar-db make-consistent-gslc --blackout-file {blackout_file}' "
                f"and derive the reference dates from that."
            )

    create_reference_dates_json(
        reference_dates,
        output=output,
        extra_metadata={
            "consistent_json_file": (
                str(consistent_json_file) if consistent_json_file else None
            ),
            "blackout_file": str(blackout_file) if blackout_file else None,
            "strategy": "month-based" if desired_month_by_frame else "interval-based",
            "interval_years": interval,
            "min_acquisitions": min_acquisitions,
        },
    )


if __name__ == "__main__":
    main()
