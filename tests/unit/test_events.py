"""Earthquakes and volcanoes, the frames that see them, and the routes / MCP tools."""

from __future__ import annotations

import json

import pytest

from nisar_db import events

QUAKE = {
    "type": "Feature",
    "id": "us7000test",
    "properties": {
        "mag": 7.3,
        "magType": "mww",
        "place": "near the frame",
        "time": 1782571719000,
        "url": "https://earthquake.usgs.gov/x",
        "tsunami": 1,
        "alert": "yellow",
    },
    "geometry": {"type": "Point", "coordinates": [-119.5, 35.5, 12.0]},
}


class FakeUsgs:
    """Answers the USGS event service: a search or one event, recording params."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.params: list[dict] = []

    def get(self, url, params, timeout):  # noqa: ARG002 - the real signature
        self.params.append(params)
        body = (
            QUAKE
            if "eventid" in params
            else {"type": "FeatureCollection", "features": [QUAKE]}
        )
        resp = type("R", (), {})()
        resp.status_code, resp.json = self.status, lambda: body
        resp.raise_for_status = lambda: None
        return resp


def _pair(ref_t: str, sec_t: str, dt: int) -> dict:
    gid = (
        f"NISAR_L2_PR_GUNW_021_013_D_070_022_4000_SH_{ref_t}_{ref_t}_{sec_t}_{sec_t}"
        "_P05023_N_F_J_001"
    )
    return {
        "gid": gid,
        "ref": f"{ref_t[:4]}-{ref_t[4:6]}-{ref_t[6:8]}",
        "sec": f"{sec_t[:4]}-{sec_t[4:6]}-{sec_t[6:8]}",
        "dt": dt,
        "qa": {"cm": 0.2},
    }


def test_search_earthquakes_builds_the_query_and_reads_events():
    usgs = FakeUsgs()
    (q,) = events.search_earthquakes(
        bbox=(-125, 32, -114, 42),
        start="2026-06-01",
        min_magnitude=6,
        order="magnitude",
        limit=5,
        session=usgs,
    )
    assert usgs.params[0] == {
        "format": "geojson",
        "minmagnitude": 6,
        "limit": 5,
        "orderby": "magnitude",
        "starttime": "2026-06-01",
        "minlongitude": -125,
        "minlatitude": 32,
        "maxlongitude": -114,
        "maxlatitude": 42,
    }
    assert q["id"] == "us7000test" and q["mag"] == 7.3 and q["depth_km"] == 12.0
    assert q["time"] == "2026-06-27T14:48:39Z" and q["tsunami"] is True


def test_search_by_circle():
    usgs = FakeUsgs()
    events.search_earthquakes(center=(-119.5, 35.5), radius_km=50, session=usgs)
    assert usgs.params[0]["maxradiuskm"] == 50 and usgs.params[0]["longitude"] == -119.5


def test_unknown_event_raises_lookup():
    with pytest.raises(LookupError, match="no event 'nope'"):
        events.get_earthquake("nope", session=FakeUsgs(status=404))


def test_coseismic_pairs_compare_acquisition_times():
    pairs = [
        _pair("20260620T020000", "20260702T020000", 12),  # spans the event
        _pair("20260608T020000", "20260702T020000", 24),  # spans it, longer
        _pair(
            "20260627T200000", "20260709T200000", 12
        ),  # starts after it (same day, later)
        _pair("20260515T020000", "20260527T020000", 12),
    ]  # before it
    found = events.coseismic_pairs(pairs, "2026-06-27T14:48:39Z")
    assert [(p["ref"], p["dt"]) for p in found] == [
        ("2026-06-20", 12),
        ("2026-06-08", 24),
    ]
    assert [
        p["dt"]
        for p in events.coseismic_pairs(pairs, "2026-06-27T14:48:39Z", max_dt=12)
    ] == [12]


def test_acquisitions_around_the_event():
    grans = [
        {"date": d}
        for d in ("2026-06-03", "2026-06-15", "2026-06-27", "2026-07-09", "2026-07-21")
    ]
    assert events.acquisitions_around(grans, "2026-06-27T14:00:00Z") == {
        "before": ["2026-06-03", "2026-06-15"],
        "after": ["2026-06-27", "2026-07-09"],
    }


def _frame(fid: str, lon: float, lat: float) -> dict:
    ring = [[lon, lat], [lon + 1, lat], [lon + 1, lat + 1], [lon, lat + 1], [lon, lat]]
    return {
        "geometry": {"type": "Polygon", "coordinates": [ring]},
        "properties": {"id": fid},
    }


def test_frames_at_a_point_or_within_a_radius():
    frames = [
        _frame("in", -120, 35),
        _frame("near", -118.95, 35),
        _frame("far", -100, 10),
    ]
    ids = lambda fs: [f["properties"]["id"] for f in fs]  # noqa: E731
    assert ids(events.frames_at(frames, -119.5, 35.5)) == ["in"]
    assert ids(events.frames_at(frames, -119.5, 35.5, radius_km=50)) == ["in", "near"]


def test_volcano_search():
    vs = events.volcano_list(
        [
            {
                "geometry": {"coordinates": [-153.43, 59.36]},
                "properties": {
                    "v": 313010,
                    "n": "Augustine",
                    "t": "Lava dome",
                    "y": 2006,
                    "e": 1252,
                    "c": "United States",
                    "r": "Alaska",
                    "ev": "x",
                },
            }
        ]
    )
    assert vs[0]["vnum"] == 313010 and vs[0]["lon"] == -153.43
    assert events.find_volcanoes(vs, name="AUGUST") == vs
    assert events.find_volcanoes(vs, erupted_since=2010) == []
    assert events.find_volcanoes(vs, bbox=(-160, 55, -150, 62)) == vs
    assert events.find_volcanoes(vs, country="Japan") == []


# -- routes and MCP tools --------------------------------------------------------------


@pytest.fixture
def client(tmp_path, page_writer, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("mcp")
    from conftest import META
    from fastapi.testclient import TestClient

    from nisar_db.api.app import create_app
    from nisar_db.api.settings import Settings

    frame = _frame("10_10", -120, 35)
    frame["type"] = "Feature"
    frame["properties"].update(
        frame_idx=1010,
        track=10,
        frame=10,
        passDirection="Ascending",
        granules=[{"date": "2026-06-20"}, {"date": "2026-07-02"}],
        gunw_ifgs=[_pair("20260620T020000", "20260702T020000", 12)],
    )
    page = page_writer(
        tmp_path / "viewer.html",
        frames={"type": "FeatureCollection", "features": [frame]},
        meta=META,
    )
    # The page also embeds the volcano list.
    page.write_text(
        page.read_text().replace(
            "</script>",
            "const VOLCANO_DATA = "
            + json.dumps(
                {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "geometry": {
                                "type": "Point",
                                "coordinates": [-119.5, 35.5],
                            },
                            "properties": {
                                "v": 1,
                                "n": "Test Dome",
                                "t": "Lava dome",
                                "y": 2020,
                                "e": 100,
                                "c": "US",
                                "r": "CA",
                                "ev": "x",
                            },
                        }
                    ],
                }
            )
            + ";\n</script>",
            1,
        )
    )
    monkeypatch.setattr(events.requests, "get", FakeUsgs().get)
    app = create_app(
        Settings.for_mode(
            "local", cache_dir=tmp_path / "c", viewer_html=str(page), repo_dir=None
        )
    )
    with TestClient(app, base_url="http://127.0.0.1:8797") as c:
        yield c


def test_earthquake_routes(client):
    (q,) = client.get("/api/v1/events/earthquakes", params={"min_magnitude": 6}).json()
    assert q["id"] == "us7000test"
    r = client.get("/api/v1/events/earthquakes/us7000test/frames").json()
    assert r["status"] == "coseismic pairs found"
    (f,) = r["frames"]
    assert f["id"] == "10_10" and f["n_coseismic"] == 1
    pair = f["coseismic_pairs"][0]
    assert pair["coherence_median"] == 0.2 and pair["browse_url"].startswith(
        "http://127.0.0.1:8797/api/v1/browse/"
    )
    assert "browse_map=1" in pair["viewer_url"] and r["viewer_url"].endswith("&zoom=7")


def test_volcano_routes(client):
    (v,) = client.get("/api/v1/events/volcanoes", params={"name": "dome"}).json()
    r = client.get(f"/api/v1/events/volcanoes/{v['vnum']}/frames").json()
    assert r["volcano"]["name"] == "Test Dome" and r["frames"][0]["n_pairs"] == 1
    assert client.get("/api/v1/events/volcanoes/999/frames").status_code == 404


def _call(client, tool, **arguments):
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    return client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
    ).json()["result"]


def test_event_mcp_tools(client):
    assert (
        _call(client, "find_earthquakes", min_magnitude=6)["structuredContent"][
            "result"
        ][0]["id"]
        == "us7000test"
    )
    # Text + optional image tools return their JSON as text content.
    res = _call(client, "earthquake_frames", event_id="us7000test")
    quake = res.get("structuredContent") or json.loads(res["content"][0]["text"])
    assert quake["frames"][0]["n_coseismic"] == 1
    assert (
        _call(client, "find_volcanoes", name="test")["structuredContent"]["result"][0][
            "vnum"
        ]
        == 1
    )
    assert (
        _call(client, "volcano_frames", vnum=1)["structuredContent"]["frames"][0]["id"]
        == "10_10"
    )
