#!/usr/bin/env python3
"""Create a JSON of consistent GSLC acquisitions per NISAR frame.

Analogous to burst_db's ``opera-disp-s1-consistent-burst-ids-*.json``,
but for NISAR which is frame-based (no sub-frame burst IDs).

Selection logic (in priority order)
-------------------------------------
1. **Common mode** — for each (track, frame), find the dominant ``mode``
   (full 4-character code, e.g. ``"4005"`` or ``"2005"``).
   Priority: standard families ``{"4005", "2005"}`` first; fall back to
   overall most-frequent if the frame has no standard-family acquisitions.

2. **Coverage** — within the winning mode, prefer full-frame (``F``) over
   partial (``P``).  If the frame has *more* acquisitions of ``<mode>_F``
   than ``<mode>_P``, select ``F``; otherwise ``P``.
   Ties go to ``F``.

   Examples (from real NISAR data):
     - ``4005_F×5, 4005_P×2``  →  winner ``4005_F``
     - ``4005_P×7, 4005_F×1``  →  winner ``4005_P``
     - ``2005_F×4, 4005_P×4``  →  winner ``4005_P``  (4005 beats 2005 first)
     - ``4005_P×3, 2005_F×3``  →  winner ``4005_P``  (4005 beats 2005, tie→P)

3. **Deduplication** — if the winning ``(mode, coverage)`` combo appears
   multiple times on the same calendar date, keep only the earliest
   sensing time.

Output JSON
-----------
``opera-nisar-disp-consistent-gslc-{date}.json[.zip]``

Schema::

    {
      "metadata": {
        "generation_time": "...",
        "input_catalog": "path/to/catalog.csv",
        "nisar_gpkg": "path/to/frames.gpkg",
        "blackout_file": null,
        "description": "..."
      },
      "data": {
        "<frame_idx>": {
          "common_mode": "4005",
          "common_coverage": "F",
          "sensing_time_list": ["2025-11-24T13:08:58", ...]
        }
      }
    }

Optional helpers
-----------------
``create_blackout_dates_json``  — write per-frame blackout period JSON.
``create_reference_dates_json`` — write per-frame reference-date change JSON.

Usage
-----
    python create_consistent_gslc_catalog.py \\
        --catalog nisar_gslc_catalog.csv \\
        --nisar-gpkg opera-nisar-disp-frames.gpkg

    # with blackout filter
    python create_consistent_gslc_catalog.py \\
        --catalog nisar_gslc_catalog.csv \\
        --nisar-gpkg opera-nisar-disp-frames.gpkg \\
        --blackout-file nisar-blackout-dates.json
"""

from __future__ import annotations

import csv
import json
import zipfile
from datetime import date, datetime
from pathlib import Path

import click
import geopandas as gpd
import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Standard science-mode codes (full 4-char mode, not just family prefix)
_STANDARD_MODES = {"4005", "2005"}

# Standard mode families (first 2 chars)
_STANDARD_FAMILIES = {"40", "20"}


# ---------------------------------------------------------------------------
# Core selection logic
# ---------------------------------------------------------------------------


def _common_mode_coverage(grp: pd.DataFrame) -> tuple[str, str]:
    """Return (common_mode, common_coverage) for a single (track, frame) group.

    Priority:
      1. Standard modes ("4005", "2005") beat non-standard.
      2. Most frequent mode wins (total count across F+P).
      3. On a mode-count tie: the mode with more F acquisitions wins.
      4. On a full tie: F beats P (i.e. prefer full-frame).

    Parameters
    ----------
    grp:
        Subset of the catalog DataFrame for one (track, frame) pair.
        Must have columns ``mode`` and ``coverage``.

    Returns
    -------
    (mode, coverage) : tuple[str, str]
        e.g. ("4005", "F")
    """
    combos = grp.groupby(["mode", "coverage"]).size().reset_index(name="n")

    # Separate standard vs non-standard mode rows
    standard_rows = combos[combos["mode"].isin(_STANDARD_MODES)]
    candidate_rows = standard_rows if not standard_rows.empty else combos

    # Build per-mode summary: total count + F count (for tiebreaking)
    mode_summary = (
        candidate_rows.groupby("mode")
        .apply(
            lambda m: pd.Series({
                "total": m["n"].sum(),
                "n_F": m.loc[m["coverage"] == "F", "n"].sum(),
            }),
            include_groups=False,
        )
        .reset_index()
    )

    # Rank standard modes above non-standard for final tiebreak
    _MODE_RANK = {m: i for i, m in enumerate(_STANDARD_MODES)}
    mode_summary["rank"] = mode_summary["mode"].map(
        lambda m: _MODE_RANK.get(m, len(_STANDARD_MODES))
    )

    # Sort: most total → most F → standard-mode rank (lower=better)
    mode_summary = mode_summary.sort_values(
        ["total", "n_F", "rank"], ascending=[False, False, True]
    )
    winning_mode = mode_summary.iloc[0]["mode"]
    winning_n_F = mode_summary.iloc[0]["n_F"]
    winning_total = mode_summary.iloc[0]["total"]
    winning_n_P = winning_total - winning_n_F

    # Within winning mode, pick coverage: F beats P on tie
    winning_coverage = "F" if winning_n_F >= winning_n_P else "P"

    return winning_mode, winning_coverage


def select_consistent_acquisitions(df: pd.DataFrame) -> pd.DataFrame:
    """Filter catalog to the consistent (mode, coverage) per (track, frame).

    Returns a DataFrame with one row per (track, frame, sensing_date),
    keeping only the earliest sensing time when there are duplicates.

    New columns added:
      ``common_mode``      — winning mode code (e.g. "4005")
      ``common_coverage``  — winning coverage ("F" or "P")
    """
    # Compute (common_mode, common_coverage) per frame
    result = (
        df.groupby(["track", "frame"])
        .apply(_common_mode_coverage, include_groups=False)
        .reset_index()
    )
    result[["common_mode", "common_coverage"]] = pd.DataFrame(
        result[0].tolist(), index=result.index
    )
    result = result.drop(columns=[0])

    df = df.merge(result, on=["track", "frame"], how="left")

    # Keep only rows matching the winning (mode, coverage)
    df = df[
        (df["mode"] == df["common_mode"])
        & (df["coverage"] == df["common_coverage"])
    ].copy()

    # Deduplicate: one row per (track, frame, sensing_date), earliest time
    df["sensing_time"] = pd.to_datetime(df["sensing_time"])
    df = (
        df.sort_values("sensing_time")
        .drop_duplicates(subset=["track", "frame", "sensing_date"], keep="first")
        .sort_values(["track", "frame", "sensing_time"])
        .reset_index(drop=True)
    )

    return df


# ---------------------------------------------------------------------------
# Blackout filtering
# ---------------------------------------------------------------------------


def _is_excluded(
    frame_idx: str,
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
        lambda r: _is_excluded(
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


# ---------------------------------------------------------------------------
# frame_idx mapping from GPKG
# ---------------------------------------------------------------------------


def build_frame_idx_map(nisar_gpkg: Path) -> dict[tuple[int, int], int]:
    """Return {(track, frame): frame_idx} from the filtered NISAR frames GPKG.

    ``frame_idx`` is the integer index stored in the ``frame_idx`` column
    (written by ``create_frame_to_bound.py``).  It is the same key used in
    the ``opera-nisar-disp-frame-to-bounds.json`` file.
    """
    gdf = gpd.read_file(nisar_gpkg)

    if "frame_idx" not in gdf.columns:
        raise ValueError(
            f"{nisar_gpkg} has no 'frame_idx' column. "
            "Run create_frame_to_bound.py first to produce the filtered GPKG."
        )

    mapping: dict[tuple[int, int], int] = {}
    for _, row in gdf.iterrows():
        track = int(row["track"])
        frame = int(row["frame"])
        mapping[(track, frame)] = int(row["frame_idx"])

    return mapping


# ---------------------------------------------------------------------------
# JSON writers
# ---------------------------------------------------------------------------


def write_zipped_json(json_path: str | Path, data: dict, level: int = 6) -> str:
    """Write ``data`` as JSON and also as a .json.zip beside it."""
    json_path = Path(json_path)
    with open(json_path, "w") as f:
        json.dump(data, f, indent=2, default=str)

    zip_path = str(json_path) + ".zip"
    with zipfile.ZipFile(
        zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=level
    ) as zf:
        zf.writestr(json_path.name, json.dumps(data, default=str))

    return zip_path


# ---------------------------------------------------------------------------
# Main JSON builder
# ---------------------------------------------------------------------------


def make_consistent_gslc_json(
    catalog_csv: Path,
    nisar_gpkg: Path,
    output: Path | None = None,
    blackout_file: Path | None = None,
) -> Path:
    """Create the consistent-GSLC JSON for all NISAR frames in the catalog.

    Parameters
    ----------
    catalog_csv:
        CSV produced by ``create_gslc_catalog.py`` (columns: track, frame,
        mode, coverage, sensing_time, sensing_date, …).
    nisar_gpkg:
        Filtered NISAR frames GeoPackage produced by ``create_frame_to_bound.py``
        (must contain a ``frame_idx`` column).
    output:
        Output JSON path.  Defaults to
        ``opera-nisar-disp-consistent-gslc-{today}.json``.
    blackout_file:
        Optional JSON with per-frame blackout periods
        (same schema as burst_db's blackout JSON).

    Returns
    -------
    Path to the written JSON file.
    """
    str_cols = {"mode", "common_mode", "common_coverage", "cycle", "crid", "version"}

    df = pd.read_csv(
        catalog_csv,
        dtype={c: str for c in str_cols if c in pd.read_csv(catalog_csv, nrows=0).columns},
    )

    click.echo(f"Loaded {len(df):,} acquisitions from {catalog_csv.name}")
    click.echo(f"  mode distribution:\n{df['mode'].value_counts().to_string()}")
    click.echo(f"  coverage distribution:\n{df['coverage'].value_counts().to_string()}")

    # Build frame_idx lookup
    frame_idx_map = build_frame_idx_map(nisar_gpkg)
    click.echo(f"Loaded {len(frame_idx_map):,} frames from {nisar_gpkg.name}")

    # Filter to frames in the GPKG (North America)
    df["_key"] = list(zip(df["track"].astype(int), df["frame"].astype(int)))
    df = df[df["_key"].isin(frame_idx_map)].drop(columns=["_key"])
    click.echo(f"  After NA filter: {len(df):,} acquisitions across "
               f"{df[['track','frame']].drop_duplicates().shape[0]} frames")

    # Select consistent (mode, coverage) per frame
    df = select_consistent_acquisitions(df)
    click.echo(f"  After mode+coverage selection: {len(df):,} acquisitions")
    click.echo(f"  common_mode distribution:\n{df['common_mode'].value_counts().to_string()}")
    click.echo(f"  common_coverage distribution:\n{df['common_coverage'].value_counts().to_string()}")

    # Add frame_idx column
    df["frame_idx"] = df.apply(
        lambda r: frame_idx_map[(int(r["track"]), int(r["frame"]))], axis=1
    )

    # Apply blackout filter
    blackout_periods: dict = {}
    if blackout_file:
        blackout_periods = json.loads(Path(blackout_file).read_text())["blackout_dates"]
        df = apply_blackout(df, "frame_idx", blackout_periods)

    # Build output dict keyed by frame_idx
    data: dict[str, dict] = {}
    for frame_idx, grp in df.groupby("frame_idx"):
        grp = grp.sort_values("sensing_time")
        data[str(frame_idx)] = {
            "common_mode": grp["common_mode"].iloc[0],
            "common_coverage": grp["common_coverage"].iloc[0],
            "sensing_time_list": [
                pd.Timestamp(t).strftime("%Y-%m-%dT%H:%M:%S")
                for t in grp["sensing_time"]
            ],
        }

    today = datetime.today().strftime("%Y-%m-%d")
    if output is None:
        output = Path(f"opera-nisar-disp-consistent-gslc-{today}.json")

    result = {
        "metadata": {
            "generation_time": datetime.today().isoformat(),
            "input_catalog": str(catalog_csv),
            "nisar_gpkg": str(nisar_gpkg),
            "blackout_file": str(blackout_file) if blackout_file else None,
            "description": (
                "Consistent NISAR GSLC acquisitions per frame. "
                "One sensing_time per calendar date, filtered to the dominant "
                "(mode, coverage) combination for each frame."
            ),
        },
        "data": data,
    }

    zip_path = write_zipped_json(output, result)
    click.echo(f"\nWritten: {output}  ({len(data)} frames)")
    click.echo(f"Written: {zip_path}")

    return output


# ---------------------------------------------------------------------------
# Optional helper: blackout dates
# ---------------------------------------------------------------------------


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
        Output path.  Defaults to
        ``nisar-blackout-dates-{today}.json``.
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
        str(k): [[str(s), str(e)] for s, e in v]
        for k, v in blackout_periods.items()
    }

    result = {
        "metadata": {
            "generation_time": datetime.today().isoformat(),
            "description": description or (
                "Per-frame NISAR blackout periods. "
                "Acquisitions whose sensing_date falls in any [start, end] range "
                "are excluded from the consistent-GSLC catalog."
            ),
        },
        "blackout_dates": normalised,
    }

    zip_path = write_zipped_json(output, result)
    click.echo(f"Written: {output}  ({len(normalised)} frames with blackouts)")
    click.echo(f"Written: {zip_path}")
    return output


# ---------------------------------------------------------------------------
# Optional helper: reference dates
# ---------------------------------------------------------------------------


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
        Output path.  Defaults to
        ``nisar-reference-dates-{today}.json``.
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
            "description": description or (
                "Per-frame NISAR reference date changes. "
                "Each date marks a reset of the InSAR reference epoch "
                "(e.g. after a major earthquake or a data gap)."
            ),
        },
        "data": normalised,
    }

    zip_path = write_zipped_json(output, result)
    click.echo(f"Written: {output}  ({len(normalised)} frames with reference changes)")
    click.echo(f"Written: {zip_path}")
    return output


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.command(context_settings={"show_default": True})
@click.option(
    "--catalog",
    "catalog_csv",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="GSLC catalog CSV produced by create_gslc_catalog.py.",
)
@click.option(
    "--nisar-gpkg",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Filtered NISAR frames GPKG produced by create_frame_to_bound.py.",
)
@click.option(
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Output JSON path (default: opera-nisar-disp-consistent-gslc-{today}.json).",
)
@click.option(
    "--blackout-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional per-frame blackout-dates JSON to filter out excluded acquisitions.",
)
def main(
    catalog_csv: Path,
    nisar_gpkg: Path,
    output: Path | None,
    blackout_file: Path | None,
):
    """Create the consistent-GSLC JSON for NISAR frames.

    \b
    Selection logic:
      1. Dominant mode per frame  (standard "4005"/"2005" preferred)
      2. Majority coverage within that mode (F beats P on tie)
      3. One acquisition per calendar date (earliest sensing time)
    """
    make_consistent_gslc_json(
        catalog_csv=catalog_csv,
        nisar_gpkg=nisar_gpkg,
        output=output,
        blackout_file=blackout_file,
    )


if __name__ == "__main__":
    main()
