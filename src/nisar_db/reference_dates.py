"""Derive per-frame InSAR reference (reset) dates for NISAR frames.

The NISAR counterpart of burst_db's ``reference_dates.py``.  Two strategies are
supported, mirroring the DISP-S1 workflow:

1. **Interval-based** (default) — read the consistent-GSLC JSON and place a new
   reference epoch roughly every ``interval_years``, provided at least
   ``min_acquisitions_per_batch`` acquisitions have accumulated since the last
   one.  Frames listed in :data:`EVENT_DATES_BY_FRAME` also reset on the given
   event date (e.g. a large earthquake), whether or not the interval has
   elapsed.
2. **Month-based** — when a blackout-dates JSON is supplied, ignore the
   acquisition history and reset on the 1st of a snow-free month chosen from
   how heavily the frame is blacked out.  A frame that spends half the year
   under snow cannot carry a winter reference epoch.

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
from datetime import datetime
from pathlib import Path

import click

from .blackout import create_reference_dates_json

logger = logging.getLogger(__name__)

__all__ = [
    "build_desired_month_map_from_blackout",
    "calculate_reference_dates",
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


def _generate_month_based_dates(
    desired_month_by_frame: dict[str, int],
    start_year: int,
    end_year: int,
) -> dict[str, list[str]]:
    """Emit one reference date per year on the 1st of each frame's month."""
    return {
        str(frame_idx): [
            datetime(year, month, 1).strftime(_TIME_FMT)
            for year in range(start_year, end_year)
        ]
        for frame_idx, month in desired_month_by_frame.items()
    }


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
        sensing_times = [
            datetime.strptime(t, _TIME_FMT) for t in frame_data["sensing_time_list"]
        ]
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
    start_year: int = 2025,
    end_year: int = 2035,
) -> dict[str, list[str]]:
    """Return the reference-epoch reset dates for every NISAR frame.

    Parameters
    ----------
    consistent_json_file : str or Path, optional
        Consistent-GSLC JSON to read acquisition histories from.  Required
        unless `desired_month_by_frame` is given.
    desired_month_by_frame : dict, optional
        ``{frame_idx: month}``.  When non-empty, the month-based strategy is
        used and `consistent_json_file` is ignored.
    interval_years : float
        Nominal spacing between reference epochs (interval-based strategy).
    min_acquisitions_per_batch : int
        Minimum acquisitions that must accumulate before a new epoch opens.
    start_year, end_year : int
        Year range covered by the month-based strategy.  NISAR science
        operations began in 2025.

    Returns
    -------
    dict
        ``{frame_idx: ["YYYY-MM-DDTHH:MM:SS", ...]}``.

    Raises
    ------
    ValueError
        If neither a consistent-GSLC JSON nor a month map is supplied.

    Example
    -------
    >>> calculate_reference_dates(desired_month_by_frame={"5827": 7})
    {'5827': ['2025-07-01T00:00:00', ...]}

    """
    if desired_month_by_frame:
        return _generate_month_based_dates(
            desired_month_by_frame, start_year=start_year, end_year=end_year
        )
    if not consistent_json_file:
        raise ValueError(
            "Pass --consistent-json (acquisition-based) or --blackout-file "
            "(month-based); neither was given, so there is nothing to derive "
            "reference dates from."
        )
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
    help="Consistent-GSLC JSON to derive reference epochs from.",
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
    if not (consistent_json_file or blackout_file):
        raise click.UsageError(
            "Pass --consistent-json (interval-based) or --blackout-file "
            "(month-based); neither was given."
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
