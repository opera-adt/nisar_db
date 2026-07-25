"""Regression tests: pin the parsed output for known granule names.

If the field layout of GSLCFilename/GUNWFilename changes, these golden
assertions fail loudly. Only meaningful, user-facing fields are pinned.
"""

from __future__ import annotations

from nisar_db.filenames import GSLCFilename, GUNWFilename

GSLC_GOLDEN = {
    "mission": "NISAR",
    "instrument": "L",
    "processing_type": "GSLC",
    "product": "L2",
    "cycle": "005",
    "relative_orbit": "128",
    "pass_direction": "A",
    "track_frame": "00129",
    "mode": "4005",
    "polarization": "HH",
    "source": "ODC",
    "crid": "P01234",
    "coverage": "F",
    "version": "v1.0",
    # Derived
    "track": "128",
    "frame": "00129",
    "date": "20240601",
    "scene_id": "T128_F00129_A",
}

GUNW_GOLDEN = {
    "mission": "NISAR",
    "processing_type": "GUNW",
    "relative_orbit": "128",
    "track_frame": "00129",
    "cycle1": "005",
    "cycle2": "006",
    "mode": "4005",
    "polarization": "HH",
    "coverage": "F",
    "crid": "P01234",
    # Derived
    "ref_date": "20240601",
    "sec_date": "20240613",
    "date": "20240601_20240613",
    "scene_id": "T128_F00129_A",
}


# Parse the realistic ".h5" form: a bare name ending in "vX.Y" would lose the
# ".Y" to Path.stem, so real granules (always ".h5") are the fixture of record.
def test_gslc_golden(gslc_name: str) -> None:
    g = GSLCFilename.from_path(gslc_name + ".h5")
    actual = {key: getattr(g, key) for key in GSLC_GOLDEN}
    assert actual == GSLC_GOLDEN


def test_gunw_golden(gunw_name: str) -> None:
    u = GUNWFilename.from_path(gunw_name + ".h5")
    actual = {key: getattr(u, key) for key in GUNW_GOLDEN}
    assert actual == GUNW_GOLDEN


def test_gslc_datetimes(gslc_name: str) -> None:
    g = GSLCFilename.from_path(gslc_name + ".h5")
    assert g.start_datetime.isoformat() == "2024-06-01T00:00:00"
    assert g.end_datetime.isoformat() == "2024-06-01T00:00:30"
