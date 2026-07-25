"""Integration tests for the shared catalog helpers (DuckDB + JSON)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

from nisar_db.catalog._common import (
    create_database,
    extract_metadata,
    generate_track_frame_json,
    update_database,
)
from nisar_db.filenames import GSLCFilename

_LOGGER = logging.getLogger("test.catalog_common")
_DATA_REL = "http://esipfed.org/ns/fedsearch/1.1/data#"


def test_extract_metadata_from_cmr_rows(gslc_name: str) -> None:
    search_df = pd.DataFrame(
        [
            {
                "granule_id": "G123-ASF",
                "title": gslc_name + ".h5",
                "links": [
                    {"rel": _DATA_REL, "href": "https://example.com/g.h5"},
                    {"rel": _DATA_REL, "href": "s3://bucket/g.h5"},
                ],
            },
            {"granule_id": "G-bad", "title": "not-a-granule", "links": []},
        ]
    )
    out = extract_metadata(search_df, GSLCFilename, _LOGGER)

    # The unparseable title is skipped; one good row remains.
    assert len(out) == 1
    row = out.iloc[0]
    assert row["id"] == gslc_name
    assert row["granule_id"] == "G123-ASF"
    assert row["url"] == "https://example.com/g.h5"
    assert row["s3_url"] == "s3://bucket/g.h5"
    assert row["mode"] == "4005"


def test_create_update_and_generate_json(tmp_path: Path) -> None:
    db_path = str(tmp_path / "catalog.duckdb")
    # INSERT OR REPLACE (used by update_database) requires a PRIMARY KEY, matching
    # the real GSLC/GUNW schemas (id VARCHAR PRIMARY KEY, ...).
    schema = (
        "id VARCHAR PRIMARY KEY, track VARCHAR, frame VARCHAR, pass_direction VARCHAR"
    )
    conn = create_database(db_path, "products", schema)
    try:
        metadata_df = pd.DataFrame(
            {
                "id": ["a", "b", "c"],
                "track": ["128", "128", "5"],
                "frame": ["129", "130", "9"],
                "pass_direction": ["A", "A", "D"],
            }
        )
        update_database(conn, metadata_df, "products", _LOGGER)
        assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 3

        generate_track_frame_json(conn, "products", str(tmp_path), "opera-nisar")
    finally:
        conn.close()

    tracks = json.loads((tmp_path / "opera-nisar_tracks.json").read_text())
    frames = json.loads((tmp_path / "opera-nisar_frames.json").read_text())

    assert tracks["tracks"] == {"128": ["A"], "5": ["D"]}
    assert frames["frames"]["T128_A"] == ["129", "130"]
    assert frames["frames"]["T5_D"] == ["9"]


def test_create_database_is_idempotent(tmp_path: Path) -> None:
    db_path = str(tmp_path / "c.duckdb")
    schema = "track VARCHAR, frame VARCHAR, pass_direction VARCHAR"
    create_database(db_path, "products", schema).close()
    # Re-opening with CREATE TABLE IF NOT EXISTS must not raise.
    conn = create_database(db_path, "products", schema)
    conn.close()
