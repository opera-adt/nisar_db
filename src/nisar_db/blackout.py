"""Per-frame blackout / reference-date filtering and JSON writers.

A blackout period marks a [start, end] date range during which a frame's
acquisitions are excluded from the consistent-GSLC catalog (e.g. seasonal snow
cover). Reference-date changes mark epochs at which a frame's InSAR reference
resets. Both are stored as per-frame JSON keyed by ``frame_idx``.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import click
import pandas as pd

from .io_json import write_zipped_json


def is_excluded(
    frame_idx: str | int,
    check_date: date,
    blackout_periods: dict[str, list[list[str]]],
) -> bool:
    """Return True if ``check_date`` falls inside any blackout period for the frame."""
    for start_str, end_str in blackout_periods.get(str(frame_idx), []):
        start = datetime.fromisoformat(start_str).date()
        end = datetime.fromisoformat(end_str).date()
        if start <= check_date <= end:
            return True
    return False


def apply_blackout(
    df: pd.DataFrame,
    frame_idx_col: str,
    blackout_periods: dict[str, list[list[str]]],
) -> pd.DataFrame:
    """Remove rows whose sensing_date falls in a per-frame blackout period."""
    if not blackout_periods:
        return df
    mask = df.apply(
        lambda r: is_excluded(
            r[frame_idx_col],
            pd.Timestamp(r["sensing_date"]).date(),
            blackout_periods,
        ),
        axis=1,
    )
    n_removed = mask.sum()
    if n_removed:
        click.echo(f"  Blackout filter: removed {n_removed} acquisitions.")
    return df[~mask].reset_index(drop=True)


def create_blackout_dates_json(
    blackout_periods: dict[str | int, list[tuple[str, str]]],
    output: Path | None = None,
    description: str = "",
) -> Path:
    """Write a per-frame blackout-dates JSON.

    Parameters
    ----------
    blackout_periods:
        ``{frame_idx: [("YYYY-MM-DD", "YYYY-MM-DD"), ...]}``
        Each tuple is an inclusive [start, end] date range to exclude.
    output:
        Output path.  Defaults to ``nisar-blackout-dates-{today}.json``.
    description:
        Free-text note stored in ``metadata.description``.

    Returns
    -------
    Path to the written JSON.

    Example
    -------
    >>> periods = {
    ...     "5827": [("2025-12-01", "2026-01-15")],   # seasonal snow
    ...     "5830": [("2026-03-01", "2026-03-31")],
    ... }
    >>> create_blackout_dates_json(periods, output=Path("nisar-blackout.json"))

    """
    today = datetime.today().strftime("%Y-%m-%d")
    if output is None:
        output = Path(f"nisar-blackout-dates-{today}.json")

    # Normalise keys to strings, values to list-of-lists (JSON-serialisable)
    normalised = {
        str(k): [[str(s), str(e)] for s, e in v] for k, v in blackout_periods.items()
    }

    result = {
        "metadata": {
            "generation_time": datetime.today().isoformat(),
            "description": (
                description
                or (
                    "Per-frame NISAR blackout periods. "
                    "Acquisitions whose sensing_date falls in any [start, end] range "
                    "are excluded from the consistent-GSLC catalog."
                )
            ),
        },
        "blackout_dates": normalised,
    }

    zip_path = write_zipped_json(output, result)
    click.echo(f"Written: {output}  ({len(normalised)} frames with blackouts)")
    click.echo(f"Written: {zip_path}")
    return output


def create_reference_dates_json(
    reference_dates: dict[str | int, list[str]],
    output: Path | None = None,
    description: str = "",
) -> Path:
    """Write a per-frame reference-date change JSON.

    Analogous to burst_db's ``reference_dates.py`` output.  Lists the dates
    at which the InSAR reference epoch changes for each frame (e.g. after a
    large earthquake or a long data gap).

    Parameters
    ----------
    reference_dates:
        ``{frame_idx: ["YYYY-MM-DD", ...]}``
        Dates at which the reference epoch resets.  Frames not listed here
        use the default (first acquisition).
    output:
        Output path.  Defaults to ``nisar-reference-dates-{today}.json``.
    description:
        Free-text note stored in ``metadata.description``.

    Returns
    -------
    Path to the written JSON.

    Example
    -------
    >>> refs = {
    ...     "5827": ["2026-01-15"],   # reference reset after gap
    ...     "5830": ["2025-12-01", "2026-06-01"],
    ... }
    >>> create_reference_dates_json(refs, output=Path("nisar-reference-dates.json"))

    """
    today = datetime.today().strftime("%Y-%m-%d")
    if output is None:
        output = Path(f"nisar-reference-dates-{today}.json")

    normalised = {str(k): [str(d) for d in v] for k, v in reference_dates.items()}

    result = {
        "metadata": {
            "generation_time": datetime.today().isoformat(),
            "description": (
                description
                or (
                    "Per-frame NISAR reference date changes. "
                    "Each date marks a reset of the InSAR reference epoch "
                    "(e.g. after a major earthquake or a data gap)."
                )
            ),
        },
        "data": normalised,
    }

    zip_path = write_zipped_json(output, result)
    click.echo(f"Written: {output}  ({len(normalised)} frames with reference changes)")
    click.echo(f"Written: {zip_path}")
    return output
