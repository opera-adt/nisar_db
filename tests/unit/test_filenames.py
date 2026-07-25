"""Unit tests for GSLC/GUNW granule-name parsing."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from nisar_db.filenames import GSLCFilename, GUNWFilename


class TestGSLCFilename:
    def test_parses_core_fields(self, gslc_name: str) -> None:
        g = GSLCFilename.from_path(gslc_name)
        assert g.mission == "NISAR"
        assert g.product == "L2"
        assert g.relative_orbit == "128"
        assert g.track_frame == "00129"
        assert g.mode == "4005"
        assert g.polarization == "HH"
        assert g.coverage == "F"

    def test_derived_properties(self, gslc_name: str) -> None:
        g = GSLCFilename.from_path(gslc_name)
        assert g.track == "128"
        assert g.frame == "00129"
        assert g.date == "20240601"
        assert g.scene_id == "T128_F00129_A"

    def test_accepts_h5_extension_and_full_path(self, gslc_name: str) -> None:
        from_ext = GSLCFilename.from_path(gslc_name + ".h5")
        from_path = GSLCFilename.from_path(f"s3://bucket/prefix/{gslc_name}.h5")
        assert from_ext.scene_id == from_path.scene_id == "T128_F00129_A"

    def test_optional_trailing_fields_default_empty(self) -> None:
        # 13-field minimum: no crid/orbits/coverage/location/version.
        minimal = (
            "NISAR_L_GSLC_L2_005_128_A_00129_4005_HH_ODC"
            "_20240601T000000_20240601T000030"
        )
        g = GSLCFilename.from_path(minimal)
        assert g.crid == ""
        assert g.coverage == ""
        assert g.version == ""

    @pytest.mark.parametrize("n_fields", [12, 19])
    def test_wrong_field_count_raises(self, n_fields: int) -> None:
        bad = "_".join(["X"] * n_fields)
        with pytest.raises(ValueError, match="fields"):
            GSLCFilename.from_path(bad)

    def test_frozen(self, gslc_name: str) -> None:
        g = GSLCFilename.from_path(gslc_name)
        with pytest.raises(FrozenInstanceError):
            g.mode = "2005"  # type: ignore[misc]


class TestGUNWFilename:
    def test_parses_reference_and_secondary_dates(self, gunw_name: str) -> None:
        u = GUNWFilename.from_path(gunw_name)
        assert u.track == "128"
        assert u.frame == "00129"
        assert u.ref_date == "20240601"
        assert u.sec_date == "20240613"
        assert u.date == "20240601_20240613"
        assert u.scene_id == "T128_F00129_A"

    @pytest.mark.parametrize("n_fields", [14, 21])
    def test_wrong_field_count_raises(self, n_fields: int) -> None:
        bad = "_".join(["X"] * n_fields)
        with pytest.raises(ValueError, match="fields"):
            GUNWFilename.from_path(bad)


def test_to_dataframe_single_row_with_derived_columns(gslc_name: str) -> None:
    df = GSLCFilename.from_path(gslc_name).to_dataframe()
    assert len(df) == 1
    row = df.iloc[0]
    # Derived columns are present and the raw ``path`` column is dropped.
    for col in ("date", "scene_id", "full_path"):
        assert col in df.columns
    assert "path" not in df.columns
    assert row["scene_id"] == "T128_F00129_A"
    assert row["date"] == "20240601"
