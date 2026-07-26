"""Parse NISAR GSLC file list into a structured catalog CSV.

Reads either a text file of S3/HTTPS/local paths (one per line) or a
``nisar-db search`` results CSV, parses each GSLC filename, and writes a
catalog CSV with per-granule fields plus derived per-frame columns:

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
    python create_gslc_catalog.py --input nisar_search_results.csv
    python create_gslc_catalog.py --input nisar_gslc_files.txt --output gslc_catalog.csv
    python create_gslc_catalog.py --input nisar_gslc_files.txt --na-only
"""

from __future__ import annotations

import csv
from pathlib import Path

import click
import pandas as pd

from nisar_db.filenames import GSLCFilename
from nisar_db.modes import FAMILY_PRIORITY, dominant_value

# Columns that contain zero-padded numeric strings (e.g. "0005", "003").
# Written with explicit quoting so pandas doesn't drop leading zeros on re-read.
_STR_COLS = ["cycle", "mode", "mode_family", "common_mode", "crid", "version"]

# Search-CSV columns holding the granule name, most specific first. A CSV
# ``granule_id`` is the CMR concept ID ("G4257267728-ASF"), never a filename.
# ``title`` is what ``name`` was called before the column was renamed.
_CSV_NAME_COLS = ("filename", "name", "title", "granule_name")

# Search-CSV columns that may hold a data URL; the scheme decides the destination.
_CSV_URL_COLS = ("url", "https", "https_url", "s3", "s3_url", "s3_path")


def _read_entries(input_file: Path) -> list[tuple[str, str, str]]:
    """Return one ``(granule_name, https_url, s3_path)`` triple per input record.

    Accepts a plain list of paths/granule names (one per line) or a
    ``nisar-db search`` results CSV, detected by its header row.
    """
    lines = [
        line.strip() for line in input_file.read_text().splitlines() if line.strip()
    ]
    if not lines:
        return []

    header = {c.strip().strip('"') for c in lines[0].split(",")}
    if not header.intersection(_CSV_NAME_COLS):
        # Bare list: the name is the last path segment, the line itself the URL.
        return [
            (
                line.split("/")[-1],
                line if line.startswith("https://") else "",
                line if line.startswith("s3://") else "",
            )
            for line in lines
        ]

    df = pd.read_csv(input_file, dtype=str).fillna("")
    name_col = next(c for c in _CSV_NAME_COLS if c in df.columns)
    url_cols = [c for c in _CSV_URL_COLS if c in df.columns]

    entries: list[tuple[str, str, str]] = []
    for row in df.itertuples(index=False):
        urls = [str(getattr(row, c)).strip() for c in url_cols]
        https = next((u for u in urls if u.startswith("https://")), "")
        s3 = next((u for u in urls if u.startswith("s3://")), "")
        entries.append((str(getattr(row, name_col)).strip(), https, s3))
    return entries


def parse_gslc_list(input_file: Path) -> tuple[pd.DataFrame, list[tuple[str, str]]]:
    """Parse a file of GSLC paths/granule IDs into a structured DataFrame.

    Parameters
    ----------
    input_file:
        Text file with one S3 path, local path, or bare granule ID per line, or a
        ``nisar-db search`` results CSV (``filename``/``name`` plus ``url``).

    Returns
    -------
    df:
        One row per granule with parsed fields + derived columns.
    failed:
        List of (granule_name, error_message) for records that could not be parsed.

    """
    rows: list[dict] = []
    failed: list[tuple[str, str]] = []

    for name, https_url, s3_path in _read_entries(input_file):
        stem = name.removesuffix(".h5")
        try:
            g = GSLCFilename.from_path(stem)
            rows.append(
                {
                    "url": https_url,
                    "s3_path": s3_path,
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
            failed.append((name, str(exc)))

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
        .agg(lambda grp: dominant_value(grp, FAMILY_PRIORITY))
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
    """Parse a NISAR GSLC file list into a structured catalog CSV.

    Adds two derived columns per row:

    \b
      common_mode  most frequent mode for the (track, frame) pair
      is_full      True when coverage == 'F' (full frame)
    """  # noqa: D301
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

    write_catalog_csv(df, output)
    click.echo(f"\nCatalog written to: {output}")

    summary_path = output.with_name(output.stem + "_frame_summary.csv")
    write_catalog_csv(frame_summary, summary_path)
    click.echo(f"Frame summary written to: {summary_path}")


if __name__ == "__main__":
    main()
