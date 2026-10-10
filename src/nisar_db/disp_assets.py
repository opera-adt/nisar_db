"""``nisar-db build-disp-assets``: build the DISP-NISAR release assets in one go.

The same steps, commands and file names as the release workflow
(``.github/workflows/release.yml``), so a local or on-demand build matches a
published release:

1. download the NISAR TrackFrame database (or reuse ``--trackframe-gpkg``);
2. frame-to-bounds and simplified frame geometries;
3. every GSLC in CMR (S3 URLs) and the North America GSLC catalog;
4. blackout dates: from ``--snow-geojson``, else ``--blackout-file`` (the
   repo's ``catalog/opera-nisar-disp-blackout-dates.json``), else the default
   seasonal windows;
5. consistent-GSLC, with and without blackouts;
6. processing-mode labels (diffed against ``--previous-consistent``);
7. reference dates.

It needs network access and, for the TrackFrame database, an Earthdata login
in ``~/.netrc``; a full CMR crawl takes minutes.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Callable

import click

#: ``run(argv, cwd)`` runs one ``nisar-db`` command; tests pass a fake.
Runner = Callable[[list[str], Path], None]


def default_run(argv: list[str], cwd: Path) -> None:
    """Run ``nisar-db <argv>`` in ``cwd`` with this Python, failing loudly."""
    click.echo(f"$ nisar-db {' '.join(argv)}", err=True)
    subprocess.run([sys.executable, "-m", "nisar_db.cli", *argv], cwd=cwd, check=True)


def _gslc_file_list(search_csv: Path, out: Path) -> int:
    import pandas as pd

    urls = pd.read_csv(search_csv)["url"].dropna().astype(str)
    urls = urls[urls.str.startswith("s3://")]
    urls.to_csv(out, index=False, header=False)
    return len(urls)


def build_disp_assets(
    out_dir: Path,
    *,
    trackframe_gpkg: Path | None = None,
    blackout_file: Path | None = None,
    snow_geojson: Path | None = None,
    previous_consistent: Path | None = None,
    max_results: int = 0,
    version: str | None = None,
    day: str | None = None,
    run: Runner = default_run,
) -> dict[str, str]:
    """Build the DISP-NISAR assets into ``out_dir``; return them by kind.

    Raises
    ------
    subprocess.CalledProcessError
        When a step fails (the default runner).
    FileNotFoundError
        When a step did not write the file the next one needs.

    """
    from nisar_db import __version__

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    day = day or date.today().isoformat()
    version = version or __version__.split("+")[0]
    gpkg = out / "NISAR_TrackFrame_L.gpkg"
    if trackframe_gpkg is not None:
        shutil.copyfile(trackframe_gpkg, gpkg)
    else:
        run(["download-frame-db"], out)
        found = sorted(out.glob("NISAR_TrackFrame_L_*.gpkg"))
        if not found:
            raise FileNotFoundError(
                "download-frame-db wrote no NISAR_TrackFrame_L_*.gpkg"
            )
        shutil.copyfile(found[-1], gpkg)

    names = {
        "frame_to_bounds": f"opera-nisar-disp-frame-to-bounds-{day}.json",
        "frame_geometries": (
            f"opera-nisar-disp-frame-geometries-simple-{version}.geojson"
        ),
        "blackout_dates": f"opera-nisar-disp-blackout-dates-{day}.json",
        "consistent_gslc": f"opera-nisar-disp-consistent-gslc-{day}.json",
        "consistent_gslc_no_blackout": (
            "opera-nisar-disp-consistent-gslc-no-blackout.json"
        ),
        "consistent_with_processing_mode": (
            f"opera-nisar-disp-consistent-gslc-with-processing-mode-{day}.json"
        ),
        "reference_dates": f"opera-nisar-disp-reference-dates-{day}.json",
        "gslc_catalog": "gslc_catalog.csv",
        "frames_gpkg": "opera-nisar-disp-frames.gpkg",
    }
    run(
        [
            "create-frame-to-bound",
            "--nisar-gpkg",
            gpkg.name,
            "--output",
            names["frame_to_bounds"],
            "--geojson",
            names["frame_geometries"],
        ],
        out,
    )
    run(
        [
            "search",
            "--product-type",
            "GSLC",
            "--url-type",
            "s3",
            "--max-results",
            str(max_results),
            "--output-csv",
            "gslc_search.csv",
        ],
        out,
    )
    n_files = _gslc_file_list(out / "gslc_search.csv", out / "gslc_files.txt")
    click.echo(f"{n_files} GSLC S3 paths", err=True)
    run(
        [
            "create-gslc-csv",
            "--input",
            "gslc_files.txt",
            "--output",
            names["gslc_catalog"],
            "--na-only",
            "--nisar-gpkg",
            gpkg.name,
        ],
        out,
    )

    blackout = names["blackout_dates"]
    if snow_geojson is not None:
        run(
            [
                "create-blackout-dates",
                "--input-file",
                str(Path(snow_geojson).resolve()),
                "--output-file",
                blackout,
            ],
            out,
        )
    elif blackout_file is not None:
        shutil.copyfile(blackout_file, out / blackout)
    else:
        run(["create-blackout-dates", "--manual", "--output-file", blackout], out)

    common = ["--catalog", names["gslc_catalog"], "--nisar-gpkg", names["frames_gpkg"]]
    run(
        [
            "create-consistent",
            *common,
            "--output",
            names["consistent_gslc_no_blackout"],
        ],
        out,
    )
    run(
        [
            "create-consistent",
            *common,
            "--blackout-file",
            blackout,
            "--output",
            names["consistent_gslc"],
        ],
        out,
    )
    label = [
        "label-processing-mode",
        "--consistent-json",
        names["consistent_gslc"],
        "--output",
        names["consistent_with_processing_mode"],
    ]
    if previous_consistent is not None:
        label += ["--previous-json", str(Path(previous_consistent).resolve())]
    run(label, out)
    run(
        [
            "create-reference-dates",
            "--consistent-json",
            names["consistent_gslc"],
            "--blackout-file",
            blackout,
            "--output",
            names["reference_dates"],
        ],
        out,
    )

    missing = [n for n in names.values() if not (out / n).exists()]
    if missing:
        raise FileNotFoundError(f"steps did not write {missing}")
    return names


@click.command("build-disp-assets")
@click.option(
    "--out-dir", type=click.Path(path_type=Path), default=Path(), show_default=True
)
@click.option(
    "--trackframe-gpkg",
    type=click.Path(path_type=Path, exists=True),
    default=None,
    help="Reuse a TrackFrame GeoPackage instead of downloading it.",
)
@click.option(
    "--blackout-file",
    type=click.Path(path_type=Path, exists=True),
    default=None,
    help=(
        "Blackout-dates JSON to use "
        "(e.g. catalog/opera-nisar-disp-blackout-dates.json)."
    ),
)
@click.option(
    "--snow-geojson",
    type=click.Path(path_type=Path, exists=True),
    default=None,
    help="Derive the blackout dates from this snow analysis instead.",
)
@click.option(
    "--previous-consistent",
    type=click.Path(path_type=Path, exists=True),
    default=None,
    help="Previous release's consistent-GSLC JSON, to label changed frames.",
)
@click.option(
    "--max-results",
    type=int,
    default=0,
    show_default=True,
    help="Cap on CMR results; 0 = all.",
)
@click.option(
    "--version", "version", default=None, help="Version in the geometry file name."
)
def main(
    out_dir,
    trackframe_gpkg,
    blackout_file,
    snow_geojson,
    previous_consistent,
    max_results,
    version,
):
    """Build the DISP-NISAR release assets (as the release workflow does)."""
    names = build_disp_assets(
        out_dir,
        trackframe_gpkg=trackframe_gpkg,
        blackout_file=blackout_file,
        snow_geojson=snow_geojson,
        previous_consistent=previous_consistent,
        max_results=max_results,
        version=version,
    )
    for kind, name in names.items():
        click.echo(f"{kind}: {Path(out_dir) / name}")
