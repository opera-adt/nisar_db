"""Unit tests for NISARProduct construction from S3 keys and CMR items."""

from __future__ import annotations

from nisar_db.search.models import NISARProduct, ProductType, UrlType


class TestFromS3Key:
    def test_gslc_fields_from_key(self, gslc_name: str) -> None:
        key = f"products/L2_L_GSLC/{gslc_name}.h5"
        p = NISARProduct.from_s3_key("bkt", key, size=2_500_000_000)
        assert p.product_type is ProductType.GSLC
        assert p.url_type is UrlType.S3
        assert p.url == f"s3://bkt/{key}"
        assert p.track == 128
        assert p.frame == 129
        assert p.direction == "A"
        assert p.polarization == "HH"
        assert p.full_frame is True
        assert p.metadata["s3_key"] == key
        assert p.metadata["s3_size_bytes"] == 2_500_000_000

    def test_gunw_detected_from_name(self, gunw_name: str) -> None:
        p = NISARProduct.from_s3_key("bkt", f"a/b/{gunw_name}.h5")
        assert p.product_type is ProductType.GUNW
        assert p.track == 128
        assert p.frame == 129

    def test_size_optional(self, gslc_name: str) -> None:
        p = NISARProduct.from_s3_key("bkt", f"{gslc_name}.h5")
        assert "s3_size_bytes" not in p.metadata


class TestFromCmrItem:
    def _item(self, title: str) -> dict:
        return {
            "meta": {"concept-id": "G123-ASF"},
            "umm": {
                "GranuleUR": title,
                "RelatedUrls": [
                    {
                        "URL": "https://example.com/data/" + title + ".h5",
                        "Type": "GET DATA",
                    },
                    {
                        "URL": "s3://bucket/data/" + title + ".h5",
                        "Type": "GET DATA VIA DIRECT ACCESS",
                    },
                ],
                "TemporalExtent": {
                    "RangeDateTime": {
                        "BeginningDateTime": "2024-06-01T00:00:00.000Z",
                        "EndingDateTime": "2024-06-01T00:00:30.000Z",
                    }
                },
            },
        }

    def test_https_url_selected_by_default(self) -> None:
        p = NISARProduct.from_cmr_item(self._item("NISAR_GSLC_scene"))
        assert p.granule_id == "G123-ASF"
        assert p.product_type is ProductType.GSLC
        assert p.url.startswith("https://")
        assert p.filename.endswith(".h5")

    def test_s3_url_selected_when_requested(self) -> None:
        p = NISARProduct.from_cmr_item(
            self._item("NISAR_GSLC_scene"), url_type=UrlType.S3
        )
        assert p.url.startswith("s3://")

    def test_product_type_gunw_from_title(self) -> None:
        p = NISARProduct.from_cmr_item(self._item("NISAR_GUNW_scene"))
        assert p.product_type is ProductType.GUNW


class TestMetadataExtractors:
    def test_attributes_and_crid(self) -> None:
        umm = {
            "AdditionalAttributes": [
                {"Name": "TRACK_NUMBER", "Values": ["128"]},
                {"Name": "FRAME_NUMBER", "Values": ["129"]},
                {"Name": "ASCENDING_DESCENDING", "Values": ["ASCENDING"]},
                {"Name": "CYCLE_NUMBER", "Values": ["5"]},
                {"Name": "POLARIZATION", "Values": ["HH"]},
                {"Name": "FULL_FRAME", "Values": ["true"]},
                {"Name": "JOINT_OBSERVATION", "Values": ["FALSE"]},
            ],
            "DataGranule": {
                "Identifiers": [
                    {"IdentifierType": "CRID", "Identifier": "P01234"},
                    {"IdentifierType": "Other", "Identifier": "ignore"},
                ]
            },
        }
        attrs = NISARProduct.extract_attributes_from_metadata(umm)
        assert attrs["track"] == 128
        assert attrs["frame"] == 129
        assert attrs["direction"] == "A"
        assert attrs["cycle"] == 5
        assert attrs["full_frame"] is True
        assert attrs["joint_observation"] is False
        assert attrs["crid"] == "P01234"

    def test_attributes_empty_when_absent(self) -> None:
        attrs = NISARProduct.extract_attributes_from_metadata({})
        assert all(v is None for v in attrs.values())

    def test_bbox_from_bounding_rectangle(self) -> None:
        umm = {
            "SpatialExtent": {
                "HorizontalSpatialDomain": {
                    "Geometry": {
                        "BoundingRectangles": [
                            {
                                "WestBoundingCoordinate": -120.0,
                                "SouthBoundingCoordinate": 30.0,
                                "EastBoundingCoordinate": -118.0,
                                "NorthBoundingCoordinate": 32.0,
                            }
                        ]
                    }
                }
            }
        }
        assert NISARProduct.extract_bbox_from_metadata(umm) == (
            -120.0,
            30.0,
            -118.0,
            32.0,
        )

    def test_bbox_none_when_absent(self) -> None:
        assert NISARProduct.extract_bbox_from_metadata({}) is None
