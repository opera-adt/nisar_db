"""Browse and QA images: fetching, overlays, and the routes and MCP tools on top."""

from __future__ import annotations

import base64
import io
import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("mcp")
PIL = pytest.importorskip("PIL.Image")

from fastapi.testclient import TestClient  # noqa: E402

from nisar_db.api import browse  # noqa: E402
from nisar_db.api.app import create_app  # noqa: E402
from nisar_db.api.settings import Settings  # noqa: E402

GUNW = (
    "NISAR_L2_PR_GUNW_021_013_D_070_022_4000_SH_20260522T024004_20260522T024039"
    "_20260603T024004_20260603T024039_P05023_N_F_J_001"
)
GSLC = (
    "NISAR_L2_PR_GSLC_014_156_D_067_2005_QPDH_A_20260309T004152_20260309T004205"
    "_P05023_N_P_J_001"
)
CORNERS = {
    "epsg": 32611,
    "bbox": [-118.0, 34.0, -117.0, 35.0],
    "quad": [[-118.0, 35.0], [-117.1, 35.1], [-117.0, 34.0], [-117.9, 33.9]],
}


def _png(size=(2000, 1000), mode="RGBA") -> bytes:
    out = io.BytesIO()
    PIL.new(mode, size, (200, 30, 30, 255) if mode == "RGBA" else 128).save(
        out, format="PNG"
    )
    return out.getvalue()


class FakeSession:
    """Answers the public browse download, counting calls."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.urls: list[str] = []

    def get(self, url, timeout):  # noqa: ARG002 - the real signature
        self.urls.append(url)
        resp = type("R", (), {})()
        resp.content, resp.raise_for_status = self.body, lambda: None
        return resp


class FakeCache:
    def __init__(self, root) -> None:
        self.root, self.layer_calls = root, 0

    def layers(self, gid):
        self.layer_calls += 1
        (self.root / gid).mkdir(parents=True, exist_ok=True)
        for name in ("coherence", "cc"):
            (self.root / gid / f"{name}.png").write_bytes(_png((800, 800), "L"))
        return ["coherence", "cc"]

    def corners(self, gid):  # noqa: ARG002 - the real signature
        return CORNERS


class FakeHelper:
    def __init__(self, root) -> None:
        self.cache = FakeCache(root)


@pytest.fixture(autouse=True)
def _fresh_cache():
    browse._CACHE.clear()
    yield
    browse._CACHE.clear()


def _size(jpeg: bytes) -> tuple[int, int]:
    return PIL.open(io.BytesIO(jpeg)).size


def test_public_browse_is_shrunk_to_jpeg_and_cached():
    session = FakeSession(_png())
    data, meta = browse.fetch_image(GSLC, "browse", max_side=500, session=session)
    assert data[:2] == b"\xff\xd8" and _size(data) == (500, 250)
    assert meta["original_size"] == [2000, 1000] and meta[
        "source"
    ] == browse.browse_url(GSLC)
    browse.fetch_image(GSLC, "browse", max_side=500, session=session)
    assert len(session.urls) == 1


def test_qa_layer_comes_from_the_helper(tmp_path):
    data, meta = browse.fetch_image(
        GUNW, "coherence", helper=FakeHelper(tmp_path), max_side=300
    )
    assert (
        _size(data) == (300, 300)
        and meta["source"] == "QA report"
        and meta["label"] == "Coherence"
    )


@pytest.mark.parametrize(
    ("gid", "layer", "helper", "error", "message"),
    [
        ("not_a_granule", "browse", False, ValueError, "not a NISAR"),
        (GUNW, "sparkle", False, ValueError, "unknown layer"),
        (GSLC, "coherence", True, ValueError, "QA layers exist for GUNW pairs only"),
        (GUNW, "coherence", False, ValueError, "need the viewer helper"),
        (GUNW, "iono", True, LookupError, "has no 'iono' layer"),
    ],
)
def test_fetch_errors(tmp_path, gid, layer, helper, error, message):
    with pytest.raises(error, match=message):
        browse.fetch_image(gid, layer, helper=FakeHelper(tmp_path) if helper else None)


def test_overlay_without_corners_still_links_the_viewer():
    out = browse.browse_overlay(
        GUNW, "coherence", dataset="globe-20261009T193409", base="http://h:1/"
    )
    assert out["viewer_url"].startswith(
        "http://h:1/view/globe-20261009T193409?browse=NISAR_L2_PR_GUNW_"
    )
    assert (
        "browse_map=1" in out["viewer_url"]
        and "browse_layer=coherence" in out["viewer_url"]
    )
    assert out["image_url"] == f"http://h:1/api/v1/browse/{GUNW}?layer=coherence"
    assert (
        out["coordinates"] is None and "corners need the viewer helper" in out["note"]
    )


def test_overlay_places_browse_by_bbox_and_qa_layers_by_quad(tmp_path):
    helper = FakeHelper(tmp_path)
    assert browse.browse_overlay(GUNW, "browse", base="b", helper=helper)[
        "coordinates"
    ] == [[-118.0, 35.0], [-117.0, 35.0], [-117.0, 34.0], [-118.0, 34.0]]
    assert (
        browse.browse_overlay(GUNW, "cc", base="b", helper=helper)["coordinates"]
        == CORNERS["quad"]
    )


# -- routes and MCP tools ------------------------------------------------------------


def _drop_frame() -> dict:
    cms = [0.6, 0.62, 0.58, 0.61, 0.1, 0.12, 0.6, 0.59, 0.61]
    pairs = [
        {
            "ref": f"2026-03-{1 + i:02d}",
            "sec": f"2026-03-{2 + i:02d}",
            "dt": 12,
            "gid": GUNW.replace("_001", f"_{i:03d}"),
            "qa": {"cm": v},
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
def client(tmp_path, page_writer, monkeypatch):
    from conftest import FRAMES, META

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
    # The REST routes read the helper per request; the MCP tools took the
    # app's helper at start-up (none: repo_dir=None), so they fall back to browse.
    helper = FakeHelper(tmp_path / "qa")
    app.state.helper = helper
    session = FakeSession(_png())
    monkeypatch.setattr(browse.requests, "get", session.get)
    with TestClient(app, base_url="http://127.0.0.1:8797") as c:
        c.helper, c.session = helper, session
        yield c


def test_browse_route_returns_a_jpeg(client):
    r = client.get(f"/api/v1/browse/{GSLC}", params={"max_side": 256})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert _size(r.content) == (256, 128) and r.headers["x-image-layer"] == "browse"


@pytest.mark.parametrize(
    ("gid", "layer", "status"), [("bad", "browse", 400), (GUNW, "iono", 404)]
)
def test_browse_route_errors(client, gid, layer, status):
    assert (
        client.get(f"/api/v1/browse/{gid}", params={"layer": layer}).status_code
        == status
    )


def test_overlay_route(client):
    r = client.get(f"/api/v1/browse/{GUNW}/overlay", params={"layer": "cc"}).json()
    assert r["coordinates"] == CORNERS["quad"] and r["viewer_url"].startswith(
        "http://127.0.0.1:8797/?browse="
    )


def test_qa_drop_images_route(client):
    r = client.get(
        "/api/v1/frames/7_7/qa-drops/images", params={"metric": "cm", "n": 1}
    ).json()
    assert r["dates"] == ["2026-03-06"]
    assert [p["role"] for p in r["picks"]] == ["flagged", "typical"]
    assert r["picks"][0]["layer"] == "coherence" and r["picks"][0][
        "image_url"
    ].endswith("?layer=coherence")


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


def test_mcp_browse_image_returns_text_and_an_image(client):
    res = _call(client, "browse_image", gid=GSLC, max_side=200)
    kinds = [c["type"] for c in res["content"]]
    assert not res.get("isError") and kinds == ["text", "image"]
    assert json.loads(res["content"][0]["text"])["layer"] == "browse"
    assert _size(base64.b64decode(res["content"][1]["data"])) == (200, 100)


def test_mcp_qa_drop_images_falls_back_to_browse_without_the_helper(client):
    res = _call(client, "qa_drop_images", key="7_7", metric="cm", n=1, max_side=200)
    kinds = [c["type"] for c in res["content"]]
    assert not res.get("isError") and kinds == [
        "text",
        "text",
        "image",
        "text",
        "image",
    ]
    flagged = json.loads(res["content"][1]["text"])
    assert (
        flagged["role"] == "flagged"
        and flagged["layer"] == "browse"
        and "unavailable" in flagged["note"]
    )


def test_mcp_browse_on_map_links_the_viewer(client):
    res = _call(client, "browse_on_map", gid=GUNW)
    assert res["structuredContent"]["viewer_url"].startswith(
        "http://127.0.0.1:8797/?browse="
    )
