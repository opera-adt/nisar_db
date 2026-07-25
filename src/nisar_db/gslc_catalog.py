"""Parse NISAR GSLC file list into a structured catalog CSV.

Reads a text file of S3 paths or local file paths (one per line), parses
each GSLC filename, and writes a catalog CSV with per-granule fields plus
derived per-frame columns:

  - mode_family : first two characters of mode ("40", "20", "00", "77", ...)
  - common_mode : dominant standard mode family ("40" or "20") per (track, frame);
                  falls back to most frequent family if neither standard is present
  - is_full     : True if coverage == 'F' (full frame)

Mode families
-------------
  "40"  standard science mode  (e.g. 4005 = 5 m, 4000 = 20 m)
  "20"  alternate science mode (e.g. 2005)
  other engineering / test / non-standard modes (excluded from common_mode voting)

Usage
-----
    python create_gslc_catalog.py --input nisar_gslc_files.txt
    python create_gslc_catalog.py --input nisar_gslc_files.txt --output gslc_catalog.csv
    python create_gslc_catalog.py --input nisar_gslc_files.txt --na-only
"""

from __future__ import annotations

import csv
from pathlib import Path

import click
import pandas as pd

from nisar_db.filenames import GSLCFilename
from nisar_db.modes import STANDARD_FAMILIES, dominant_value

# Columns that contain zero-padded numeric strings (e.g. "0005", "003").
# Written with explicit quoting so pandas doesn't drop leading zeros on re-read.
_STR_COLS = ["cycle", "mode", "mode_family", "common_mode", "crid", "version"]


def parse_gslc_list(input_file: Path) -> tuple[pd.DataFrame, list[tuple[str, str]]]:
    """Parse a file of GSLC paths/granule IDs into a structured DataFrame.

    Parameters
    ----------
    input_file:
        Text file with one S3 path, local path, or bare granule ID per line.

    Returns
    -------
    df:
        One row per granule with parsed fields + derived columns.
    failed:
        List of (line, error_message) for lines that could not be parsed.

    """
    lines = [
        line.strip() for line in input_file.read_text().splitlines() if line.strip()
    ]

    rows: list[dict] = []
    failed: list[tuple[str, str]] = []

    for line in lines:
        # Extract the stem whether the line is an S3 path, local path, or bare name
        name = line.split("/")[-1]
        stem = name.removesuffix(".h5")
        try:
            g = GSLCFilename.from_path(stem)
            rows.append(
                {
                    "s3_path": line if line.startswith("s3://") else "",
                    "granule_id": stem,
                    "cycle": g.cycle,
                    "track": int(g.relative_orbit),
                    "frame": int(g.track_frame),
                    "pass_direction": g.pass_direction,
                    "mode": g.mode,
                    "polarization": g.polarization,
                    "coverage": g.coverage,
                    "sensing_time": g.start_datetime.strftime("%Y-%m-%dT%H:%M:%S"),
                    "sensing_date": g.start_datetime.strftime("%Y-%m-%d"),
                    "crid": g.crid,
                    "version": g.version,
                }
            )
        except Exception as exc:
            failed.append((line, str(exc)))

    df = pd.DataFrame(rows)
    if df.empty:
        return df, failed

    # mode_family: first two characters of the mode code ("40", "20", "00", …)
    df["mode_family"] = df["mode"].str[:2]

    # common_mode per (track, frame): dominant standard family ("40" or "20").
    # Only standard-family rows vote; fall back to overall most-frequent if the
    # frame has no standard-family acquisitions at all.
    common_mode = (
        df.groupby(["track", "frame"])["mode_family"]
        .agg(lambda grp: dominant_value(grp, STANDARD_FAMILIES))
        .rename("common_mode")
        .reset_index()
    )
    df = df.merge(common_mode, on=["track", "frame"], how="left")

    # Derived column: full-frame flag
    df["is_full"] = df["coverage"] == "F"

    return df, failed


def write_catalog_csv(df: pd.DataFrame, output: Path) -> None:
    """Write a catalog DataFrame to CSV, preserving zero-padded numeric strings.

    Columns like ``mode`` ("4005") or ``cycle`` ("003") are quoted explicitly so
    ``pandas.read_csv`` does not strip their leading zeros on the way back in.
    """
    df = df.copy()
    str_cols = [c for c in _STR_COLS if c in df.columns]
    df[str_cols] = df[str_cols].astype(str)
    df.to_csv(output, index=False, quoting=csv.QUOTE_NONNUMERIC)


def filter_north_america(df: pd.DataFrame, nisar_gpkg: Path) -> pd.DataFrame:
    """Keep only rows whose (track, frame) intersects the OPERA NA polygon."""
    import geopandas as gpd

    from nisar_db.geodb import filter_frames_to_na

    nisar_gdf = gpd.read_file(nisar_gpkg)
    na_frames = filter_frames_to_na(nisar_gdf)[["track", "frame"]].copy()
    na_frames["track"] = na_frames["track"].astype(int)
    na_frames["frame"] = na_frames["frame"].astype(int)

    return df.merge(na_frames, on=["track", "frame"], how="inner")


@click.command(context_settings={"show_default": True})
@click.option(
    "--input",
    "input_file",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Text file with one GSLC S3 path or granule ID per line.",
)
@click.option(
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Output CSV path. Defaults to <input_stem>_catalog.csv.",
)
@click.option(
    "--na-only",
    is_flag=True,
    default=False,
    help="Filter to OPERA North America frames (requires --nisar-gpkg).",
)
@click.option(
    "--nisar-gpkg",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="NISAR TrackFrame GeoPackage, required when --na-only is set.",
)
def main(
    input_file: Path,
    output: Path | None,
    na_only: bool,
    nisar_gpkg: Path | None,
):
    r"""Parse a NISAR GSLC file list into a structured catalog CSV.

    Adds two derived columns per row:

    \b
      common_mode  most frequent mode for the (track, frame) pair
      is_full      True when coverage == 'F' (full frame)
    """
    if na_only and nisar_gpkg is None:
        raise click.UsageError("--nisar-gpkg is required when --na-only is set.")

    click.echo(f"Parsing {input_file} …")
    df, failed = parse_gslc_list(input_file)

    if failed:
        click.echo(f"  Warning: failed to parse {len(failed)} lines.", err=True)
        for line, err in failed[:5]:
            click.echo(f"    {line[:80]}: {err}", err=True)

    n_pairs = df[["track", "frame"]].drop_duplicates().shape[0]
    click.echo(
        f"  Parsed {len(df):,} granules, {n_pairs:,} unique (track, frame) pairs."
    )

    if na_only:
        assert nisar_gpkg is not None  # guaranteed by the UsageError check above
        before = len(df)
        df = filter_north_america(df, nisar_gpkg)
        click.echo(f"  NA filter: {before:,} -> {len(df):,} granules.")

    # Print a quick per-frame mode summary
    frame_summary = (
        df.groupby(["track", "frame", "common_mode"])
        .agg(
            n_acquisitions=("sensing_date", "nunique"),
            n_full=("is_full", "sum"),
            n_partial=("is_full", lambda x: (~x).sum()),
        )
        .reset_index()
        .sort_values(["track", "frame"])
    )
    mode_family_dist = df["mode_family"].value_counts().to_string()
    common_mode_per_frame = (
        df[["track", "frame", "common_mode"]]
        .drop_duplicates()["common_mode"]
        .value_counts()
        .to_string()
    )
    click.echo(f"\nmode_family distribution:\n{mode_family_dist}")
    click.echo(
        f"\ncommon_mode per frame (standard families only):\n{common_mode_per_frame}"
    )
    n_full = df["is_full"].sum()
    n_partial = (~df["is_full"]).sum()
    click.echo(f"\nCoverage:\n  full={n_full:,}  partial={n_partial:,}")

    if output is None:
        output = input_file.parent / (input_file.stem + "_catalog.csv")

    # Force zero-padded string columns (e.g. "0005") to be quoted so pandas
    # doesn't strip leading zeros when reading the file back.
    str_cols = [c for c in _STR_COLS if c in df.columns]
    df[str_cols] = df[str_cols].astype(str)
    df.to_csv(output, index=False, quoting=csv.QUOTE_NONNUMERIC)
    click.echo(f"\nCatalog written to: {output}")

    summary_path = output.with_name(output.stem + "_frame_summary.csv")
    str_cols_s = [c for c in _STR_COLS if c in frame_summary.columns]
    frame_summary[str_cols_s] = frame_summary[str_cols_s].astype(str)
    frame_summary.to_csv(summary_path, index=False, quoting=csv.QUOTE_NONNUMERIC)
    click.echo(f"Frame summary written to: {summary_path}")


if __name__ == "__main__":
    main()
