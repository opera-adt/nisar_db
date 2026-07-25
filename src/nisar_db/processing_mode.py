"""Label consistent-GSLC sensing times as historical / forward / no_run.

The NISAR counterpart of burst_db's ``reconcile_and_label_db.py``.  It answers
two questions the consistent-GSLC JSON alone does not:

1. **Which acquisitions can be processed as a complete historical batch?**
   Acquisitions are grouped in batches of ``batch_size``; full batches are
   ``historical``, the trailing partial batch is ``forward`` (it will be
   reprocessed as new data arrives), and a frame that has never accumulated a
   full batch is ``no_run``.  A gap longer than ``gap_threshold_years`` starts
   a new numbered group, since the stack cannot span it.

2. **Which frames changed definition since the last release?**  For NISAR the
   frame footprint is fixed, so the thing that invalidates an existing stack is
   the winning ``(common_mode, common_coverage)`` flipping.  Comparing against
   the previous release surfaces exactly those frames.

Example:
-------
    nisar-db label-processing-mode \
        --consistent-json opera-nisar-disp-consistent-gslc-2026-07-25.json \
        --previous-json opera-nisar-disp-consistent-gslc-2026-04-25.json \
        --output opera-nisar-disp-consistent-gslc-with-processing-mode-2026-07-25.json

"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import click

from .io_json import write_zipped_json

__all__ = [
    "add_processing_modes",
    "assign_processing_modes",
    "find_frames_with_changed_mode",
    "get_processing_mode_summary",
]

_TIME_FMT = "%Y-%m-%dT%H:%M:%S"
_DAYS_PER_YEAR = 365.25


def _parse(time_str: str) -> datetime:
    return datetime.strptime(time_str, _TIME_FMT)


def _data_section(db: dict[str, Any]) -> dict[str, Any]:
    """Return the frame mapping whether or not the doc is metadata-wrapped."""
    return db.get("data", db)


def identify_time_groups(
    sorted_times: list[str], gap_threshold_years: float
) -> list[list[str]]:
    """Split sorted sensing times wherever a gap exceeds the threshold.

    Parameters
    ----------
    sorted_times : list of str
        Sensing times in ascending order, ``YYYY-MM-DDTHH:MM:SS``.
    gap_threshold_years : float
        Gap length, in years, that starts a new group.

    Returns
    -------
    list of list of str
        One list per contiguous group.

    """
    if not sorted_times:
        return []

    threshold_days = gap_threshold_years * _DAYS_PER_YEAR
    groups = [[sorted_times[0]]]
    for previous, current in zip(sorted_times, sorted_times[1:]):
        if (_parse(current) - _parse(previous)).days >= threshold_days:
            groups.append([current])
        else:
            groups[-1].append(current)
    return groups


def assign_processing_modes(
    sensing_times: list[str],
    batch_size: int = 15,
    gap_threshold_years: float = 2.0,
) -> dict[str, str]:
    """Map each sensing time to its processing-mode label.

    Parameters
    ----------
    sensing_times : list of str
        Sensing times for one frame, in any order.
    batch_size : int
        Acquisitions per processing batch.
    gap_threshold_years : float
        Gap length, in years, that restarts batch numbering.

    Returns
    -------
    dict
        ``{sensing_time: "historical_01" | "forward_01" | "no_run"}``, ordered
        by sensing time.

    Example
    -------
    >>> assign_processing_modes(["2025-01-01T00:00:00"], batch_size=15)
    {'2025-01-01T00:00:00': 'no_run'}

    """
    if not sensing_times:
        return {}

    sorted_times = sorted(sensing_times, key=_parse)
    labels: dict[str, str] = {}

    for group_num, group in enumerate(
        identify_time_groups(sorted_times, gap_threshold_years), start=1
    ):
        if len(group) < batch_size:
            labels.update(dict.fromkeys(group, "no_run"))
            continue

        n_full_batches = len(group) // batch_size
        suffix = f"_{group_num:02d}"
        for i, time_str in enumerate(group):
            in_full_batch = (i // batch_size) < n_full_batches
            labels[time_str] = (
                f"historical{suffix}" if in_full_batch else f"forward{suffix}"
            )

    return {t: labels[t] for t in sorted_times}


def add_processing_modes(
    db: dict[str, Any],
    batch_size: int = 15,
    gap_threshold_years: float = 2.0,
) -> dict[str, Any]:
    """Return a copy of the consistent-GSLC doc with labelled sensing times.

    ``sensing_time_list`` changes from a list of times to a
    ``{time: label}`` mapping; every other per-frame field is preserved.

    Parameters
    ----------
    db : dict
        Loaded consistent-GSLC JSON.
    batch_size : int
        Acquisitions per processing batch.
    gap_threshold_years : float
        Gap length, in years, that restarts batch numbering.

    Returns
    -------
    dict
        Labelled document, ready to serialise.

    """
    result: dict[str, Any] = {}
    if "metadata" in db:
        result["metadata"] = {
            **db["metadata"],
            "processing_mode_params": {
                "batch_size": batch_size,
                "gap_threshold_years": gap_threshold_years,
                "labeling_time": datetime.now().isoformat(),
            },
        }

    result["data"] = {
        frame_idx: {
            **frame_data,
            "sensing_time_list": assign_processing_modes(
                frame_data["sensing_time_list"], batch_size, gap_threshold_years
            ),
        }
        for frame_idx, frame_data in _data_section(db).items()
    }
    return result


def find_frames_with_changed_mode(
    old_data: dict[str, Any], new_data: dict[str, Any]
) -> list[str]:
    """List frames whose winning (mode, coverage) differs between releases.

    Those frames cannot reuse the previous stack: the acquisitions selected as
    consistent are a different set.

    Parameters
    ----------
    old_data, new_data : dict
        ``data`` sections of the previous and current consistent-GSLC JSONs.

    Returns
    -------
    list of str
        Frame indices present in both documents whose mode or coverage changed.

    """
    return [
        frame_idx
        for frame_idx, old_frame in old_data.items()
        if frame_idx in new_data
        and (
            old_frame.get("common_mode"),
            old_frame.get("common_coverage"),
        )
        != (
            new_data[frame_idx].get("common_mode"),
            new_data[frame_idx].get("common_coverage"),
        )
    ]


def get_processing_mode_summary(db: dict[str, Any]) -> dict[str, Any]:
    """Count frames and sensing times per processing mode.

    Parameters
    ----------
    db : dict
        Document returned by :func:`add_processing_modes`.

    Returns
    -------
    dict
        Totals for ``historical``, ``forward`` and ``no_run`` labels.

    """
    data = _data_section(db)
    counts = {"historical": 0, "forward": 0, "no_run": 0}
    frames_with_forward = 0

    for frame_data in data.values():
        modes = list(frame_data["sensing_time_list"].values())
        for mode in modes:
            # Labels are "<mode>_<group>", except "no_run", which is unnumbered.
            counts["no_run" if mode == "no_run" else mode.rsplit("_", 1)[0]] += 1
        if any(m.startswith("forward") for m in modes):
            frames_with_forward += 1

    return {
        "total_frames": len(data),
        "frames_with_forward": frames_with_forward,
        "total_sensing_times": sum(counts.values()),
        "historical_count": counts["historical"],
        "forward_count": counts["forward"],
        "no_run_count": counts["no_run"],
    }


@click.command(name="label-processing-mode", context_settings={"show_default": True})
@click.option(
    "--consistent-json",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Consistent-GSLC JSON to label.",
)
@click.option(
    "--previous-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Previous release's consistent-GSLC JSON, to report changed frames.",
)
@click.option(
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=(
        "Output JSON path (default: "
        "opera-nisar-disp-consistent-gslc-with-processing-mode-{today}.json)."
    ),
)
@click.option("--batch-size", type=int, default=15, help="Acquisitions per batch.")
@click.option(
    "--gap-threshold-years",
    type=float,
    default=2.0,
    help="Gap length, in years, that restarts batch numbering.",
)
def main(
    consistent_json: Path,
    previous_json: Path | None,
    output: Path | None,
    batch_size: int,
    gap_threshold_years: float,
) -> None:
    """Add historical/forward processing-mode labels to a consistent-GSLC JSON."""
    db = json.loads(consistent_json.read_text())
    labelled = add_processing_modes(db, batch_size, gap_threshold_years)

    if previous_json:
        changed = find_frames_with_changed_mode(
            _data_section(json.loads(previous_json.read_text())),
            _data_section(db),
        )
        labelled.setdefault("metadata", {})["frames_with_changed_mode"] = changed
        click.echo(
            f"{len(changed)} frames changed (mode, coverage) since {previous_json}"
        )

    if output is None:
        today = datetime.today().strftime("%Y-%m-%d")
        output = Path(
            f"opera-nisar-disp-consistent-gslc-with-processing-mode-{today}.json"
        )

    zip_path = write_zipped_json(output, labelled)

    summary = get_processing_mode_summary(labelled)
    click.echo(f"Written: {output}")
    click.echo(f"Written: {zip_path}")
    click.echo(f"  frames:     {summary['total_frames']:,}")
    click.echo(f"  historical: {summary['historical_count']:,}")
    click.echo(f"  forward:    {summary['forward_count']:,}")
    click.echo(f"  no_run:     {summary['no_run_count']:,}")


if __name__ == "__main__":
    main()
