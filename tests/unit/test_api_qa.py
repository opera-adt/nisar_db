"""QA drops over HTTP (/api/v1) and over MCP (/mcp)."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("mcp")

from fastapi.testclient import TestClient

from nisar_db.api.app import create_app
from nisar_db.api.settings import Settings


def _drop_frame() -> dict:
    """Frame 7_7 with 9 chained 12-day pairs; the two touching 2026-03-06 drop."""
    cms = [0.6, 0.62, 0.58, 0.61, 0.1, 0.12, 0.6, 0.59, 0.61]
    pairs = [
        {
            "ref": f"2026-03-{1 + i:02d}",
            "sec": f"2026-03-{2 + i:02d}",
            "dt": 12,
            "mode": "4000",
            "pol": "SH",
            "gid": f"NISAR_L2_PR_GUNW_{i:03d}",
            "qa": {"cm": v, "n": 1},
        }
        for i, v in enumerate(cms)
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
            "granules": [],
            "gunw_ifgs": pairs,
        },
    }


@pytest.fixture
def client(tmp_path, page_writer):
    from conftest import FRAMES, META  # the shared two-frame page

    frames = {
        "type": "FeatureCollection",
        "features": [*FRAMES["features"], _drop_frame()],
    }
    page = page_writer(tmp_path / "viewer.html", frames=frames, meta=META)
    app = create_app(
        Settings.for_mode(
            "local", cache_dir=tmp_path / "cache", viewer_html=str(page), repo_dir=None
        )
    )
    with TestClient(app, base_url="http://127.0.0.1:8797") as c:
        yield c


def test_qa_metrics_list(client):
    metrics = {m["metric"]: m for m in client.get("/api/v1/qa-metrics").json()}
    assert metrics["cm"]["bad_when"] == "low" and metrics["n"]["bad_when"] == "high"
    assert metrics["rl"]["products"] == ["gunw", "gslc"] and metrics["rl"]["log"]


def test_frame_qa_drops(client):
    r = client.get("/api/v1/frames/7_7/qa-drops", params={"metric": "cm"}).json()
    assert r["id"] == "7_7" and r["median"] == 0.6
    assert [f["gid"][-3:] for f in r["flagged"]] == ["004", "005"]
    assert [d["date"] for d in r["dates"]] == ["2026-03-06"]


def test_frame_qa_drops_respects_the_date_filter(client):
    r = client.get(
        "/api/v1/frames/7_7/qa-drops", params={"metric": "cm", "end": "2026-03-04"}
    ).json()
    assert r["status"].startswith("too few values")


@pytest.mark.parametrize(
    ("params", "detail"),
    [
        ({"metric": "zz"}, "unknown metric"),
        ({"metric": "cm", "product": "gslc"}, "not a GSLC metric"),
    ],
)
def test_bad_qa_options(client, params, detail):
    r = client.get("/api/v1/qa-drops", params=params)
    assert r.status_code == 400 and detail in r.json()["detail"]


def test_scan_finds_the_frame_and_its_date(client):
    r = client.get("/api/v1/qa-drops", params={"metric": "cm"}).json()
    assert r["frames_judged"] == 1 and r["frames_flagged"] == 1
    assert r["frames"][0]["id"] == "7_7" and r["frames"][0]["dates"] == ["2026-03-06"]
    assert r["dates"] == [{"date": "2026-03-06", "n_frames": 1, "frames": ["7_7"]}]


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
    if res.get("isError"):
        return True, res["content"][0]["text"]
    data = res["structuredContent"]
    return False, data["result"] if set(data) == {"result"} else data


def test_mcp_frame_and_scan_tools(client):
    err, report = _call(client, "frame_qa_drops", key="T7_F7", metric="cm")
    assert not err and [d["date"] for d in report["dates"]] == ["2026-03-06"]
    err, scan = _call(client, "scan_qa_drops", metric="cm")
    assert not err and scan["dates"][0]["date"] == "2026-03-06"
    err, metrics = _call(client, "list_qa_metrics")
    assert {m["metric"] for m in metrics} >= {"cm", "v", "n", "is", "rl"}


def test_mcp_reports_a_bad_metric(client):
    err, text = _call(client, "frame_qa_drops", key="7_7", metric="zz")
    assert err and "unknown metric 'zz'" in text
