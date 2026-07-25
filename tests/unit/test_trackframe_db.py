"""Unit tests for the TrackFrame database fetch helpers (no real network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nisar_db import geodb
from nisar_db.filenames import NISAR_DB_GRANULE_ID


def test_get_trackframe_db_returns_gpkg_path(tmp_path: Path, monkeypatch) -> None:
    gpkg = tmp_path / "NISAR_TrackFrame_L_20250909.gpkg"
    calls: dict = {}

    def _fake_download(granule_id: str, **kwargs) -> list[str]:
        calls.update(granule_id=granule_id, **kwargs)
        return [str(gpkg)]

    monkeypatch.setattr(geodb, "download_earthdata_granule", _fake_download)

    assert geodb.get_trackframe_db(output_dir=tmp_path) == gpkg
    assert calls["granule_id"] == NISAR_DB_GRANULE_ID
    # The granule lists a browse page under the same CMR relation as the
    # GeoPackage; without this the browse page lands as a stray HTML file.
    assert calls["filename_suffix"] == ".gpkg"
    assert calls["skip_existing"] is True


def test_get_trackframe_db_honors_explicit_granule(tmp_path: Path, monkeypatch) -> None:
    seen: dict = {}

    def _fake_download(granule_id: str, **kwargs) -> list[str]:
        seen["granule_id"] = granule_id
        seen["skip_existing"] = kwargs["skip_existing"]
        return [str(tmp_path / "old.gpkg")]

    monkeypatch.setattr(geodb, "download_earthdata_granule", _fake_download)

    geodb.get_trackframe_db(tmp_path, skip_existing=False, granule_id="G123-ASF")
    assert seen == {"granule_id": "G123-ASF", "skip_existing": False}


def test_get_trackframe_db_raises_when_nothing_downloaded(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(geodb, "download_earthdata_granule", lambda *a, **kw: [])

    with pytest.raises(RuntimeError, match="No GeoPackage downloaded"):
        geodb.get_trackframe_db(output_dir=tmp_path)
