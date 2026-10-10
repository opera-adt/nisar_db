"""The MCP server: its tools over HTTP (/mcp) and over stdio (`nisar-db mcp`)."""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("mcp")

from fastapi.testclient import TestClient

from nisar_db.api.app import create_app
from nisar_db.api.settings import Settings, load_keys

HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
INIT = {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {"name": "test", "version": "0"},
}


class Rpc:
    """A JSON-RPC caller for /mcp."""

    def __init__(self, client: TestClient, key: str | None = None) -> None:
        self.client, self.key, self.n = client, key, 0

    def post(self, method: str, params: dict | None = None):
        self.n += 1
        headers = {**HEADERS, **({"X-API-Key": self.key} if self.key else {})}
        return self.client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": self.n,
                "method": method,
                "params": params or {},
            },
        )

    def call(self, tool: str, **arguments) -> tuple[bool, object]:
        """Call a tool; return (is_error, parsed JSON or the error text)."""
        result = self.post("tools/call", {"name": tool, "arguments": arguments}).json()[
            "result"
        ]
        if result.get("isError"):
            return True, result["content"][0]["text"]
        # The whole result is in structuredContent; a list is wrapped
        # as {"result": [...]}.
        data = result["structuredContent"]
        return False, data["result"] if set(data) == {"result"} else data


def _app(tmp_path, viewer_page, mode="local", **kw):
    if mode == "shared":
        kw.setdefault(
            "keys", load_keys(None, "reader:r:read,worker:w:read+jobs,other:o:jobs")
        )
    settings = Settings.for_mode(
        mode,
        cache_dir=tmp_path / "cache",
        viewer_html=str(viewer_page),
        repo_dir=None,
        **kw,
    )
    return create_app(settings, launcher=kw.pop("launcher", None))


@pytest.fixture
def local_rpc(tmp_path, viewer_page, make_launcher):
    app = create_app(
        Settings.for_mode(
            "local",
            cache_dir=tmp_path / "cache",
            viewer_html=str(viewer_page),
            repo_dir=None,
        ),
        launcher=make_launcher(outputs=("results.csv",)),
    )
    with TestClient(app, base_url="http://127.0.0.1:8797") as client:
        rpc = Rpc(client)
        assert rpc.post("initialize", INIT).status_code == 200
        yield rpc


def test_tools_are_listed(local_rpc):
    names = {t["name"] for t in local_rpc.post("tools/list").json()["result"]["tools"]}
    assert {
        "find_frames",
        "frame_granules",
        "list_cycles",
        "viewer_link",
        "start_job",
        "read_job_output",
    } <= names


def test_catalog_tools(local_rpc):
    err, datasets = local_rpc.call("list_datasets")
    assert not err and datasets[0]["id"] == "published" and datasets[0]["n_gunw"] == 1
    err, found = local_rpc.call("find_frames", cycle="22", limit=5)
    assert not err and found["total"] == 1 and found["frames"][0]["n_selected"] == 1
    err, pairs = local_rpc.call("frame_granules", key="T34_F19", product="gunw")
    assert not err and pairs["total"] == 1
    err, cycles = local_rpc.call("list_cycles")
    assert [c["cycle"] for c in cycles] == [22, 23]
    err, bo = local_rpc.call("frame_blackout", key="34_19")
    assert bo["month_share"]["Jan"] == 1.0
    err, summary = local_rpc.call("summarize", direction="D")
    assert summary["consistent"]["n_frames"] == 1


@pytest.mark.parametrize(
    ("tool", "arguments", "message"),
    [
        ("get_frame", {"key": "99_99"}, "no frame '99_99'"),
        ("find_frames", {"dataset": "nope"}, "not found: 'nope'"),
        ("find_frames", {"bbox": [1, 2, 3]}, "bbox is [west, south, east, north]"),
        (
            "viewer_link",
            {"params": {"colour": "x"}},
            "unknown viewer parameters ['colour']",
        ),
        ("start_job", {"kind": "rm-rf"}, "no job kind 'rm-rf'"),
    ],
)
def test_expected_errors_reach_the_client(local_rpc, tool, arguments, message):
    err, text = local_rpc.call(tool, **arguments)
    assert err and message in text


def test_viewer_link(local_rpc):
    _err, link = local_rpc.call(
        "viewer_link",
        dataset="globe-20261009T193409",
        params={"product": "gunw", "sky": "space", "opera": False},
    )
    assert (
        link["url"]
        == "http://127.0.0.1:8797/view/globe-20261009T193409?product=gunw&sky=space&opera=0"
    )


def test_job_round_trip(local_rpc, waiter):
    err, job = local_rpc.call("start_job", kind="search", params={"track": 34})
    assert not err and job["argv"][0] == "search"
    assert waiter(
        lambda: local_rpc.call("job_status", job_id=job["id"])[1]["state"] == "done"
    )
    err, out = local_rpc.call("read_job_output", job_id=job["id"], name="results.csv")
    assert out == {
        "name": "results.csv",
        "size": 4,
        "truncated": False,
        "text": "out\n",
    }
    err, listed = local_rpc.call("job_status")
    assert [j["id"] for j in listed["jobs"]] == [job["id"]]


@pytest.fixture
def shared(tmp_path, viewer_page, make_launcher):
    app = create_app(
        Settings.for_mode(
            "shared",
            cache_dir=tmp_path / "cache",
            viewer_html=str(viewer_page),
            repo_dir=None,
            keys=load_keys(None, "reader:r:read,worker:w:read+jobs,other:o:jobs"),
        ),
        launcher=make_launcher(),
    )
    with TestClient(app) as client:
        yield client


def test_shared_mcp_needs_a_key(shared):
    assert Rpc(shared).post("tools/list").status_code == 401
    assert Rpc(shared, "wrong").post("tools/list").status_code == 401
    assert Rpc(shared, "r").post("tools/list").status_code == 200


def test_shared_job_tools_check_the_keys_scope(shared):
    err, text = Rpc(shared, "r").call("start_job", kind="create-blackout-dates")
    assert err and "the key 'reader' lacks the 'jobs' scope" in text
    err, job = Rpc(shared, "w").call("start_job", kind="create-blackout-dates")
    assert not err and job["owner"] == "worker"
    # Another key cannot see it.
    err, text = Rpc(shared, "o").call("job_status", job_id=job["id"])
    assert err and "no job" in text


def test_shared_refuses_heavy_jobs(shared):
    err, text = Rpc(shared, "w").call(
        "start_job", kind="download", params={"granule_ids": ["G1"]}
    )
    assert err and "does not run 'download' jobs" in text


def test_stdio_server(tmp_path, viewer_page):
    """`nisar-db mcp` speaks MCP over stdio to the SDK's own client."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def session() -> tuple[int, dict]:
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "nisar_db.cli",
                "mcp",
                "--cache-dir",
                str(tmp_path),
                "--viewer-html",
                str(viewer_page),
            ],
        )
        async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            res = await s.call_tool("get_frame", {"key": "5826"})
            return len(tools.tools), json.loads(res.content[0].text)

    n_tools, frame = asyncio.run(session())
    assert n_tools == 42 and frame["id"] == "34_19"


def test_viewer_links_point_where_clients_reach_the_server():
    from nisar_db.api.mcp_http import viewer_url

    assert viewer_url(Settings.for_mode("local", port=8800)) == "http://127.0.0.1:8800"
    assert viewer_url(Settings.for_mode("shared")) == "http://127.0.0.1:8797"
    assert (
        viewer_url(Settings.for_mode("shared", public_url="https://x.org/nisar"))
        == "https://x.org/nisar"
    )


@pytest.mark.parametrize(
    ("origin", "status"),
    [("https://opera-adt.github.io", 200), ("https://evil.example", 403)],
)
def test_local_mcp_accepts_the_published_viewer_only(local_rpc, origin, status):
    """The published page's assistant reaches a local /mcp; other sites don't."""
    r = local_rpc.client.post(
        "/mcp",
        headers={**HEADERS, "Origin": origin},
        json={"jsonrpc": "2.0", "id": 99, "method": "tools/list", "params": {}},
    )
    assert r.status_code == status
