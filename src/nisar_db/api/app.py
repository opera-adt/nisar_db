"""Build the FastAPI app from ``Settings``."""

from __future__ import annotations

import inspect
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from nisar_db.api import helper as helper_routes
from nisar_db.api import routes, routes_more, viewer
from nisar_db.api.jobs import Jobs, Launcher
from nisar_db.api.security import RateLimiter
from nisar_db.api.settings import HEAVY_JOBS, Settings
from nisar_db.api.store import FrameStore

DESCRIPTION = """
Catalog queries over the frames the viewer shows, `nisar-db` commands as
background jobs, viewer pages opened in a given state, and the viewer helper
(QA images, grid corners, rebuilds).

**Local mode** listens on 127.0.0.1 and asks for no key. **Shared mode** needs
an API key (`X-API-Key` header or `Authorization: Bearer`) for jobs and
rebuilds, and for everything when reads are private.

AI assistants reach the same catalog and job tools over MCP at `/mcp`.
"""


class PrivateNetworkAccess:
    """Let a public page (the published viewer) call this server on 127.0.0.1.

    Chrome asks before a page on the internet reaches a private address; the
    answer is ``Access-Control-Allow-Private-Network``. Written out rather than
    left to ``CORSMiddleware``, which takes no such option before Starlette 0.38.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap ``app``."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Add the header to every HTTP response."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                # A newer Starlette's CORS answer already carries it.
                MutableHeaders(scope=message).setdefault(
                    "Access-Control-Allow-Private-Network", "true"
                )
            await send(message)

        await self.app(scope, receive, send_with_header)


def _published(settings: Settings) -> Path | None:
    if settings.viewer_html and not settings.viewer_html.startswith(
        ("http://", "https://")
    ):
        return Path(settings.viewer_html)
    return helper_routes.default_viewer(settings)


def create_app(
    settings: Settings | None = None, *, launcher: Launcher | None = None
) -> FastAPI:
    """Return the API app for ``settings`` (local defaults when omitted).

    ``launcher`` replaces how jobs start their process (tests pass a fake).

    Raises
    ------
    ValueError
        For a configuration ``Settings.validate`` refuses.

    """
    settings = settings or Settings()
    settings.validate()
    app = FastAPI(title="nisar_db API", version="1", description=DESCRIPTION)
    app.state.settings = settings
    app.state.store = FrameStore(_published(settings), settings.cache_dir / "views")
    app.state.limiter = RateLimiter(settings.rate_limit)
    allowed = None
    if settings.shared:
        allowed = tuple(
            p
            for p in (
                settings.repo_dir and Path(settings.repo_dir) / "catalog",
                settings.cache_dir / "views",
            )
            if p
        )
    app.state.jobs = Jobs(
        settings.cache_dir / "jobs",
        max_jobs=settings.max_jobs,
        allowed_roots=allowed,
        refuse=HEAVY_JOBS - settings.allowed_heavy_jobs,
        **({"launcher": launcher} if launcher is not None else {}),
    )
    try:
        app.state.helper, app.state.helper_error = helper_routes.Helper(settings), None
    except (RuntimeError, ImportError) as exc:
        # Outside a checkout the catalog and job routes still work.
        app.state.helper, app.state.helper_error = None, str(exc)

    if settings.cors_origins:
        cors: dict = {
            "allow_origins": list(settings.cors_origins),
            "allow_methods": ["GET", "POST", "DELETE"],
            "allow_headers": ["*"],
            "allow_credentials": "*" not in settings.cors_origins,
        }
        # A Starlette that knows private-network preflights refuses them (400)
        # unless told otherwise; older ones ignore the request header.
        if "allow_private_network" in inspect.signature(CORSMiddleware).parameters:
            cors["allow_private_network"] = not settings.shared
        app.add_middleware(CORSMiddleware, **cors)
    if not settings.shared:
        # Outermost, so CORS preflight answers carry it too.
        app.add_middleware(PrivateNetworkAccess)
    app.include_router(routes.router)
    app.include_router(routes_more.router)
    app.include_router(viewer.router)
    app.include_router(helper_routes.router)
    try:
        from nisar_db.api.mcp_http import mount_mcp
    except ImportError:  # pragma: no cover - the mcp package is optional
        app.state.mcp = None
    else:
        mount_mcp(app)
    return app
