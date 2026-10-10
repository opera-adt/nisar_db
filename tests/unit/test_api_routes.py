"""The HTTP API: catalog queries, jobs, viewer links, and the two modes."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi import HTTPException
from fastapi.testclient import TestClient

from nisar_db.api.app import create_app
from nisar_db.api.jobs import Jobs
from nisar_db.api.security import RateLimiter
from nisar_db.api.settings import Settings, load_keys

KEYS = "reader:r-key:read,worker:w-key:read+jobs,other:o-key:jobs,root:a-key:admin"


def _client(tmp_path, viewer_page, mode="local", launcher=None, **kw) -> TestClient:
    if mode == "shared":
        kw.setdefault("keys", load_keys(None, KEYS))
    settings = Settings.for_mode(
        mode,
        cache_dir=tmp_path / "cache",
        viewer_html=str(viewer_page),
        repo_dir=None,
        **kw,
    )
    app = create_app(settings)
    if launcher is not None:
        app.state.jobs = Jobs(
            tmp_path / "cache" / "jobs", launcher=launcher, refuse=app.state.jobs.refuse
        )
    return TestClient(app)


@pytest.fixture
def local(tmp_path, viewer_page):
    return _client(tmp_path, viewer_page)


def test_health_reports_the_mode(local):
    body = local.get("/health").json()
    assert body["mode"] == "local" and body["page_login"] is True


def test_datasets_list_the_published_page(local):
    (ds,) = local.get("/api/v1/datasets").json()
    assert ds["id"] == "published"
    assert local.get("/api/v1/datasets/published").json()["n_frames"] == 2


def test_frames_filter_and_page(local):
    body = local.get("/api/v1/frames", params={"cycle": "22", "limit": 1}).json()
    assert body["total"] == 1 and body["frames"][0]["n_selected"] == 1
    assert "granules" not in body["frames"][0]
    geo = local.get(
        "/api/v1/frames", params={"format": "geojson", "direction": "desc"}
    ).json()
    assert (
        geo["type"] == "FeatureCollection"
        and geo["features"][0]["properties"]["frame_idx"] == 60
    )


@pytest.mark.parametrize(
    ("params", "message"),
    [({"bbox": "1,2,3"}, "bbox is"), ({"track": "a-b"}, "track:")],
)
def test_bad_filters_are_400(local, params, message):
    r = local.get("/api/v1/frames", params=params)
    assert r.status_code == 400 and message in r.json()["detail"]


def test_frame_detail_granules_and_blackout(local):
    assert local.get("/api/v1/frames/T34_F19").json()["frame_idx"] == 5826
    assert (
        "geometry" in local.get("/api/v1/frames/5826", params={"geometry": True}).json()
    )
    pairs = local.get(
        "/api/v1/frames/34_19/granules", params={"product": "gunw", "cycle": "23"}
    ).json()
    assert pairs["total"] == 1 and pairs["items"][0]["dt"] == 12
    assert (
        local.get("/api/v1/frames/34_19/blackout").json()["month_share"]["Jan"] == 1.0
    )
    assert local.get("/api/v1/frames/99_99").status_code == 404
    assert local.get("/api/v1/frames", params={"dataset": "nope"}).status_code == 404


def test_cycles_and_summary(local):
    assert [c["cycle"] for c in local.get("/api/v1/cycles").json()] == [22, 23]
    summary = local.get("/api/v1/summary", params={"calval": True}).json()
    assert summary["consistent"]["n_frames"] == 1 and summary["rollout"][0] == {
        "option": "P1",
        "shown": 1,
        "total": 1,
    }


def test_viewer_link(local):
    body = local.get(
        "/api/v1/viewer/link",
        params={
            "dataset": "globe-20261009T193409",
            "product": "gunw",
            "cycle": "20-25",
            "sky": "space",
            "opera": False,
        },
    ).json()
    assert (
        body["url"]
        == "http://testserver/view/globe-20261009T193409?product=gunw&opera=0&cycle=20-25&sky=space"
    )
    assert (
        local.get("/api/v1/viewer/link", params={"center": "200,0"}).status_code == 400
    )
    assert (
        local.get("/api/v1/viewer/link", params={"dataset": "../etc"}).status_code
        == 400
    )
    assert "play" in local.get("/api/v1/viewer/params").json()


def test_viewer_helper_needs_a_checkout(local):
    r = local.get("/corners/NISAR_L2_PR_GSLC_X.json")
    assert r.status_code == 503 and "checkout" in r.json()["detail"]


def test_jobs_round_trip(tmp_path, viewer_page, make_launcher, waiter):
    c = _client(tmp_path, viewer_page, launcher=make_launcher(outputs=("results.csv",)))
    job = c.post("/api/v1/jobs", json={"kind": "search", "params": {"track": 34}})
    assert job.status_code == 202
    job_id = job.json()["id"]
    assert waiter(lambda: c.get(f"/api/v1/jobs/{job_id}").json()["state"] == "done")
    assert c.get(f"/api/v1/jobs/{job_id}/files/results.csv").text == "out\n"
    assert c.get(f"/api/v1/jobs/{job_id}/files/job.json").status_code == 404
    assert [j["id"] for j in c.get("/api/v1/jobs").json()] == [job_id]


@pytest.mark.parametrize(
    ("payload", "status"),
    [
        ({"kind": "nope"}, 404),
        ({"kind": "search", "params": {"x": 1}}, 422),
        ({"kind": "create-blackout-dates", "params": {"source": "snow"}}, 400),
    ],
)
def test_bad_jobs(tmp_path, viewer_page, make_launcher, payload, status):
    c = _client(tmp_path, viewer_page, launcher=make_launcher())
    assert c.post("/api/v1/jobs", json=payload).status_code == status


def test_job_kinds_show_what_a_shared_service_refuses(tmp_path, viewer_page):
    c = _client(tmp_path, viewer_page, mode="shared")
    kinds = {k["kind"]: k["enabled"] for k in c.get("/api/v1/jobs/kinds").json()}
    assert kinds["search"] and not kinds["download"] and not kinds["build-s3-catalog"]


# -- shared mode ------------------------------------------------------------


@pytest.fixture
def shared(tmp_path, viewer_page, make_launcher):
    return _client(tmp_path, viewer_page, mode="shared", launcher=make_launcher())


def test_shared_reads_are_public_by_default(shared):
    assert shared.get("/api/v1/frames").status_code == 200


def test_shared_private_reads_need_a_key(tmp_path, viewer_page):
    c = _client(tmp_path, viewer_page, mode="shared", private_read=True)
    assert c.get("/api/v1/frames").status_code == 401
    assert c.get("/api/v1/frames", headers={"X-API-Key": "r-key"}).status_code == 200
    assert (
        c.get("/api/v1/frames", headers={"Authorization": "Bearer r-key"}).status_code
        == 200
    )


def test_shared_jobs_need_the_jobs_scope(shared):
    body = {"kind": "create-blackout-dates"}
    assert shared.post("/api/v1/jobs", json=body).status_code == 401
    assert (
        shared.post(
            "/api/v1/jobs", json=body, headers={"X-API-Key": "wrong"}
        ).status_code
        == 401
    )
    r = shared.post("/api/v1/jobs", json=body, headers={"X-API-Key": "r-key"})
    assert r.status_code == 403 and "lacks the 'jobs' scope" in r.json()["detail"]
    assert (
        shared.post(
            "/api/v1/jobs", json=body, headers={"X-API-Key": "w-key"}
        ).status_code
        == 202
    )


def test_shared_refuses_heavy_jobs(shared):
    r = shared.post(
        "/api/v1/jobs",
        json={"kind": "download", "params": {"granule_ids": ["G1"]}},
        headers={"X-API-Key": "w-key"},
    )
    assert r.status_code == 403


def test_shared_jobs_are_private_to_their_owner(shared):
    job_id = shared.post(
        "/api/v1/jobs",
        json={"kind": "create-blackout-dates"},
        headers={"X-API-Key": "w-key"},
    ).json()["id"]
    assert [
        j["owner"]
        for j in shared.get("/api/v1/jobs", headers={"X-API-Key": "o-key"}).json()
    ] == []
    assert (
        shared.get(f"/api/v1/jobs/{job_id}", headers={"X-API-Key": "o-key"}).status_code
        == 404
    )
    assert (
        shared.delete(
            f"/api/v1/jobs/{job_id}", headers={"X-API-Key": "o-key"}
        ).status_code
        == 404
    )
    assert (
        shared.get(f"/api/v1/jobs/{job_id}", headers={"X-API-Key": "a-key"}).status_code
        == 200
    )


def test_session_cookie_carries_the_key(tmp_path, viewer_page):
    c = _client(tmp_path, viewer_page, mode="shared", private_read=True)
    assert c.post("/api/v1/session", json={"key": "bad"}).status_code == 401
    r = c.post("/api/v1/session", json={"key": "r-key"})
    assert r.json() == {"ok": True, "name": "reader", "scopes": ["read"]}
    assert "httponly" in r.headers["set-cookie"].lower()
    assert c.get("/api/v1/frames").status_code == 200
    c.delete("/api/v1/session")
    c.cookies.clear()
    assert c.get("/api/v1/frames").status_code == 401


def test_rate_limiter_refuses_past_the_limit():
    limiter = RateLimiter(2)
    limiter.check("a")
    limiter.check("a")
    limiter.check("b")
    with pytest.raises(HTTPException) as err:
        limiter.check("a")
    assert err.value.status_code == 429 and int(err.value.headers["Retry-After"]) > 0


def test_rate_limit_zero_is_off():
    limiter = RateLimiter(0)
    for _ in range(100):
        limiter.check("a")


def test_create_app_refuses_unsafe_settings(tmp_path):
    with pytest.raises(ValueError, match="shared mode needs at least one API key"):
        create_app(Settings.for_mode("shared", cache_dir=tmp_path, repo_dir=None))


def test_local_mode_answers_private_network_preflights(local):
    r = local.options(
        "/health",
        headers={
            "Origin": "https://opera-adt.github.io",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Private-Network": "true",
        },
    )
    assert r.headers["access-control-allow-private-network"] == "true"
    assert r.headers["access-control-allow-origin"] == "*"


def test_shared_mode_does_not_open_the_private_network(shared):
    assert "access-control-allow-private-network" not in shared.get("/health").headers


def test_local_preflight_from_the_published_page_is_allowed(local):
    """Regression: Starlette's CORS refused (400) a preflight asking for
    private-network access, so the published viewer could not reach a local
    server's /mcp or QA routes."""
    r = local.options(
        "/mcp",
        headers={
            "Origin": "https://opera-adt.github.io",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
            "Access-Control-Request-Private-Network": "true",
        },
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-private-network"] == "true"
