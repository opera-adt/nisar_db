"""Create blackout dates for NISAR frames.

This script:
1. Reads a GeoJSON or Parquet file with blackout periods for NISAR frames
2. Processes the data to create blackout dates
3. Saves the results as a JSON file

The blackout dates indicate periods when data for certain frames should not be
processed, typically due to environmental conditions like snow or extreme weather.
"""

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Union

import geopandas as gpd
import numpy as np
import pandas as pd

from nisar_db.logging_setup import configure_logging

logger = configure_logging("create_blackout_dates")


def _yearly_windows(
    start_ts: pd.Timestamp,
    end_ts: pd.Timestamp,
    years: range,
) -> List[List[str]]:
    """Generate blackout windows for every year in *years*.

    If the end month/day occurs *earlier* in the calendar than the start
    month/day (e.g. Nov-01 ➜ May-31), the end year is `year + 1`.

    Parameters
    ----------
    start_ts : pd.Timestamp
        Start timestamp with month and day information.
    end_ts : pd.Timestamp
        End timestamp with month and day information.
    years : range
        Range of years to generate windows for.

    Returns
    -------
    List[List[str]]
        List of date ranges as [start, end] pairs in ISO format.

    """
    s_month, s_day = start_ts.month, start_ts.day
    e_month, e_day = end_ts.month, end_ts.day

    windows: List[List[str]] = []
    for yr in years:
        start = pd.Timestamp(year=yr, month=s_month, day=s_day)
        # If end date is earlier in the calendar than start date, end year is next year
        end_year = yr + (e_month < s_month or (e_month == s_month and e_day < s_day))
        # End timestamp is end of day
        end = pd.Timestamp(year=end_year, month=e_month, day=e_day) + pd.Timedelta(
            hours=23, minutes=59, seconds=59
        )
        windows.append([start.isoformat(), end.isoformat()])
    return windows


def _select_blackout_dates(
    gdf: gpd.GeoDataFrame, max_default_duration: float
) -> gpd.GeoDataFrame:
    """Select appropriate blackout dates based on duration threshold.

    Parameters
    ----------
    gdf : gpd.GeoDataFrame
        GeoDataFrame with blackout dates information.
    max_default_duration : float
        Maximum number of days for default blackout period.

    Returns
    -------
    gpd.GeoDataFrame
        GeoDataFrame with selected blackout dates.

    """
    use_aggressive_mask = gdf.blackout_duration_median > max_default_duration
    gdf["start_selected"] = np.where(
        use_aggressive_mask,
        gdf["start_aggressive"],
        gdf["start_median"],
    )
    gdf["end_selected"] = np.where(
        use_aggressive_mask,
        gdf["end_aggressive"],
        gdf["end_median"],
    )
    gdf["blackout_duration_selected"] = np.where(
        use_aggressive_mask,
        gdf["blackout_duration_aggressive"],
        gdf["blackout_duration_median"],
    )
    gdf["mode_selected"] = np.where(use_aggressive_mask, "aggressive", "median")
    return gdf


def snow_months_to_blackout_json(
    input_file: Union[Path, str],
    output_file: Optional[Union[Path, str]] = None,
    max_default_duration: float = 180,
    year_range: Optional[List[int]] = None,
) -> Dict:
    """Create a JSON of blackout periods from snow month analysis data.

    Parameters
    ----------
    input_file : Path or str
        Path to the input file (Parquet or GeoJSON).
    output_file : Path or str, optional
        Path to the output JSON file. If None, a default name is generated.
    max_default_duration : float, optional
        The maximum number of days for the default blackout period, by default 180.
    year_range : List[int], optional
        Range of years to generate blackout dates for, by default None.

    Returns
    -------
    Dict
        Dictionary containing blackout dates for each frame.

    """
    input_path = Path(input_file)
    generation_time = datetime.now().strftime("%Y-%m-%d")
    output_filename = output_file or f"nisar-blackout-dates-{generation_time}.json"

    result: Dict[str, object] = {
        "metadata": {
            "generation_time": datetime.now().isoformat(),
            "max_default_duration": max_default_duration,
            "input_file": input_path.name,
            "output_file": str(output_filename),
        },
    }

    # Load the snow-analysis table
    if input_path.suffix == ".parquet":
        gdf = gpd.read_parquet(input_file)
    else:
        gdf = gpd.read_file(input_file)

    if "start_selected" not in gdf.columns:
        gdf = _select_blackout_dates(gdf, max_default_duration)

    # Span of calendar years to pre-compute
    if year_range is None:
        years = range(2025, 2030)  # Default: 5 years starting from 2025 for NISAR
    else:
        years = range(year_range[0], year_range[1] + 1)

    blackout_dates: Dict[str, List[List[str]]] = {}
    for tup in gdf.itertuples():
        # skip rows missing a valid window
        if pd.isna(tup.start_selected) or pd.isna(tup.end_selected):
            continue

        blackout_dates[str(tup.frame_id)] = _yearly_windows(
            tup.start_selected, tup.end_selected, years
        )

    result["blackout_dates"] = blackout_dates

    # Save the result to a JSON file
    with open(output_filename, "w") as f:
        json.dump(result, f, indent=2)

    logger.info(
        "Blackout JSON created: %s (%d frames)", output_filename, len(blackout_dates)
    )
    return result


def monthly_data_to_blackout_json(
    input_file: Union[Path, str],
    output_file: Optional[Union[Path, str]] = None,
) -> Dict:
    """Create a JSON of blackout periods from monthly data.

    Parameters
    ----------
    input_file : Path or str
        Path to the input GeoJSON file.
    output_file : Path or str, optional
        Path to the output JSON file. If None, a default name is generated.

    Returns
    -------
    Dict
        Dictionary containing blackout dates for each frame.

    """
    input_path = Path(input_file)
    generation_time = datetime.now().strftime("%Y-%m-%d")
    output_filename = output_file or f"nisar-blackout-dates-{generation_time}.json"

    # Read the GeoJSON file
    gdf = gpd.read_file(input_file)

    blackout_dates: Dict[str, List[List[str]]] = {}

    for frame_id, group in gdf.groupby("frame_id"):
        frame_dates = []
        blackout_start = None

        # Sort the group by year and month
        group_sorted = group.sort_values(["year", "month"])

        for _, row in group_sorted.iterrows():
            year = int(row["year"])
            month = int(row["month"])
            to_process = int(row["to_process"])

            current_date = datetime(year, month, 1)

            if to_process == 0:
                if blackout_start is None:
                    blackout_start = current_date
            else:
                if blackout_start is not None:
                    # End the blackout period at the very end of the previous month
                    blackout_end = current_date - timedelta(days=1)
                    blackout_end = blackout_end.replace(hour=23, minute=59, second=59)
                    frame_dates.append(
                        [blackout_start.isoformat(), blackout_end.isoformat()]
                    )
                    blackout_start = None

        # Check if there's an ongoing blackout period at the end of the data
        if blackout_start is not None:
            last_row = group_sorted.iloc[-1]
            last_year = int(last_row["year"])
            last_month = int(last_row["month"])

            # Get the last day of the month
            if last_month == 12:
                blackout_end = datetime(last_year, 12, 31, 23, 59, 59)
            else:
                next_month = datetime(last_year, last_month + 1, 1)
                blackout_end = next_month - timedelta(days=1)
                blackout_end = blackout_end.replace(hour=23, minute=59, second=59)

            frame_dates.append([blackout_start.isoformat(), blackout_end.isoformat()])

        blackout_dates[str(frame_id)] = frame_dates or []

    result = {
        "metadata": {
            "generation_time": datetime.now().isoformat(),
            "input_file": input_path.name,
            "output_file": str(output_filename),
        },
        "blackout_dates": blackout_dates,
    }

    # Save the result to a JSON file
    with open(output_filename, "w") as f:
        json.dump(result, f, indent=2)

    logger.info(
        f"Blackout JSON created: {output_filename} ({len(blackout_dates)} frames)"
    )

    return result


def manual_blackout_dates(
    output_file: Optional[Union[Path, str]] = None,
    year_range: Optional[List[int]] = None,
    blackout_periods: Optional[Dict[str, List[Dict[str, int]]]] = None,
) -> Dict:
    """Create a JSON of blackout periods from manually defined periods.

    Parameters
    ----------
    output_file : Path or str, optional
        Path to the output JSON file. If None, a default name is generated.
    year_range : List[int], optional
        Range of years to generate blackout dates for, by default None.
    blackout_periods : Dict[str, List[Dict[str, int]]], optional
        Manually defined blackout periods by frame ID. Month/day values are
        integers (consumed by ``pandas.Timestamp``). Format:
        {
            "frame_id": [
                {
                    "start_month": 11, "start_day": 1,
                    "end_month": 5, "end_day": 31,
                }
            ]
        }

    Returns
    -------
    Dict
        Dictionary containing blackout dates for each frame.

    """
    generation_time = datetime.now().strftime("%Y-%m-%d")
    output_filename = (
        output_file or f"nisar-manual-blackout-dates-{generation_time}.json"
    )

    # Span of calendar years to pre-compute
    if year_range is None:
        years = range(2025, 2030)  # Default: 5 years starting from 2025 for NISAR
    else:
        years = range(year_range[0], year_range[1] + 1)

    # Default blackout periods if none provided
    if blackout_periods is None:
        # Example: Northern US states have snow cover from November to May
        blackout_periods = {
            # Example frames with winter snow cover
            "1001": [
                {"start_month": 11, "start_day": 1, "end_month": 5, "end_day": 31}
            ],
            "1002": [
                {"start_month": 11, "start_day": 15, "end_month": 4, "end_day": 30}
            ],
            # Example tropical frames with monsoon season
            "2001": [{"start_month": 6, "start_day": 1, "end_month": 9, "end_day": 30}],
        }

    blackout_dates: Dict[str, List[List[str]]] = {}

    for frame_id, periods in blackout_periods.items():
        frame_blackout = []
        for period in periods:
            s_month = period["start_month"]
            s_day = period["start_day"]
            e_month = period["end_month"]
            e_day = period["end_day"]

            start_ts = pd.Timestamp(year=2000, month=s_month, day=s_day)
            end_ts = pd.Timestamp(year=2000, month=e_month, day=e_day)

            windows = _yearly_windows(start_ts, end_ts, years)
            frame_blackout.extend(windows)

        blackout_dates[frame_id] = frame_blackout

    result = {
        "metadata": {
            "generation_time": datetime.now().isoformat(),
            "output_file": str(output_filename),
            "type": "manual",
            "year_range": [min(years), max(years)],
        },
        "blackout_dates": blackout_dates,
    }

    # Save the result to a JSON file
    with open(output_filename, "w") as f:
        json.dump(result, f, indent=2)

    logger.info(
        f"Manual blackout JSON created: {output_filename} "
        f"({len(blackout_dates)} frames)"
    )

    return result


def main():
    """Create the NISAR blackout-dates JSON from the command line."""
    parser = argparse.ArgumentParser(
        description="Create blackout dates for NISAR frames"
    )

    parser.add_argument("--input-file", help="Path to input file (GeoJSON or Parquet)")
    parser.add_argument("--output-file", help="Path to output JSON file")
    parser.add_argument(
        "--max-default-duration",
        type=float,
        default=180,
        help="Maximum number of days for default blackout period",
    )
    parser.add_argument(
        "--start-year",
        type=int,
        default=2025,
        help="Start year for blackout date generation",
    )
    parser.add_argument(
        "--end-year",
        type=int,
        default=2030,
        help="End year for blackout date generation",
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Create manual blackout dates without input file",
    )
    parser.add_argument(
        "--monthly", action="store_true", help="Use monthly data format for input file"
    )

    args = parser.parse_args()

    year_range = [args.start_year, args.end_year]

    if args.manual:
        return manual_blackout_dates(
            output_file=args.output_file,
            year_range=year_range,
        )
    elif args.input_file:
        if args.monthly:
            return monthly_data_to_blackout_json(
                input_file=args.input_file,
                output_file=args.output_file,
            )
        else:
            return snow_months_to_blackout_json(
                input_file=args.input_file,
                output_file=args.output_file,
                max_default_duration=args.max_default_duration,
                year_range=year_range,
            )
    else:
        logger.error("Either --input-file or --manual must be specified")
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
