r"""Create a JSON of consistent GSLC acquisitions per NISAR frame.

Analogous to burst_db's ``opera-disp-s1-consistent-burst-ids-*.json``,
but for NISAR which is frame-based (no sub-frame burst IDs).

Selection logic (in priority order)
-------------------------------------
Each candidate ``mode`` first settles its own coverage by majority (``F`` when
``n_F >= n_P``, else ``P``), then the modes compete:

1. **Coverage** — a mode that resolves to full-frame (``F``) beats one that
   resolves to partial (``P``).  Full coverage is what a DISP stack needs, so
   it outranks the mode code itself.

   The exception is a frame that is *mostly* observed partially: once partial
   acquisitions exceed :data:`PARTIAL_DOMINANCE_THRESHOLD` of the frame, the
   preference flips to ``P``, because the partial series is then the one with
   real temporal coverage.

2. **Count** — among modes with the same coverage, the longer series wins:
   the number of acquisitions in the selected ``(mode, coverage)`` combo.
   Only the standard modes compete: a frame observed exclusively in
   non-standard (engineering / test) modes has no consistent stack and is
   dropped from the output, unless ``keep_nonstandard_modes`` is set, which
   lets its non-standard modes compete among themselves instead.

3. **Mode** — the remaining tie-break, for modes that are level on coverage
   *and* count, is ``"4005"`` over ``"2005"``.

   Examples (from real NISAR data):
     - ``4005_F x5, 4005_P x2``  ->  winner ``4005_F``
     - ``4005_P x7, 4005_F x1``  ->  winner ``4005_P``  (only mode; P majority)
     - ``2005_F x4, 4005_F x4``  ->  winner ``4005_F``  (F, level counts: 4005)
     - ``2005_F x4, 4005_P x4``  ->  winner ``2005_F``  (50% partial; F beats P)
     - ``4005_P x4, 2005_F x1``  ->  winner ``4005_P``  (80% partial; P wins)
     - ``2005_P x4, 4005_P x1``  ->  winner ``2005_P``  (both P; count decides)

4. **Deduplication** — if the winning ``(mode, coverage)`` combo appears
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

import json
from datetime import datetime
from pathlib import Path

import click
import geopandas as gpd
import pandas as pd

from .blackout import (
    apply_blackout,
    create_blackout_dates_json,  # re-exported for back-compat
    create_reference_dates_json,  # re-exported for back-compat
)
from .io_json import write_zipped_json
from .modes import MODE_PRIORITY, STANDARD_MODES, value_rank

__all__ = [
    "apply_blackout",
    "build_frame_idx_map",
    "create_blackout_dates_json",
    "create_reference_dates_json",
    "make_consistent_gslc_json",
    "select_consistent_acquisitions",
]


# ---------------------------------------------------------------------------
# Core selection logic
# ---------------------------------------------------------------------------

# Share of a frame's *candidate* acquisitions (the standard modes, or all modes
# when the frame has none) that must be partial before partial coverage outranks
# full. Preferring ``F`` costs temporal coverage whenever the frame is mostly
# observed partially -- a frame seen as ``4005_P`` five times and ``2005_F`` once
# would yield a one-epoch stack. Above this share the partial series is the
# longer one and wins instead. The value is a judgement call, not a derived
# quantity.
PARTIAL_DOMINANCE_THRESHOLD = 0.66


def _common_mode_coverage(
    grp: pd.DataFrame, keep_nonstandard_modes: bool = False
) -> tuple[str, str] | None:
    """Return (common_mode, common_coverage) for a single (track, frame) group.

    Only the standard modes compete. Every candidate mode first settles its own
    coverage by majority (``F`` when ``n_F >= n_P``, else ``P``); the modes then
    compete on, in order:

      1. coverage — full-frame (``F``) beats partial (``P``), unless partial
         acquisitions make up more than :data:`PARTIAL_DOMINANCE_THRESHOLD` of
         the candidates, in which case the preference flips,
      2. count — acquisitions in the selected ``(mode, coverage)`` combo,
      3. mode — :data:`~nisar_db.modes.MODE_PRIORITY` (``"4005"`` then
         ``"2005"``) settles modes that are level on coverage and count.

    Parameters
    ----------
    grp:
        Subset of the catalog DataFrame for one (track, frame) pair.
        Must have columns ``mode`` and ``coverage``.
    keep_nonstandard_modes:
        When the frame has no standard acquisitions at all, let its
        non-standard modes compete among themselves rather than returning
        ``None``.

    Returns
    -------
    (mode, coverage) : tuple[str, str] or None
        e.g. ("4005", "F"); ``None`` for a frame with no standard acquisitions
        (unless ``keep_nonstandard_modes``).

    """
    counts = (
        grp.groupby(["mode", "coverage"])
        .size()
        .unstack(fill_value=0)
        .reindex(columns=["F", "P"], fill_value=0)
    )

    standard = counts[counts.index.isin(STANDARD_MODES)]
    if standard.empty and not keep_nonstandard_modes:
        return None
    candidates = standard if not standard.empty else counts

    partial_share = candidates["P"].sum() / candidates.to_numpy().sum()
    preferred_coverage = "P" if partial_share > PARTIAL_DOMINANCE_THRESHOLD else "F"

    ranked = pd.DataFrame(
        {
            "mode": candidates.index,
            "coverage": (
                candidates["F"].ge(candidates["P"]).map({True: "F", False: "P"})
            ),
            # The majority coverage is by definition the larger of the two counts.
            "n_selected": candidates[["F", "P"]].max(axis=1),
        }
    )
    ranked["coverage_rank"] = (ranked["coverage"] != preferred_coverage).astype(int)
    ranked["mode_rank"] = [value_rank(m, MODE_PRIORITY) for m in ranked["mode"]]

    winner = ranked.sort_values(
        ["coverage_rank", "n_selected", "mode_rank"],
        ascending=[True, False, True],
        kind="stable",
    ).iloc[0]

    return str(winner["mode"]), str(winner["coverage"])


def select_consistent_acquisitions(
    df: pd.DataFrame, keep_nonstandard_modes: bool = False
) -> pd.DataFrame:
    """Filter catalog to the consistent (mode, coverage) per (track, frame).

    Returns a DataFrame with one row per (track, frame, sensing_date),
    keeping only the earliest sensing time when there are duplicates. Frames
    observed only in non-standard modes are dropped unless
    ``keep_nonstandard_modes`` is set.

    New columns added:
      ``common_mode``      — winning mode code (e.g. "4005")
      ``common_coverage``  — winning coverage ("F" or "P")
    """
    # Compute (common_mode, common_coverage) per frame. Frames whose winner is
    # ``None`` have no standard-mode stack and are simply left out, so the inner
    # merge below drops their acquisitions.
    winners = [
        (track, frame, *winner)
        for (track, frame), grp in df.groupby(["track", "frame"])
        if (winner := _common_mode_coverage(grp, keep_nonstandard_modes)) is not None
    ]
    result = pd.DataFrame(
        winners, columns=["track", "frame", "common_mode", "common_coverage"]
    ).astype({"track": df["track"].dtype, "frame": df["frame"].dtype})

    # ``create-gslc-csv`` writes its own ``common_mode`` (the 2-char mode *family*),
    # which would collide with the full 4-char mode computed above and turn the
    # merge into _x/_y suffixes. Ours is authoritative, so drop theirs first.
    df = df.drop(columns=["common_mode", "common_coverage"], errors="ignore")
    df = df.merge(result, on=["track", "frame"], how="inner")

    # Keep only rows matching the winning (mode, coverage)
    df = df[
        (df["mode"] == df["common_mode"]) & (df["coverage"] == df["common_coverage"])
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
# Main JSON builder
# ---------------------------------------------------------------------------


def make_consistent_gslc_json(
    catalog_csv: Path,
    nisar_gpkg: Path,
    output: Path | None = None,
    blackout_file: Path | None = None,
    keep_nonstandard_modes: bool = False,
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
    keep_nonstandard_modes:
        Keep frames observed only in non-standard (engineering / test) modes,
        letting those modes compete among themselves. Off by default, so such
        frames are absent from the output.

    Returns
    -------
    Path to the written JSON file.

    """
    str_cols = {"mode", "common_mode", "common_coverage", "cycle", "crid", "version"}

    df = pd.read_csv(
        catalog_csv,
        dtype={
            c: str for c in str_cols if c in pd.read_csv(catalog_csv, nrows=0).columns
        },
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
    click.echo(
        f"  After NA filter: {len(df):,} acquisitions across "
        f"{df[['track','frame']].drop_duplicates().shape[0]} frames"
    )

    # Select consistent (mode, coverage) per frame
    n_frames_before = df[["track", "frame"]].drop_duplicates().shape[0]
    df = select_consistent_acquisitions(
        df, keep_nonstandard_modes=keep_nonstandard_modes
    )
    n_dropped = n_frames_before - df[["track", "frame"]].drop_duplicates().shape[0]
    click.echo(f"  After mode+coverage selection: {len(df):,} acquisitions")
    if n_dropped:
        click.echo(f"  Dropped {n_dropped} frames with no standard-mode acquisitions")
    mode_dist = df["common_mode"].value_counts().to_string()
    coverage_dist = df["common_coverage"].value_counts().to_string()
    click.echo(f"  common_mode distribution:\n{mode_dist}")
    click.echo(f"  common_coverage distribution:\n{coverage_dist}")

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
        sorted_grp = grp.sort_values("sensing_time")
        data[str(frame_idx)] = {
            "common_mode": sorted_grp["common_mode"].iloc[0],
            "common_coverage": sorted_grp["common_coverage"].iloc[0],
            "sensing_time_list": [
                pd.Timestamp(t).strftime("%Y-%m-%dT%H:%M:%S")
                for t in sorted_grp["sensing_time"]
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
@click.option(
    "--keep-nonstandard-modes",
    is_flag=True,
    default=False,
    help=(
        "Keep frames observed only in non-standard modes (not 4005/2005), "
        "picking a winner among those modes instead of dropping the frame."
    ),
)
def main(
    catalog_csv: Path,
    nisar_gpkg: Path,
    output: Path | None,
    blackout_file: Path | None,
    keep_nonstandard_modes: bool,
):
    """Create the consistent-GSLC JSON for NISAR frames.

    \b
    Selection logic:
      1. Majority coverage per mode (F beats P on tie)
      2. Winning mode: full-frame beats partial (reversed on frames more than
         66% partial), then acquisition count, then "4005" beats "2005"
      3. Frames with no 4005/2005 acquisitions are dropped
         (--keep-nonstandard-modes keeps them)
      4. One acquisition per calendar date (earliest sensing time)
    """  # noqa: D301
    make_consistent_gslc_json(
        catalog_csv=catalog_csv,
        nisar_gpkg=nisar_gpkg,
        output=output,
        blackout_file=blackout_file,
        keep_nonstandard_modes=keep_nonstandard_modes,
    )


if __name__ == "__main__":
    main()
