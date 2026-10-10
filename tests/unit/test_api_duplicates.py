"""Duplicates over HTTP (/api/v1) and over MCP (/mcp)."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("mcp")

from fastapi.testclient import TestClient

from nisar_db.api.app import create_app
from nisar_db.api.settings import Settings


def _name(date: str, start: str, crid: str) -> str:
    d = date.replace("-", "")
    return (
        f"NISAR_L2_PR_GSLC_005_007_A_007_2005_DHDH_A_{d}T{start}_{d}T{start}"
        f"_{crid}_N_F_J_001"
    )


def _dup_frame() -> dict:
    granules = [
        {
            "gid": _name(d, s, c),
            "date": d,
            "mode": "2005",
            "cov": "F",
            "pol": "DHDH",
            "cycle": 5,
        }
        for d, s, c in [
            ("2026-03-01", "010538", "P05012"),
            ("2026-03-01", "010538", "P05023"),
            ("2026-03-13", "010538", "P05023"),
            ("2026-03-25", "010538", "P05023"),
            ("2026-03-25", "010610", "P05023"),
        ]
    ]
    ring = [[-100, 40], [-99, 40], [-99, 41], [-100, 41], [-100, 40]]
    return {
        "type": "Feature",
        "geometry": {"type": "MultiPolygon", "coordinates": [[ring]]},
        "properties": {
            "id": "7_7",
            "frame_idx": 707,
            "track": 7,
            "frame": 7,
            "passDirection": "Ascending",
            "granules": granules,
            "gunw_ifgs": [],
        },
    }


@pytest.fixture
def client(tmp_path, page_writer):
    from conftest import FRAMES, META

    frames = {
        "type": "FeatureCollection",
        "features": [*FRAMES["features"], _dup_frame()],
    }
    page = page_writer(tmp_path / "viewer.html", frames=frames, meta=META)
    app = create_app(
        Settings.for_mode(
            "local", cache_dir=tmp_path / "cache", viewer_html=str(page), repo_dir=None
        )
    )
    with TestClient(app, base_url="http://127.0.0.1:8797") as c:
        yield c


def test_frame_duplicates(client):
    r = client.get("/api/v1/frames/7_7/duplicates").json()
    assert r["groups"] == 2 and r["extra_granules"] == 2
    assert r["by_reason"] == {"reprocessed": 1, "split": 1}
    assert [(g["date"], g["reason"]) for g in r["duplicates"]] == [
        ("2026-03-01", "reprocessed"),
        ("2026-03-25", "split"),
    ]


def test_frame_duplicates_respect_the_date_filter(client):
    r = client.get(
        "/api/v1/frames/7_7/duplicates", params={"start": "2026-03-10"}
    ).json()
    assert [g["date"] for g in r["duplicates"]] == ["2026-03-25"]


def test_scan_duplicates(client):
    r = client.get("/api/v1/duplicates").json()
    assert r["frames_judged"] == 3 and r["frames_with_duplicates"] == 1
    assert r["frames"][0]["id"] == "7_7" and r["frames"][0]["dates"] == [
        "2026-03-01",
        "2026-03-25",
    ]
    assert r["by_reason"] == {"reprocessed": 1, "split": 1}


def _call(client, tool, **arguments):
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    res = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
    ).json()["result"]
    return res.get("isError", False), res.get("structuredContent")


def test_mcp_duplicate_tools(client):
    err, frame = _call(client, "frame_duplicates", key="T7_F7")
    assert not err and frame["extra_granules"] == 2
    err, scan = _call(client, "scan_duplicates")
    assert (
        not err
        and scan["frames_with_duplicates"] == 1
        and scan["frames"][0]["id"] == "7_7"
    )
