"""The MCP tools at ``/mcp`` on the REST API (streamable HTTP).

Stateless JSON responses, so any number of clients can call it without
sessions. A shared service asks for an API key on every request, the same keys
as the REST API; the caller is handed to the tools, which check its scopes.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from nisar_db.api.mcp_tools import CALLER, McpContext, build_mcp
from nisar_db.api.security import Caller, match_key, presented_key
from nisar_db.api.settings import Settings

#: Where the published viewer lives; its assistant calls a local `/mcp`.
PUBLISHED_VIEWER_ORIGIN = "https://opera-adt.github.io"


def viewer_url(settings: Settings) -> str:
    """Return the base URL viewer links point to."""
    if settings.public_url:
        return settings.public_url
    host = "127.0.0.1" if settings.host in ("0.0.0.0", "::") else settings.host
    return f"http://{host}:{settings.port}"


class McpDoor:
    """Identify the caller of ``/mcp`` (by API key on a shared service)."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        """Wrap the MCP endpoint ``app``."""
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Refuse a shared-mode request without a key; pass the caller on."""
        caller = Caller("local")
        if scope["type"] == "http" and self.settings.shared:
            key = match_key(self.settings, presented_key(Request(scope)))
            if key is None:
                await JSONResponse(
                    {
                        "error": (
                            "an API key is needed (X-API-Key header or Bearer token)"
                        )
                    },
                    status_code=401,
                )(scope, receive, send)
                return
            caller = Caller(key.name, key)
        token = CALLER.set(caller)
        try:
            await self.app(scope, receive, send)
        finally:
            CALLER.reset(token)


def mount_mcp(app: FastAPI) -> None:
    """Add ``/mcp`` to ``app`` and run the MCP session manager with it."""
    state = app.state
    settings: Settings = state.settings
    server = build_mcp(
        McpContext(
            settings, state.store, state.jobs, viewer_url(settings), state.helper
        )
    )
    # Local mode keeps the SDK's DNS-rebinding check (a web page must not reach
    # a 127.0.0.1 server through a hostile name); a shared service is reached
    # by its own names, behind keys.
    security = (
        TransportSecuritySettings(enable_dns_rebinding_protection=False)
        if settings.shared
        else TransportSecuritySettings(
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
            # The published viewer calls a local server's tools too.
            allowed_origins=[
                "http://127.0.0.1:*",
                "http://localhost:*",
                PUBLISHED_VIEWER_ORIGIN,
            ],
        )
    )
    mcp_app = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=security,
    )
    (route,) = mcp_app.routes
    route.app = McpDoor(route.app, settings)  # type: ignore[attr-defined]
    app.router.routes.append(route)
    state.mcp = server

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with server.session_manager.run():
            yield

    app.router.lifespan_context = lifespan
