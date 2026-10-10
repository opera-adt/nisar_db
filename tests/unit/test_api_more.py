"""Areas, exports, DISP-NISAR assets, and the frame-health routes / MCP tools."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nisar_db import aoi, disp_assets, exports


def _square(lon, lat, size=1.0):
    ring = [
        [lon, lat],
        [lon + size, lat],
        [lon + size, lat + size],
        [lon, lat + size],
        [lon, lat],
    ]
    return {"type": "Polygon", "coordinates": [ring]}


def test_aoi_shares_and_total_coverage():
    frames = [
        {"geometry": _square(0, 0, 2), "properties": {"id": "a"}},
        {"geometry": _square(1, 0, 2), "properties": {"id": "b"}},
        {"geometry": _square(10, 10), "properties": {"id": "far"}},
    ]
    r = aoi.aoi_frames(frames, geometry=_square(0.5, 0.5, 1))
    assert [f["id"] for f in r["frames"]] == ["a", "b"]
    assert r["frames"][0]["aoi_share"] == pytest.approx(1.0, abs=1e-3)
    assert r["frames"][1]["aoi_share"] == pytest.approx(0.5, abs=1e-3)
    assert r["covered_share"] == pytest.approx(1.0, abs=1e-3)
    assert [
        f["id"]
        for f in aoi.aoi_frames(frames, geometry=_square(0.5, 0.5, 1), min_share=0.6)[
            "frames"
        ]
    ] == ["a"]


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({}, "give a GeoJSON geometry or a box"),
        ({"box": (1, 1, 1, 1)}, "the area is empty"),
    ],
)
def test_aoi_needs_an_area(kw, message):
    with pytest.raises(ValueError, match=message):
        aoi.aoi_frames([], **kw)


def test_exports():
    feats = [
        {
            "geometry": _square(0, 0),
            "properties": {"id": "34_19", "track": 34, "rollout": ["P1"]},
        }
    ]
    assert (
        exports.to_csv([f["properties"] for f in feats])
        == 'id,track,rollout\n34_19,34,"[""P1""]"\n'
    )
    gj = json.loads(exports.to_geojson(feats, ["id"]))
    assert gj["features"][0]["properties"] == {"id": "34_19"}
    kml = exports.to_kml(feats, properties=["id", "track"])
    assert (
        "<name>34_19</name>" in kml
        and '<Data name="track"><value>34</value></Data>' in kml
    )
    multi = [
        {
            "geometry": {
                "type": "MultiPolygon",
                "coordinates": [
                    _square(0, 0)["coordinates"],
                    _square(5, 5)["coordinates"],
                ],
            },
            "properties": {"id": "m"},
        }
    ]
    assert "<MultiGeometry>" in exports.to_kml(multi)
    with pytest.raises(ValueError, match="KML export takes polygons"):
        exports.to_kml(
            [{"geometry": {"type": "Point", "coordinates": [0, 0]}, "properties": {}}]
        )


def test_build_disp_assets_runs_the_release_steps(tmp_path):
    """The same commands and file names as .github/workflows/release.yml."""
    calls = []

    def run(argv, cwd):
        calls.append(argv)
        out = Path(cwd)
        arg = lambda flag: argv[argv.index(flag) + 1]  # noqa: E731
        if argv[0] == "search":
            (out / "gslc_search.csv").write_text("url\ns3://b/a.h5\nhttps://x/b.h5\n")
            return
        writes = {
            "download-frame-db": lambda: ["NISAR_TrackFrame_L_20250909.gpkg"],
            "create-frame-to-bound": lambda: [
                arg("--output"),
                arg("--geojson"),
                "opera-nisar-disp-frames.gpkg",
            ],
            "create-gslc-csv": lambda: ["gslc_catalog.csv"],
        }
        for name in writes.get(argv[0], lambda: [arg("--output")])():
            (out / name).write_text("{}")

    blackout = tmp_path / "blackout.json"
    blackout.write_text("{}")
    names = disp_assets.build_disp_assets(
        tmp_path / "out",
        blackout_file=blackout,
        version="0.2.0",
        day="2026-10-10",
        run=run,
    )
    assert [c[0] for c in calls] == [
        "download-frame-db",
        "create-frame-to-bound",
        "search",
        "create-gslc-csv",
        "create-consistent",
        "create-consistent",
        "label-processing-mode",
        "create-reference-dates",
    ]
    assert calls[2] == [
        "search",
        "--product-type",
        "GSLC",
        "--url-type",
        "s3",
        "--max-results",
        "0",
        "--output-csv",
        "gslc_search.csv",
    ]
    assert (tmp_path / "out" / "gslc_files.txt").read_text() == "s3://b/a.h5\n"
    assert (
        names["consistent_gslc"] == "opera-nisar-disp-consistent-gslc-2026-10-10.json"
    )
    assert (
        names["frame_geometries"]
        == "opera-nisar-disp-frame-geometries-simple-0.2.0.geojson"
    )
    assert "--blackout-file" in calls[5] and "--blackout-file" not in calls[4]


def test_build_disp_assets_reports_a_missing_output(tmp_path):
    with pytest.raises(FileNotFoundError, match="NISAR_TrackFrame_L_"):
        disp_assets.build_disp_assets(tmp_path, run=lambda *_: None)


# -- assets ----------------------------------------------------------------------------


class FakeGitHub:
    def __init__(self, ok=True) -> None:
        self.ok = ok

    def get(self, url, timeout, headers):  # noqa: ARG002 - the real signature
        import requests

        if not self.ok:
            raise requests.ConnectionError("down")
        resp = type("R", (), {})()
        resp.raise_for_status = lambda: None
        resp.json = lambda: {
            "tag_name": "v0.1.0",
            "html_url": "https://g/r",
            "assets": [
                {
                    "name": "opera-nisar-disp-blackout-dates-2026-10-03.json",
                    "size": 10,
                    "browser_download_url": "https://g/b.json",
                }
            ],
        }
        return resp


def test_published_assets_and_a_github_outage():
    from nisar_db.api import assets

    pub = assets.published(FakeGitHub())
    assert pub["tag"] == "v0.1.0" and pub["assets"][0]["kind"] == "blackout_dates"
    assert assets.published(FakeGitHub(ok=False))["error"].startswith(
        "GitHub did not answer"
    )


def test_frame_entry_reads_json_and_zip(tmp_path):
    import zipfile

    from nisar_db.api import assets

    doc = {"metadata": {}, "data": {"5826": {"common_mode": "4005"}}}
    (tmp_path / "c.json").write_text(json.dumps(doc))
    with zipfile.ZipFile(tmp_path / "c.json.zip", "w") as z:
        z.writestr("c.json", json.dumps(doc))
    for name in ("c.json", "c.json.zip"):
        assert assets.frame_entry(assets.load_json(tmp_path / name), 5826) == {
            "common_mode": "4005"
        }


# -- routes and MCP tools --------------------------------------------------------------


@pytest.fixture
def client(tmp_path, viewer_page, make_launcher, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("mcp")
    from fastapi.testclient import TestClient

    from nisar_db.api import assets
    from nisar_db.api.app import create_app
    from nisar_db.api.settings import Settings

    repo = tmp_path / "repo"
    (repo / "catalog").mkdir(parents=True)
    (repo / "catalog" / "opera-nisar-disp-blackout-dates.json").write_text(
        json.dumps(
            {
                "metadata": {},
                "blackout_dates": {
                    "5826": [["2026-01-01T00:00:00", "2026-02-28T23:59:59"]]
                },
            }
        )
    )
    monkeypatch.setattr(
        assets, "published", lambda *_, **__: {"tag": "v0.1.0", "assets": []}
    )
    app = create_app(
        Settings.for_mode(
            "local",
            cache_dir=tmp_path / "c",
            viewer_html=str(viewer_page),
            repo_dir=None,
        ),
        launcher=make_launcher(
            outputs=("opera-nisar-disp-consistent-gslc-2026-10-10.json",)
        ),
    )
    app.state.settings.repo_dir = repo
    with TestClient(app, base_url="http://127.0.0.1:8797") as c:
        yield c


def test_frame_health_routes(client):
    cov = client.get(
        "/api/v1/frames/34_19/coverage", params={"today": "2026-07-01"}
    ).json()
    assert cov["n_acquisitions"] == 2 and cov["days_since_last"] == 15
    assert client.get("/api/v1/frames/34_19/network").json()["status"] == "connected"
    nxt = client.get(
        "/api/v1/frames/34_19/next-passes", params={"today": "2026-07-01", "n": 2}
    ).json()
    assert nxt["next"] == ["2026-07-10", "2026-07-22"]
    assert (
        client.get(
            "/api/v1/next-passes",
            params={"lon": -119.5, "lat": 35.5, "today": "2026-07-01"},
        ).json()["frames"][0]["id"]
        == "34_19"
    )
    ready = client.get("/api/v1/frames/34_19/disp-readiness").json()
    assert ready["status"] == "accumulating" and ready["n_usable"] == 1
    assert client.get("/api/v1/disp-readiness").json()["status"] == {
        "accumulating": 1,
        "no consistent mode": 1,
    }
    assert client.get("/api/v1/network").json()["status"] == {
        "connected": 1,
        "no GUNW": 1,
    }
    assert (
        client.get("/api/v1/coverage", params={"today": "2026-12-01"}).json()[
            "frames_flagged"
        ]
        == 1
    )
    assert client.get("/api/v1/frames/99_99/coverage").status_code == 404


def test_area_routes(client):
    r = client.get("/api/v1/aoi", params={"bbox": "-120,35,-119.5,35.5"}).json()
    assert r["frames"][0]["id"] == "34_19" and r["covered_share"] == pytest.approx(
        1.0, abs=1e-3
    )
    poly = client.post("/api/v1/aoi", json=_square(149.5, -40, 1)).json()
    assert poly["frames"][0]["id"] == "1_61"
    assert (
        client.post(
            "/api/v1/aoi",
            json={"type": "Polygon", "coordinates": [[[0, 0], [0, 0], [0, 0], [0, 0]]]},
        ).status_code
        == 400
    )


def test_event_qa_route(client):
    r = client.get(
        "/api/v1/frames/34_19/event-qa", params={"when": "2026-06-10", "metric": "cm"}
    ).json()
    assert r["n"] == {"before": 0, "spanning": 1, "after": 0}


def test_export_routes(client):
    csv_text = client.get("/api/v1/export/frames").text
    assert csv_text.splitlines()[0].startswith("id,frame_idx,track")
    kml = client.get("/api/v1/export/frames", params={"format": "kml"})
    assert kml.headers["content-type"].startswith(
        "application/vnd.google-earth.kml+xml"
    )
    pairs = client.get(
        "/api/v1/export/entries", params={"product": "gunw"}
    ).text.splitlines()
    assert pairs[0].startswith("frame_id,frame_idx,ref,sec") and len(pairs) == 2


def test_disp_asset_routes(client, waiter):
    assert (
        client.get("/api/v1/disp/assets").json()["repo"][0]["kind"] == "blackout_dates"
    )
    bo = client.get("/api/v1/disp/blackout-dates/T34_F19").json()
    assert (
        bo["frame_idx"] == 5826 and bo["blackout_dates"][0][0] == "2026-01-01T00:00:00"
    )
    assert client.get("/api/v1/disp/consistent/34_19").status_code == 404
    assert client.get("/api/v1/disp/assets/blackout_dates").status_code == 200
    job = client.post("/api/v1/disp/build", json={"max_results": 5}).json()
    assert job["kind"] == "build-disp-assets" and "--blackout-file" in job["argv"]
    assert waiter(
        lambda: client.get(f"/api/v1/jobs/{job['id']}").json()["state"] == "done"
    )
    built = client.get("/api/v1/disp/assets").json()["built"]
    assert built["job"] == job["id"] and built["assets"][0]["kind"] == "consistent_gslc"


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
    return (
        res.get("isError", False),
        res.get("structuredContent") or res["content"][0]["text"],
    )


def test_mcp_frame_health_and_exports(client):
    err, cov = _call(client, "frame_coverage", key="34_19", today="2026-07-01")
    assert not err and cov["days_since_last"] == 15
    err, nxt = _call(client, "next_passes", lon=-119.5, lat=35.5, today="2026-07-01")
    assert not err and nxt["frames"][0]["id"] == "34_19"
    err, area = _call(client, "frames_in_area", bbox=[-120, 35, -119.5, 35.5])
    assert not err and area["frames"][0]["id"] == "34_19"
    err, out = _call(client, "export_frames", format="geojson")
    assert not err and json.loads(out["text"])["type"] == "FeatureCollection"
    assert out["download_url"].startswith("http://127.0.0.1:8797/api/v1/export/frames?")
    err, text = _call(client, "next_passes")
    assert err and "give a frame key, or lon and lat" in text


def test_mcp_disp_assets(client):
    err, bo = _call(client, "disp_blackout_dates", key="5826")
    assert not err and bo["asset"] == "opera-nisar-disp-blackout-dates.json"
    err, listing = _call(client, "list_disp_assets")
    assert not err and listing["published"]["tag"] == "v0.1.0"
    err, text = _call(client, "disp_consistent", key="5826")
    assert err and "no local consistent_gslc asset" in text
