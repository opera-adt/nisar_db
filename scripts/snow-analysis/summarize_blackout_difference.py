#!/usr/bin/env python
"""Summarize a consistent-GSLC JSON before and after blackout filtering.

Step 3 of the NISAR snow analysis: quantifies what the blackout windows cost.
Takes the two consistent-GSLC JSONs -- one built with ``--blackout-file`` and
one without -- and reports acquisitions kept, acquisitions lost, and how many
frames still clear the minimum ministack length.

NISAR analogue of ``burst_db``'s ``snow-analysis/summarize_blackout_difference.py``.
NISAR frames carry no sub-frame burst list, so the count that matters is
``len(sensing_time_list)`` per ``frame_idx``.

Examples
--------
Compare two runs, grouped by common mode::

    python summarize_blackout_difference.py \\
        --all opera-nisar-disp-consistent-gslc-2026-07-25-no-blackout.json \\
        --filtered opera-nisar-disp-consistent-gslc-2026-07-25.json

Group by latitude band (where snow loss actually varies) and save the plots::

    python summarize_blackout_difference.py \\
        --all all.json --filtered filtered.json \\
        --frames-gpkg notebooks/opera-nisar-disp-frames.gpkg \\
        --group-by latitude --outdir figures/

Point at a directory and let it pick the pair by filename::

    python summarize_blackout_difference.py --dir ./outputs

"""

from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table

LAT_BINS = [-90, 40, 50, 60, 70, 90]
LAT_LABELS = ["<40N", "40-50N", "50-60N", "60-70N", ">70N"]


def _read_json(path: Path) -> dict:
    """Read a ``.json`` or ``.json.zip`` consistent-GSLC file."""
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            return json.loads(zf.read(zf.namelist()[0]))
    return json.loads(path.read_text())


def load_consistent_json(path: Path) -> pd.DataFrame:
    """Return per-frame acquisition counts from a consistent-GSLC JSON."""
    data = _read_json(path)["data"]
    return pd.DataFrame(
        {
            "frame_idx": [int(k) for k in data],
            "common_mode": [v["common_mode"] for v in data.values()],
            "common_coverage": [v["common_coverage"] for v in data.values()],
            "sensing_time_count": [len(v["sensing_time_list"]) for v in data.values()],
        }
    )


def compare(df_all: pd.DataFrame, df_filtered: pd.DataFrame) -> pd.DataFrame:
    """Join the unfiltered and filtered tables and add the loss columns.

    Frames dropped entirely by the blackout filter keep a zero count rather
    than disappearing, so the totals stay comparable.
    """
    df = df_all.merge(
        df_filtered[["frame_idx", "sensing_time_count"]],
        on="frame_idx",
        how="left",
        suffixes=("_all", "_selected"),
    )
    df["sensing_time_count_selected"] = (
        df["sensing_time_count_selected"].fillna(0).astype(int)
    )
    df["acqs_lost"] = df.sensing_time_count_all - df.sensing_time_count_selected
    df["pct_lost"] = 100 * df.acqs_lost / df.sensing_time_count_all
    return df


def add_groups(
    df: pd.DataFrame, frames_gpkg: Path | None, group_by: str
) -> tuple[pd.DataFrame, str]:
    """Attach the grouping column, returning the frame and the column name."""
    if group_by == "mode":
        df["group"] = df.common_mode + "_" + df.common_coverage
        return df, "mode+coverage"

    if frames_gpkg is None:
        raise ValueError(f"--group-by {group_by} needs --frames-gpkg")

    gdf = gpd.read_file(frames_gpkg)[["frame_idx", "track", "geometry"]]
    gdf["frame_idx"] = gdf.frame_idx.astype(int)
    if group_by == "latitude":
        # representative_point stays inside the frame, unlike a centroid on the
        # dateline-crossing MultiPolygons. The result stays categorical so the
        # summary rows come out south-to-north rather than alphabetically.
        gdf["group"] = pd.cut(
            gdf.geometry.representative_point().y,
            bins=LAT_BINS,
            labels=LAT_LABELS,
        ).cat.add_categories("unknown")
    else:
        gdf["group"] = gdf.track.astype(int).astype(str)

    df = df.merge(gdf[["frame_idx", "group"]], on="frame_idx", how="left")
    df["group"] = df["group"].fillna("unknown")
    return df, group_by


def print_summary(df: pd.DataFrame, group_label: str, min_stack: int) -> None:
    """Print a per-group table of kept/lost acquisitions."""
    console = Console()
    table = Table(
        title=f"NISAR consistent GSLC - after blackout filter (by {group_label})",
        header_style="bold magenta",
    )
    table.add_column(group_label.capitalize())
    table.add_column("# Frames", justify="right")
    table.add_column("Acqs kept", justify="right")
    table.add_column("Acqs lost", justify="right")
    table.add_column("% lost", justify="right")
    table.add_column(f"Frames >={min_stack}", justify="right")

    summary = (
        df.assign(long_enough=df.sensing_time_count_selected >= min_stack)
        .groupby("group", observed=True)
        .agg(
            frames=("frame_idx", "count"),
            kept=("sensing_time_count_selected", "sum"),
            lost=("acqs_lost", "sum"),
            long_enough=("long_enough", "sum"),
        )
        .assign(pct_lost=lambda x: 100 * x.lost / (x.kept + x.lost))
        .sort_index()
    )

    # iterrows() collapses the row to a single float dtype, hence the int casts.
    for group, row in summary.iterrows():
        table.add_row(
            str(group),
            f"{int(row.frames):,d}",
            f"{int(row.kept):,d}",
            f"{int(row.lost):,d}",
            f"{row.pct_lost:.1f}",
            f"{int(row.long_enough):,d}",
        )
    totals = summary.sum()
    table.add_section()
    table.add_row(
        "TOTAL",
        f"{int(totals.frames):,d}",
        f"{int(totals.kept):,d}",
        f"{int(totals.lost):,d}",
        f"{100 * totals.lost / (totals.kept + totals.lost):.1f}",
        f"{int(totals.long_enough):,d}",
    )
    console.print(table)

    dropped = int((df.sensing_time_count_selected == 0).sum())
    if dropped:
        console.print(f"[yellow]{dropped} frames lost every acquisition.[/yellow]")


def make_plots(df: pd.DataFrame, min_stack: int, outdir: Path | None) -> None:
    """Draw the coverage, loss and before/after scatter figures."""
    # Imported here so `--no-plots` runs in an environment without the plotting
    # stack installed.
    import matplotlib.pyplot as plt
    import seaborn as sns

    sns.set_theme(style="whitegrid")
    bins = np.arange(0, df.sensing_time_count_all.max() + 12, 12)

    figs = {}

    fig, ax = plt.subplots(figsize=(10, 5))
    sns.histplot(
        data=df,
        x="sensing_time_count_selected",
        hue="group",
        bins=bins,
        multiple="stack",
        edgecolor=".2",
        ax=ax,
    )
    ax.axvline(min_stack, ls="--", color="k", lw=1)
    ax.set(
        xlabel="Acquisitions per frame (after blackout)",
        ylabel="# Frames",
        title="Coverage per frame (blackout-filtered)",
    )
    figs["coverage-after-blackout"] = fig

    fig, ax = plt.subplots(figsize=(7, 6))
    sns.histplot(
        data=df[df.acqs_lost > 0],
        x="acqs_lost",
        hue="group",
        bins=bins,
        multiple="stack",
        edgecolor=".2",
        ax=ax,
    )
    ax.set(
        xlabel="Acquisitions lost per frame",
        ylabel="# Frames",
        title="Acquisitions lost to snow blackouts",
    )
    figs["acquisitions-lost"] = fig

    fig, ax = plt.subplots(figsize=(6, 6))
    sns.scatterplot(
        data=df,
        x="sensing_time_count_all",
        y="sensing_time_count_selected",
        hue="group",
        alpha=0.6,
        edgecolor="none",
        ax=ax,
    )
    lim = df.sensing_time_count_all.max()
    ax.plot([0, lim], [0, lim], "--k", lw=1)
    ax.set(
        xlabel="Acquisitions before blackout",
        ylabel="Acquisitions after blackout",
        title="Per-frame acquisition loss",
    )
    figs["before-after-scatter"] = fig

    for fig in figs.values():
        fig.tight_layout()

    if outdir is None:
        plt.show()
        return
    outdir.mkdir(parents=True, exist_ok=True)
    for name, fig in figs.items():
        path = outdir / f"{name}.png"
        fig.savefig(path, dpi=150)
        print(f"Wrote {path}")


def _pair_from_dir(directory: Path) -> tuple[Path, Path]:
    """Pick the (unfiltered, filtered) JSON pair out of ``directory``."""
    candidates = sorted(directory.glob("*consistent-gslc*.json"))
    unfiltered = [p for p in candidates if "no-blackout" in p.name]
    filtered = [p for p in candidates if "no-blackout" not in p.name]
    if not unfiltered or not filtered:
        raise FileNotFoundError(
            f"{directory} must hold one '*no-blackout*.json' and one other "
            f"consistent-GSLC JSON; found {[p.name for p in candidates]}"
        )
    return unfiltered[-1], filtered[-1]


def main() -> None:
    """Report the blackout filter's cost on a consistent-GSLC pair."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--all", type=Path, help="Consistent-GSLC JSON, no blackout.")
    parser.add_argument(
        "--filtered", type=Path, help="Consistent-GSLC JSON, blackout applied."
    )
    parser.add_argument(
        "--dir",
        type=Path,
        help="Directory holding both JSONs (picked by the 'no-blackout' name tag).",
    )
    parser.add_argument(
        "--frames-gpkg", type=Path, default=None, help="Frames GPKG, for --group-by."
    )
    parser.add_argument(
        "--group-by",
        choices=["mode", "latitude", "track"],
        default="mode",
        help="How to group the summary rows.",
    )
    parser.add_argument(
        "--min-stack",
        type=int,
        default=15,
        help="Acquisitions a frame needs to still make a ministack.",
    )
    parser.add_argument(
        "--outdir",
        type=Path,
        default=None,
        help="Save figures here instead of showing.",
    )
    parser.add_argument("--no-plots", action="store_true", help="Table only.")
    args = parser.parse_args()

    if args.dir:
        path_all, path_filtered = _pair_from_dir(args.dir)
    elif args.all and args.filtered:
        path_all, path_filtered = args.all, args.filtered
    else:
        parser.error("pass either --dir or both --all and --filtered")

    print(f"Unfiltered: {path_all.name}\nFiltered:   {path_filtered.name}")

    df = compare(load_consistent_json(path_all), load_consistent_json(path_filtered))
    df, group_label = add_groups(df, args.frames_gpkg, args.group_by)

    print_summary(df, group_label, args.min_stack)
    if not args.no_plots:
        make_plots(df, args.min_stack, args.outdir)


if __name__ == "__main__":
    main()
