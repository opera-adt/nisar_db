"""``nisar-db serve``: run the REST API, locally or as a shared service."""

from __future__ import annotations

import os
from pathlib import Path

import click


@click.command()
@click.option(
    "--mode",
    type=click.Choice(["local", "shared"]),
    default=None,
    help=(
        "local: 127.0.0.1, no keys (default). shared: API keys, rate limits, "
        "the server's own Earthdata login."
    ),
)
@click.option(
    "--host",
    default=None,
    help="Address to listen on (local: 127.0.0.1; shared: 0.0.0.0).",
)
@click.option("--port", type=int, default=8797, show_default=True)
@click.option(
    "--cache-dir",
    type=click.Path(path_type=Path),
    default=Path(".qa_helper_cache"),
    show_default=True,
    help="QA images, grid corners, rebuilt views and job folders.",
)
@click.option(
    "--viewer-html",
    default=None,
    help="Viewer page served at / (path or URL; default: the checkout's).",
)
@click.option(
    "--trackframe-gpkg",
    type=click.Path(path_type=Path, exists=True),
    default=None,
    help="TrackFrame GeoPackage for rebuilds (downloaded when omitted).",
)
@click.option(
    "--repo-dir",
    type=click.Path(path_type=Path, exists=True),
    default=None,
    help="nisar_db checkout with scripts/ (found when installed from one).",
)
@click.option(
    "--keys-file",
    type=click.Path(path_type=Path, exists=True),
    default=None,
    help=(
        "JSON list of {name, sha256|key, scopes}; "
        "also NISAR_DB_API_KEYS=name:key:read+jobs,..."
    ),
)
@click.option(
    "--private-read/--public-read",
    default=False,
    show_default=True,
    help="Shared mode: whether catalog queries and the viewer need a key too.",
)
@click.option(
    "--cors-origin",
    "cors_origins",
    multiple=True,
    help="Origin a browser may call the API from (repeatable; local default: any).",
)
@click.option(
    "--rate-limit",
    type=int,
    default=None,
    help=(
        "Requests per minute per client on Earthdata-backed routes "
        "(shared default 60; 0 = off)."
    ),
)
@click.option(
    "--public-url",
    default=None,
    help="Address clients reach this service at, for viewer links (behind a proxy).",
)
@click.option(
    "--max-jobs", type=int, default=None, help="Jobs running at once (default 2)."
)
@click.option(
    "--allow-job",
    "allow_jobs",
    multiple=True,
    type=click.Choice(["download", "build-s3-catalog", "build-disp-assets"]),
    help="Shared mode: also run this heavy job kind (repeatable).",
)
def serve(
    mode,
    host,
    port,
    cache_dir,
    viewer_html,
    trackframe_gpkg,
    repo_dir,
    keys_file,
    private_read,
    cors_origins,
    rate_limit,
    public_url,
    max_jobs,
    allow_jobs,
):
    """Serve the REST API (docs at /docs), the viewer and its helper."""
    try:
        import uvicorn

        from nisar_db.api.app import create_app
        from nisar_db.api.settings import Settings, load_keys
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise click.ClickException(
            f"the API needs the 'api' extra: pip install 'nisar_db[api]' ({exc})"
        ) from exc

    mode = mode or os.environ.get("NISAR_DB_API_MODE", "local")
    keys_file = keys_file or (
        Path(os.environ["NISAR_DB_API_KEYS_FILE"])
        if os.environ.get("NISAR_DB_API_KEYS_FILE")
        else None
    )
    try:
        keys = load_keys(keys_file, os.environ.get("NISAR_DB_API_KEYS"))
        given = {
            "host": host,
            "port": port,
            "cache_dir": cache_dir,
            "viewer_html": viewer_html,
            "trackframe_gpkg": trackframe_gpkg,
            "repo_dir": repo_dir,
            "keys": keys or None,
            "private_read": private_read,
            "cors_origins": tuple(cors_origins) or None,
            "rate_limit": rate_limit,
            "public_url": public_url,
            "max_jobs": max_jobs,
            "allowed_heavy_jobs": frozenset(allow_jobs) or None,
        }
        # Options left unset keep the mode's defaults.
        settings = Settings.for_mode(
            mode, **{k: v for k, v in given.items() if v is not None}
        )
        app = create_app(settings)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"nisar_db API ({settings.mode}) on "
        f"http://{settings.host}:{settings.port}/  docs: /docs"
    )
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
