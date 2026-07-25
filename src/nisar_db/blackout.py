"""Per-frame blackout / reference-date filtering and JSON writers.

A blackout period marks a [start, end] date range during which a frame's
acquisitions are excluded from the consistent-GSLC catalog (e.g. seasonal snow
cover). Reference-date changes mark epochs at which a frame's InSAR reference
resets. Both are stored as per-frame JSON keyed by ``frame_idx``.
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

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


def _parse_boundary(value: str, *, end_of_day: bool = False) -> datetime:
    """Parse an ISO date/datetime boundary of a blackout window.

    A bare ``YYYY-MM-DD`` end boundary is pushed to ``23:59:59`` so the window
    stays inclusive of its last day, matching the windows written by
    :mod:`nisar_db.catalog.create_blackout_dates`.
    """
    parsed = datetime.fromisoformat(value)
    if end_of_day and ":" not in value:
        return datetime.combine(parsed.date(), time(23, 59, 59))
    return parsed


def normalize_period(start: str, end: str) -> list[str]:
    """Return ``[start, end]`` as ISO strings, with an inclusive end of day.

    Raises:
    ------
    ValueError
        If ``end`` precedes ``start``.

    Example:
    -------
    >>> normalize_period("2025-11-01", "2026-05-31")
    ['2025-11-01T00:00:00', '2026-05-31T23:59:59']

    """
    start_dt = _parse_boundary(start)
    end_dt = _parse_boundary(end, end_of_day=True)
    if end_dt < start_dt:
        raise ValueError(f"Blackout end {end!r} precedes start {start!r}")
    return [start_dt.isoformat(), end_dt.isoformat()]


def append_blackout_period(
    blackout_dates: dict[str, list[list[str]]],
    frame_idx: str | int,
    start: str,
    end: str,
) -> bool:
    """Add one ``[start, end]`` window to ``frame_idx`` in ``blackout_dates``.

    Mutates ``blackout_dates`` in place, creating the frame entry if needed and
    keeping each frame's windows sorted by start date. An identical window that
    is already present is not duplicated.

    Parameters
    ----------
    blackout_dates:
        ``{frame_idx: [[start, end], ...]}`` mapping, modified in place.
    frame_idx:
        Frame the window applies to; coerced to ``str`` to match the JSON keys.
    start, end:
        Inclusive window bounds as ISO dates or datetimes.

    Returns
    -------
    True if the window was added, False if it was already present.

    """
    period = normalize_period(start, end)
    key = str(frame_idx)
    periods = blackout_dates.setdefault(key, [])
    if period in [list(p) for p in periods]:
        return False
    periods.append(period)
    periods.sort(key=lambda p: p[0])
    return True


def load_blackout_json(path: str | Path) -> dict:
    """Read a blackout-dates document from a ``.json`` or ``.json.zip`` file."""
    path = Path(path)
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            doc = json.loads(zf.read(zf.namelist()[0]))
    else:
        doc = json.loads(path.read_text())

    if "blackout_dates" not in doc:
        raise ValueError(f"{path} has no 'blackout_dates' key; not a blackout JSON")
    return doc


def append_blackout_dates_json(
    json_file: str | Path,
    frame_idx: str | int,
    periods: Iterable[Sequence[str]],
    output: str | Path | None = None,
    create: bool = False,
    write_zip: bool = True,
) -> Path:
    """Append manual blackout windows for one frame to a blackout-dates JSON.

    Parameters
    ----------
    json_file:
        Existing blackout-dates JSON (``.json`` or ``.json.zip``) to extend.
    frame_idx:
        Frame the windows apply to.
    periods:
        Iterable of ``(start, end)`` pairs, ISO dates or datetimes.
    output:
        Where to write the result. Defaults to ``json_file`` (edit in place);
        a ``.json.zip`` input defaults to the matching ``.json``.
    create:
        Start from an empty document when ``json_file`` does not exist.
    write_zip:
        Also refresh the ``<output>.zip`` sidecar (default True).

    Returns
    -------
    Path to the written JSON.

    Example
    -------
    >>> append_blackout_dates_json(
    ...     "nisar-blackout-dates.json", 5827, [("2025-11-01", "2026-05-31")]
    ... )

    """
    json_file = Path(json_file)
    if json_file.exists():
        doc = load_blackout_json(json_file)
    elif create:
        doc = {"metadata": {"type": "manual"}, "blackout_dates": {}}
    else:
        raise FileNotFoundError(
            f"{json_file} does not exist (pass create=True to make it)"
        )

    if output is None:
        output = json_file.with_suffix("") if json_file.suffix == ".zip" else json_file
    output = Path(output)

    blackout_dates = doc["blackout_dates"]
    added: list[list[str]] = []
    for start, end in periods:
        if append_blackout_period(blackout_dates, frame_idx, start, end):
            added.append(normalize_period(start, end))
        else:
            click.echo(f"  Already present, skipped: [{start}, {end}]")

    if added:
        metadata = doc.setdefault("metadata", {})
        metadata["last_modified"] = datetime.now().isoformat()
        metadata.setdefault("manual_edits", []).append(
            {
                "time": metadata["last_modified"],
                "frame_idx": str(frame_idx),
                "periods": added,
            }
        )

    if write_zip:
        zip_path = write_zipped_json(output, doc)
        click.echo(f"Written: {zip_path}")
    else:
        with open(output, "w") as f:
            json.dump(doc, f, indent=2, default=str)

    n_frame = len(blackout_dates[str(frame_idx)])
    click.echo(
        f"Frame {frame_idx}: added {len(added)} window(s), {n_frame} total. "
        f"Written: {output}"
    )
    return output


def create_reference_dates_json(
    reference_dates: Mapping[Any, list[str]],
    output: Path | None = None,
    description: str = "",
    extra_metadata: dict | None = None,
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
    extra_metadata:
        Additional key/value pairs merged into ``metadata`` (e.g. the inputs
        and parameters the dates were derived from).

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
            **(extra_metadata or {}),
        },
        "data": normalised,
    }

    zip_path = write_zipped_json(output, result)
    click.echo(f"Written: {output}  ({len(normalised)} frames with reference changes)")
    click.echo(f"Written: {zip_path}")
    return output


@click.command(name="append-blackout-dates")
@click.option("--frame", "frame_idx", required=True, help="Frame index to extend")
@click.option(
    "--period",
    "periods",
    required=True,
    multiple=True,
    nargs=2,
    metavar="START END",
    help="Inclusive blackout window, ISO dates/datetimes. Repeatable.",
)
@click.option(
    "--json-file",
    "json_file",
    required=True,
    type=click.Path(path_type=Path),
    help="Blackout-dates JSON to append to (.json or .json.zip)",
)
@click.option(
    "--output",
    type=click.Path(path_type=Path),
    help="Write here instead of editing --json-file in place",
)
@click.option(
    "--create", is_flag=True, help="Create --json-file if it does not exist yet"
)
@click.option("--no-zip", is_flag=True, help="Skip refreshing the .json.zip sidecar")
def main(
    frame_idx: str,
    periods: tuple[tuple[str, str], ...],
    json_file: Path,
    output: Path | None,
    create: bool,
    no_zip: bool,
) -> None:
    """Manually append blackout windows for a single frame.

    Windows are inclusive; a bare end date covers that whole day.

    \b
    Example:
      nisar-db append-blackout-dates --frame 5827 \\
        --period 2025-11-01 2026-05-31 \\
        --period 2026-11-01 2027-05-31 \\
        --json-file nisar-blackout-dates.json
    """  # noqa: D301
    append_blackout_dates_json(
        json_file,
        frame_idx,
        periods,
        output=output,
        create=create,
        write_zip=not no_zip,
    )
