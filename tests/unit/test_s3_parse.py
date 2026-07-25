"""Unit tests for S3 URI parsing and CMR link extraction."""

from __future__ import annotations

import pytest

from nisar_db.catalog._common import extract_urls
from nisar_db.search.s3 import parse_s3_uri

_DATA = "http://esipfed.org/ns/fedsearch/1.1/data#"
_BROWSE = "http://esipfed.org/ns/fedsearch/1.1/browse#"
_METADATA = "http://esipfed.org/ns/fedsearch/1.1/metadata#"


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("s3://bkt/products/L2_L_GSLC/", ("bkt", "products/L2_L_GSLC/")),
        ("bkt/products/L2_L_GSLC/", ("bkt", "products/L2_L_GSLC/")),
        ("s3://bkt//products/", ("bkt", "products/")),
        ("s3://bkt", ("bkt", "")),
        ("bkt", ("bkt", "")),
    ],
)
def test_parse_s3_uri(uri: str, expected: tuple[str, str]) -> None:
    assert parse_s3_uri(uri) == expected


class TestExtractUrls:
    def test_maps_rel_values_to_columns(self) -> None:
        links = [
            {"rel": _DATA, "href": "https://example.com/g.h5"},
            {"rel": _DATA, "href": "s3://bucket/g.h5"},
            {"rel": _BROWSE, "href": "https://example.com/g.png"},
            {"rel": _METADATA, "href": "https://example.com/g.iso.xml"},
        ]
        urls = extract_urls(links)
        assert urls["url"] == "https://example.com/g.h5"
        assert urls["s3_url"] == "s3://bucket/g.h5"
        assert urls["browse_url"] == "https://example.com/g.png"
        assert urls["metadata_url"] == "https://example.com/g.iso.xml"

    def test_empty_and_none_links(self) -> None:
        assert extract_urls([]) == {}
        assert extract_urls(None) == {}

    def test_unknown_rel_ignored(self) -> None:
        assert extract_urls([{"rel": "something-else", "href": "x"}]) == {}
