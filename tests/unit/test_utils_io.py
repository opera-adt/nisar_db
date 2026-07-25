"""Unit tests for small utilities and JSON writers."""

from __future__ import annotations

import json
import zipfile
from datetime import datetime
from pathlib import Path

from nisar_db.io_json import write_catalog_json, write_zipped_json
from nisar_db.utils import parts_str


def test_parts_str_formats_nisar_timestamp() -> None:
    assert parts_str(datetime(2024, 6, 1, 12, 30, 45)) == "20240601T123045"


def test_write_zipped_json_writes_plain_and_zip(tmp_path: Path) -> None:
    out = tmp_path / "frames.json"
    data = {"frames": {"T128_A": [1, 2, 3]}}

    zip_path = write_zipped_json(out, data)

    assert out.exists()
    assert zip_path == str(out) + ".zip"
    assert Path(zip_path).exists()
    assert json.loads(out.read_text()) == data
    with zipfile.ZipFile(zip_path) as zf:
        assert json.loads(zf.read(out.name)) == data


def test_write_zipped_json_can_skip_plain(tmp_path: Path) -> None:
    out = tmp_path / "frames.json"
    write_zipped_json(out, {"a": 1}, write_plain=False)
    assert not out.exists()
    assert Path(str(out) + ".zip").exists()


def test_write_zipped_json_serialises_non_json_via_str(tmp_path: Path) -> None:
    out = tmp_path / "d.json"
    write_zipped_json(out, {"when": datetime(2024, 1, 1)})
    assert "2024-01-01" in out.read_text()


def test_write_catalog_json_stamps_generated_at(tmp_path: Path) -> None:
    write_catalog_json(tmp_path, "tracks.json", "tracks", {"128": ["A"]})
    payload = json.loads((tmp_path / "tracks.json").read_text())
    assert payload["tracks"] == {"128": ["A"]}
    assert "generated_at" in payload
    # generated_at must be an ISO-8601 timestamp.
    datetime.fromisoformat(payload["generated_at"])
