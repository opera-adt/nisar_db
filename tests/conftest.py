"""Shared fixtures for the nisar_db test suite.

Fixtures here provide deterministic, network-free inputs: valid GSLC/GUNW
granule names, a synthetic S3-derived product list, and a small in-memory
catalog DataFrame. Tests must stay order-independent (pytest-randomly is
active) and warning-clean (``filterwarnings = ["error"]``).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from nisar_db.search.models import NISARProduct

# A valid 18-field GSLC granule name and 20-field GUNW granule name. Field
# layout matches nisar_db.filenames.{GSLCFilename,GUNWFilename}.from_path.
GSLC_NAME = (
    "NISAR_L_GSLC_L2_005_128_A_00129_4005_HH_ODC"
    "_20240601T000000_20240601T000030_P01234_001_F_NA_v1.0"
)
GUNW_NAME = (
    "NISAR_L_GUNW_L2_005_128_A_00129_006_4005_HH"
    "_20240601T000000_20240601T000030_20240613T000000_20240613T000030"
    "_P01234_001_F_NA_v1.0"
)


@pytest.fixture
def gslc_name() -> str:
    """Return a valid GSLC granule name (no extension)."""
    return GSLC_NAME


@pytest.fixture
def gunw_name() -> str:
    """Return a valid GUNW granule name (no extension)."""
    return GUNW_NAME


@pytest.fixture
def s3_products() -> list[NISARProduct]:
    """Two GSLC products parsed from synthetic S3 keys (distinct crids)."""
    key_old = f"products/L2_L_GSLC/{GSLC_NAME}.h5"
    key_new = key_old.replace("_P01234_", "_P09999_")
    return [
        NISARProduct.from_s3_key(
            "my-bucket",
            key_old,
            size=2_000_000_000,
            last_modified=datetime(2024, 6, 2, 12, 0, tzinfo=timezone.utc),
        ),
        NISARProduct.from_s3_key(
            "my-bucket",
            key_new,
            size=3_000_000_000,
            last_modified=datetime(2024, 9, 30, 12, 0, tzinfo=timezone.utc),
        ),
    ]


@pytest.fixture
def consistent_catalog_df() -> pd.DataFrame:
    """Synthetic GSLC catalog exercising the mode/coverage selection rules.

    Frame (128, 129): standard mode 4005 dominates (2xF, 1xP) plus a 2005 F and
    a same-date duplicate -> keeps the two 4005 F dates. Frame (200, 300): only
    the non-standard mode 7700 (1xF, 2xP) -> falls back to 7700 P (two dates).
    """
    rows = [
        # (track, frame, mode, coverage, sensing_time)
        (128, 129, "4005", "F", "2024-06-01T00:00:00"),
        (128, 129, "4005", "F", "2024-06-01T00:05:00"),  # dup date -> dropped
        (128, 129, "4005", "F", "2024-06-13T00:00:00"),
        (128, 129, "4005", "P", "2024-06-25T00:00:00"),  # loses coverage vote
        (128, 129, "2005", "F", "2024-07-01T00:00:00"),  # loses mode vote
        (200, 300, "7700", "F", "2024-06-02T00:00:00"),  # non-standard, minority
        (200, 300, "7700", "P", "2024-06-14T00:00:00"),
        (200, 300, "7700", "P", "2024-06-26T00:00:00"),
    ]
    df = pd.DataFrame(
        rows, columns=["track", "frame", "mode", "coverage", "sensing_time"]
    )
    df["sensing_date"] = pd.to_datetime(df["sensing_time"]).dt.strftime("%Y-%m-%d")
    return df
