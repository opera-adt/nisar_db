"""Settings for the REST API: one object, two modes.

``local`` is the viewer helper on a workstation: it listens on 127.0.0.1 only,
asks for no key, and may use the Earthdata login a viewer page hands it.
``shared`` is a service other people reach: every request that spends the
server's resources (jobs, rebuilds, QA images from Earthdata) needs an API
key, the page login is off (the server's own ``~/.netrc`` is used), and CMR- or
Earthdata-heavy calls are rate limited.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Mode = Literal["local", "shared"]

#: Scopes an API key can carry. ``read`` covers catalog queries and the viewer
#: (only checked when reads are private), ``jobs`` starting and cancelling
#: jobs and rebuilds, ``admin`` everything, including other people's jobs.
SCOPES = ("read", "jobs", "admin")

#: Job kinds a shared service refuses unless they are allowed explicitly:
#: they move gigabytes or need cloud credentials of the host.
HEAVY_JOBS = frozenset({"download", "build-s3-catalog", "build-disp-assets"})


def _repo_root() -> Path | None:
    """Return the checkout this package is installed from, if any."""
    root = Path(__file__).resolve().parents[3]
    return root if (root / "scripts" / "generate_scope_viewer.py").exists() else None


@dataclass(frozen=True)
class ApiKey:
    """One API key, stored as its SHA-256 so a key file never holds secrets."""

    name: str
    sha256: str
    scopes: frozenset[str]

    def allows(self, scope: str) -> bool:
        """Return whether the key carries ``scope`` (``admin`` carries all)."""
        return "admin" in self.scopes or scope in self.scopes


def hash_key(key: str) -> str:
    """Return the hex SHA-256 of an API key."""
    return hashlib.sha256(key.encode()).hexdigest()


def load_keys(path: Path | None, env: str | None = None) -> tuple[ApiKey, ...]:
    """Read API keys from a JSON file and/or an environment string.

    Parameters
    ----------
    path : Path or None
        JSON list of ``{"name", "sha256" | "key", "scopes"}``. ``key`` (plain)
        is accepted for convenience and hashed on load.
    env : str or None
        ``name:key:scope+scope,...`` entries, e.g. the value of
        ``NISAR_DB_API_KEYS``.

    Returns
    -------
    tuple of ApiKey

    Raises
    ------
    ValueError
        For an entry without a key, or with an unknown scope.

    """
    entries: list[dict] = []
    if path is not None:
        entries += json.loads(Path(path).read_text())
    for item in (env or "").split(","):
        if not item.strip():
            continue
        name, key, *rest = item.strip().split(":")
        entries.append(
            {"name": name, "key": key, "scopes": (rest or ["read"])[0].split("+")}
        )
    keys = []
    for e in entries:
        digest = e.get("sha256") or (hash_key(e["key"]) if e.get("key") else None)
        if not digest:
            raise ValueError(
                f"API key {e.get('name')!r} has neither 'key' nor 'sha256'"
            )
        scopes = frozenset(e.get("scopes") or ["read"])
        unknown = scopes - set(SCOPES)
        if unknown:
            raise ValueError(
                f"API key {e.get('name')!r} has unknown scopes {sorted(unknown)}"
            )
        keys.append(
            ApiKey(name=str(e.get("name", "key")), sha256=digest, scopes=scopes)
        )
    return tuple(keys)


@dataclass
class Settings:
    """Everything ``create_app`` needs; ``Settings.for_mode`` fills the defaults."""

    mode: Mode = "local"
    host: str = "127.0.0.1"
    port: int = 8797
    #: Checkout holding ``scripts/`` (viewer page, rebuild, QA helper code).
    repo_dir: Path | None = field(default_factory=_repo_root)
    #: QA images, grid corners, rebuilt views and job folders live under here.
    cache_dir: Path = Path(".qa_helper_cache")
    #: The page served at ``/`` and the ``published`` dataset; a URL is fetched.
    viewer_html: str | None = None
    #: TrackFrame GeoPackage for rebuilds (downloaded when omitted).
    trackframe_gpkg: Path | None = None
    keys: tuple[ApiKey, ...] = ()
    #: Shared mode: whether catalog queries and the viewer need a key too.
    private_read: bool = False
    cors_origins: tuple[str, ...] = ("*",)
    #: Requests per minute and client for CMR- / Earthdata-heavy endpoints;
    #: 0 turns the limit off.
    rate_limit: int = 0
    max_jobs: int = 2
    #: Base URL of this service as clients reach it (for viewer links), e.g.
    #: behind a proxy; defaults to http://<host>:<port>.
    public_url: str | None = None
    allowed_heavy_jobs: frozenset[str] = frozenset()

    @classmethod
    def for_mode(cls, mode: Mode, **overrides) -> "Settings":
        """Return settings with the defaults of ``mode``, then ``overrides``.

        Every override is applied as given, ``None`` included (``repo_dir=None``
        runs without a checkout); callers with optional values leave them out.
        """
        base: dict = {"mode": mode}
        if mode == "shared":
            base.update(
                host="0.0.0.0",
                cors_origins=(),
                rate_limit=60,
                max_jobs=2,
            )
        else:
            base.update(allowed_heavy_jobs=HEAVY_JOBS)
        base.update(overrides)
        return cls(**base)

    @property
    def shared(self) -> bool:
        """Whether this is the shared service."""
        return self.mode == "shared"

    @property
    def page_login(self) -> bool:
        """Whether a viewer page may hand the server an Earthdata login."""
        return not self.shared

    def validate(self) -> None:
        """Refuse a configuration that would expose the server unprotected.

        Raises
        ------
        ValueError
            Local mode bound to a non-loopback address, or shared mode
            without a single API key.

        """
        if not self.shared and self.host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError(
                f"local mode listens on 127.0.0.1 only (got {self.host}); "
                "use --mode shared to serve other machines"
            )
        if self.shared and not self.keys:
            raise ValueError(
                "shared mode needs at least one API key "
                "(--keys-file or NISAR_DB_API_KEYS)"
            )


def settings_from_env(**overrides) -> Settings:
    """Build settings from ``NISAR_DB_API_*`` variables and ``overrides``."""
    mode = overrides.pop("mode", None) or os.environ.get("NISAR_DB_API_MODE", "local")
    keys_file = os.environ.get("NISAR_DB_API_KEYS_FILE")
    keys = load_keys(
        Path(keys_file) if keys_file else None, os.environ.get("NISAR_DB_API_KEYS")
    )
    if keys:
        overrides["keys"] = keys
    if mode not in ("local", "shared"):
        raise ValueError(f"NISAR_DB_API_MODE is local or shared, not {mode!r}")
    return Settings.for_mode("shared" if mode == "shared" else "local", **overrides)
