#!/usr/bin/env python
"""Serve NISAR QA report images to the frame viewer from this machine.

The public browse PNG of a GUNW shows only its unwrapped phase. The wrapped
phase, coherence, connected components and ionosphere screen are drawn only in
the product's ``_QA_REPORT.pdf``, which sits behind the Earthdata login, and
ASF's download endpoint does not let a web page send that login. This helper
does it instead: when the viewer asks for a granule, it downloads the report
(~400 kB) with your Earthdata login, pulls the raster images out of it, and
caches them on disk. Nothing is hosted; each image is fetched once, on request.

The login is the ``urs.earthdata.nasa.gov`` entry of ``$NETRC`` or
``~/.netrc``. Without one, the viewer's Earthdata panel can hand the helper a
username and password; they are kept in memory only and never leave this
machine except to Earthdata itself.

It also reads the grid corners of the product (a few small byte-range reads),
so the viewer can place the images, and the public ``_LATLON`` browse, on the
map.

It also serves the viewer itself at ``http://127.0.0.1:<port>/`` (the
published page, or ``--viewer-html``). Opened from there, page and helper share
one origin, so no browser rule stands between them: Chrome otherwise asks for
"local network access" before a public site may reach a program on this
computer, and may refuse it. When the browser runs on another computer than
the helper (a laptop viewing a server), forward the port, e.g.
``ssh -L 8797:127.0.0.1:8797 <server>``, and open the same address there.

Endpoints (all JSON or PNG, with permissive CORS so the published viewer can
call it):

* ``/`` -- the viewer, served from this helper;
* ``/health`` -- liveness check, and where the login comes from (``auth``:
  ``netrc``, ``page`` or ``none``);
* ``POST /login`` -- ``{"username": ..., "password": ...}`` from the viewer;
* ``POST /logout`` -- forget that login and fall back to the netrc file;
* ``POST /build`` -- ``{"scope": "na" | "globe" | "bbox", "bbox": [w, s, e, n]}``:
  search CMR and rebuild the viewer for that scope in the background
  (``build_local_view.py``); ``GET /build`` reports its progress;
* ``/view/<id>`` -- a page such a build wrote;
* ``/qa/<gid>/index.json`` -- the layers extracted from the report;
* ``/qa/<gid>/<layer>.png`` -- one layer, ``?thumb=1`` for a small copy;
* ``/corners/<gid>.json`` -- the grid's corners and lon/lat bounding box.

Examples
--------
Start it, then open the viewer (local or published) in Chrome, Edge or Firefox::

    python scripts/qa_browse_server.py --cache-dir ~/.cache/nisar_db/qa_browse

"""

from __future__ import annotations

import argparse
import io
import json
import netrc
import os
import re
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

import requests
from collect_granule_flags import BLOCK_SIZE, COLLECTIONS, product_url

if TYPE_CHECKING:
    from PIL import Image

REPORT_URL = (
    "https://nisar.asf.earthdatacloud.nasa.gov/NISAR/{collection}/{gid}/"
    "{gid}_QA_REPORT.pdf"
)
GID = re.compile(r"^NISAR_L2_PR_(GSLC|GUNW)_[A-Za-z0-9_]+$")
# Report pages are found by their title, and their rasters taken in drawing
# order; colourbars and other small images are skipped. Most specific title
# first: the wrapped group's page also mentions coherence.
LAYER_PAGES = [
    ("Wrapped Phase Image Group", ["wrapped", "coherence_wrapped"]),
    ("Ionosphere Phase Screen", ["iono", "iono_unc"]),
    ("Connected Components", ["cc"]),
    ("Coherence Magnitude (Unwrapped Group)", ["coherence"]),
    ("Unwrapped Phase Image", ["unwrapped", "rewrapped"]),
]
LAYER_LABELS = {
    "wrapped": "Wrapped phase",
    "coherence_wrapped": "Coherence (wrapped group)",
    "coherence": "Coherence",
    "cc": "Connected components",
    "unwrapped": "Unwrapped phase",
    "rewrapped": "Unwrapped, rewrapped",
    "iono": "Ionosphere screen",
    "iono_unc": "Ionosphere uncertainty",
}
MIN_SIDE = 100
# The connected-component mask is drawn at full resolution; this is plenty to
# show which parts unwrapped.
MAX_SIDE = 720
THUMB_SIDE = 128
VALID_CC_COLOR = (77, 210, 201, 255)


URS_HOST = "urs.earthdata.nasa.gov"
PUBLISHED_VIEWER = (
    "https://opera-adt.github.io/nisar_db/assets/opera_nisar_db_viewer.html"
)
# Tells the page it came from the helper, so it calls the helper on its own
# origin rather than on the default address.
SAME_ORIGIN_MARK = '<meta name="nisar-qa-helper" content="same-origin">'
# Read-only, and answers 401 to a wrong username or password.
URS_CHECK_URL = f"https://{URS_HOST}/api/users/tokens"


class EarthdataSession(requests.Session):
    """A session that sends the login to Earthdata's login host only.

    ``requests`` drops credentials when a redirect leaves the original host,
    and ASF's downloads redirect to ``urs.earthdata.nasa.gov`` to log in; it
    then re-adds credentials only from a netrc file. This session adds its
    own login on that hop, and nowhere else.
    """

    def __init__(self, login: tuple[str, str]) -> None:
        super().__init__()
        self.login = login

    def rebuild_auth(
        self, prepared_request: requests.PreparedRequest, response: requests.Response
    ) -> None:
        """Strip credentials as usual, then add the login for Earthdata."""
        super().rebuild_auth(prepared_request, response)
        if urlparse(prepared_request.url).hostname == URS_HOST:
            prepared_request.prepare_auth(self.login)


class EarthdataAuth:
    """The Earthdata login the helper downloads with.

    A login handed over by the viewer wins; otherwise the netrc file's, read
    afresh each time so a file written after the helper started is picked up.
    """

    def __init__(self) -> None:
        self._page: requests.Session | None = None
        self._lock = threading.Lock()

    @staticmethod
    def _netrc_login() -> tuple[str, str] | None:
        path = os.environ.get("NETRC") or Path.home() / ".netrc"
        try:
            entry = netrc.netrc(path).authenticators(URS_HOST)
        except (FileNotFoundError, netrc.NetrcParseError):
            return None
        return (entry[0], entry[2] or "") if entry else None

    @property
    def source(self) -> str:
        """``page``, ``netrc`` or ``none``."""
        if self._page is not None:
            return "page"
        return "netrc" if self._netrc_login() else "none"

    def login(self, username: str, password: str) -> None:
        """Check ``username`` / ``password`` with Earthdata and use them.

        Raises
        ------
        PermissionError
            If Earthdata rejects them.

        """
        resp = requests.get(URS_CHECK_URL, auth=(username, password), timeout=30)
        if resp.status_code == 401:
            raise PermissionError("Earthdata rejected the username or password")
        resp.raise_for_status()
        with self._lock:
            self._page = EarthdataSession((username, password))

    def logout(self) -> None:
        """Forget the viewer's login."""
        with self._lock:
            self._page = None

    def session(self) -> requests.Session:
        """Return a session carrying the current login.

        Raises
        ------
        PermissionError
            If there is no login at all.

        """
        with self._lock:
            if self._page is not None:
                return self._page
        login = self._netrc_login()
        if login is None:
            raise PermissionError(
                "no Earthdata login: add urs.earthdata.nasa.gov to ~/.netrc "
                "or log in from the viewer"
            )
        return EarthdataSession(login)


def _get(session: requests.Session, url: str, **kwargs: object) -> requests.Response:
    resp = session.get(url, timeout=120, **kwargs)  # type: ignore[arg-type]
    # A rejected login ends on the Earthdata login page rather than an error.
    if resp.status_code == 401 or URS_HOST in resp.url:
        raise PermissionError("Earthdata rejected the login")
    resp.raise_for_status()
    return resp


def report_url(gid: str) -> str:
    """Return the HTTPS URL of a granule's QA report."""
    kind = gid.split("_")[3]
    return REPORT_URL.format(collection=COLLECTIONS[kind], gid=gid)


def _cc_mask(image: Image.Image) -> Image.Image:
    # The mask has two colours: background (no unwrapped data, inside the swath
    # or not) and valid. The corner pixel is always outside the swath.
    from PIL import Image

    rgb = image.convert("RGB")
    rgb.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.NEAREST)
    background = rgb.getpixel((0, 0))
    out = Image.new("RGBA", rgb.size, (0, 0, 0, 0))
    out.putdata(
        [(0, 0, 0, 0) if px == background else VALID_CC_COLOR for px in rgb.getdata()]
    )
    return out


def extract_layers(pdf: bytes) -> dict[str, Image.Image]:
    """Pull the raster layers out of a GUNW QA report.

    Parameters
    ----------
    pdf : bytes
        The ``_QA_REPORT.pdf`` contents.

    Returns
    -------
    dict
        Layer name (see ``LAYER_LABELS``) to an RGBA image covering the
        product's grid; layers the report does not draw are absent.

    """
    import numpy as np
    import pypdf
    from PIL import Image
    from pypdf.generic import ContentStream

    reader = pypdf.PdfReader(io.BytesIO(pdf))
    layers: dict[str, Image.Image] = {}
    for page in reader.pages:
        text = page.extract_text() or ""
        names = next((n for title, n in LAYER_PAGES if title in text), None)
        if names is None or names[0] in layers:
            continue
        # Every page shares one resource dictionary, so the page's own drawing
        # operations say which images are on it. Each is drawn through the
        # matrix set just before it, and the QA rasters are stored bottom row
        # first and flipped back by a negative height there.
        images = []
        matrix = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
        ops = ContentStream(page.get_contents(), reader).operations
        for operands, op in ops:
            if op == b"cm":
                matrix = [float(v) for v in operands]
            elif op == b"Do" and str(operands[0]) in page.images.keys():
                image = page.images[str(operands[0])].image
                if min(image.size) < MIN_SIDE:
                    continue
                if matrix[3] < 0:
                    image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
                if matrix[0] < 0:
                    image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                images.append(image)
        for name, image in zip(names, images, strict=False):
            layers[name] = _cc_mask(image) if name == "cc" else image.convert("RGBA")
    # The mask's plot paints the grid's fill value like a valid component, as a
    # band along the grid edge outside the swath; the coherence image, drawn on
    # the same grid, is transparent there.
    if "cc" in layers and "coherence" in layers:
        swath = (
            layers["coherence"]
            .getchannel("A")
            .resize(layers["cc"].size, Image.Resampling.NEAREST)
        )
        alpha = np.minimum(np.asarray(layers["cc"].getchannel("A")), np.asarray(swath))
        layers["cc"].putalpha(Image.fromarray(alpha))
    return layers


def grid_corners(gid: str, session: requests.Session) -> dict:
    """Read a product's grid corners from its HDF5 file.

    Parameters
    ----------
    gid : str
        GSLC or GUNW granule id.
    session : requests.Session
        Session carrying the Earthdata login.

    Returns
    -------
    dict
        ``epsg``; ``quad``, the grid's outer corners as ``[lon, lat]`` in the
        order upper-left, upper-right, lower-right, lower-left (how the QA
        images, which are drawn on the grid, are placed); and ``bbox``,
        ``[west, south, east, north]`` of the whole grid in lon / lat (how the
        public ``_LATLON`` browse, the grid reprojected to EPSG:4326, is).

    """
    import fsspec
    import h5py
    import numpy as np
    from pyproj import Transformer

    signed = _get(session, product_url(gid), headers={"Range": "bytes=0-0"}).url
    with (
        fsspec.filesystem("http").open(
            signed, block_size=BLOCK_SIZE, cache_type="blockcache"
        ) as fo,
        h5py.File(fo) as h5,
    ):
        if gid.split("_")[3] == "GUNW":
            unw = h5["science/LSAR/GUNW/grids/frequencyA/unwrappedInterferogram"]
            grid = unw[sorted(k for k in unw if isinstance(unw[k], h5py.Group))[0]]
        else:
            grids = h5["science/LSAR/GSLC/grids"]
            grid = grids[sorted(k for k in grids if k.startswith("frequency"))[0]]
        x = grid["xCoordinates"][()]
        y = grid["yCoordinates"][()]
        dx = float(grid["xCoordinateSpacing"][()])
        dy = float(grid["yCoordinateSpacing"][()])
        epsg = int(grid["projection"][()])
    # Coordinates are pixel centres; the image covers the pixels' outer edges.
    left, right = x[0] - dx / 2, x[-1] + dx / 2
    top, bottom = y[0] - dy / 2, y[-1] + dy / 2
    to_lonlat = Transformer.from_crs(epsg, 4326, always_xy=True)
    quad = [
        list(to_lonlat.transform(cx, cy))
        for cx, cy in ((left, top), (right, top), (right, bottom), (left, bottom))
    ]
    # The reprojected browse spans the lon / lat bounds of the whole grid edge,
    # which bow outwards between the corners.
    t = np.linspace(0.0, 1.0, 64)
    edge_x = np.concatenate(
        [
            left + (right - left) * t,
            np.full(64, right),
            right - (right - left) * t,
            np.full(64, left),
        ]
    )
    edge_y = np.concatenate(
        [
            np.full(64, top),
            top + (bottom - top) * t,
            np.full(64, bottom),
            bottom - (bottom - top) * t,
        ]
    )
    lon, lat = to_lonlat.transform(edge_x, edge_y)
    bbox = [float(lon.min()), float(lat.min()), float(lon.max()), float(lat.max())]
    return {"epsg": epsg, "quad": quad, "bbox": bbox}


class QaCache:
    """Disk cache of extracted layers and corners, one folder per granule."""

    def __init__(self, root: Path, auth: EarthdataAuth) -> None:
        self.root = root
        self.auth = auth
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock(self, key: str) -> threading.Lock:
        # Two thumbnails of one granule asked for at once must not download
        # the report twice.
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def layers(self, gid: str) -> list[str]:
        """Return the granule's layer names, extracting them on first use."""
        folder = self.root / gid
        index = folder / "index.json"
        with self._lock(f"layers:{gid}"):
            if not index.exists():
                names: list[str] = []
                if gid.split("_")[3] == "GUNW":
                    from PIL import Image

                    resp = _get(self.auth.session(), report_url(gid))
                    folder.mkdir(parents=True, exist_ok=True)
                    for name, image in extract_layers(resp.content).items():
                        image.save(folder / f"{name}.png", optimize=True)
                        thumb = image.copy()
                        thumb.thumbnail(
                            (THUMB_SIDE, THUMB_SIDE), Image.Resampling.LANCZOS
                        )
                        thumb.save(folder / f"{name}_thumb.png", optimize=True)
                        names.append(name)
                folder.mkdir(parents=True, exist_ok=True)
                index.write_text(json.dumps(names))
        return json.loads(index.read_text())

    def corners(self, gid: str) -> dict:
        """Return the granule's grid corners, reading them on first use."""
        path = self.root / gid / "corners.json"
        with self._lock(f"corners:{gid}"):
            if not path.exists():
                corners = grid_corners(gid, self.auth.session())
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(corners))
        return json.loads(path.read_text())


class ViewerPage:
    """The viewer page the helper serves: a local file, or the published page.

    A local file is read again whenever it changes; the published page is
    fetched again at most every ``ttl`` seconds, so a long-running helper
    follows the weekly rebuild.
    """

    def __init__(self, source: str, ttl: float = 600.0) -> None:
        self.source = source
        self.ttl = ttl
        self._html: bytes | None = None
        self._stamp: float | None = None
        self._lock = threading.Lock()

    def _current_stamp(self) -> float:
        if self.source.startswith(("http://", "https://")):
            # Changes once per ``ttl`` window.
            return float(int(time.time() // self.ttl))
        return Path(self.source).stat().st_mtime

    def html(self) -> bytes:
        """Return the page, marked as served by the helper."""
        with self._lock:
            stamp = self._current_stamp()
            if self._html is None or stamp != self._stamp:
                if self.source.startswith(("http://", "https://")):
                    resp = requests.get(self.source, timeout=60)
                    resp.raise_for_status()
                    text = resp.text
                else:
                    text = Path(self.source).read_text()
                self._html = text.replace(
                    "<head>", f"<head>{SAME_ORIGIN_MARK}", 1
                ).encode()
                self._stamp = stamp
            return self._html


VIEW_ID = re.compile(r"^[a-z]+-[0-9TZ]+$")


class ViewBuilds:
    """Rebuilds of the viewer for another scope, one at a time.

    A build searches CMR and renders a page with ``build_local_view``; the
    viewer polls :meth:`status` and opens the page when it is done.
    """

    def __init__(self, root: Path, trackframe_gpkg: Path | None = None) -> None:
        self.root = root / "views"
        self.trackframe_gpkg = trackframe_gpkg
        self._job: dict | None = None
        self._lock = threading.Lock()

    def status(self) -> dict:
        """Return the current (or last) build, or ``{"state": "idle"}``."""
        with self._lock:
            return dict(self._job) if self._job else {"state": "idle"}

    def start(self, scope: str, bbox: list[float] | None) -> dict:
        """Start a build unless one is running; return its status.

        Raises
        ------
        ValueError
            For an unknown scope or a malformed box.

        """
        if scope not in ("na", "globe", "bbox"):
            raise ValueError(f"unknown scope {scope!r}")
        if scope == "bbox":
            if bbox is None or len(bbox) != 4:
                raise ValueError("the bbox scope needs [west, south, east, north]")
            west, south, east, north = (float(v) for v in bbox)
            if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
                raise ValueError("bbox must be west < east, south < north, in degrees")
            bbox = [round(v, 4) for v in (west, south, east, north)]
        with self._lock:
            if self._job and self._job["state"] == "running":
                return dict(self._job)
            stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
            self._job = {
                "id": f"{scope}-{stamp}",
                "scope": scope,
                "bbox": bbox if scope == "bbox" else None,
                "state": "running",
                "step": "Starting",
                "started": time.time(),
            }
            job = dict(self._job)
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def _update(self, **fields: object) -> None:
        with self._lock:
            assert self._job is not None
            self._job.update(fields)

    def _run(self, job: dict) -> None:
        try:
            # Heavy (geopandas and the viewer generator); only builds need it.
            from build_local_view import build_view
            from nisar_db.geodb import get_trackframe_db

            gpkg = self.trackframe_gpkg
            if gpkg is None:
                self._update(step="Fetching the NISAR frame database")
                gpkg = get_trackframe_db(output_dir=self.root.parent)
            out = self.root / f"{job['id']}.html"
            meta = build_view(
                job["scope"],
                out,
                Path(gpkg),
                tuple(job["bbox"]) if job["bbox"] else None,
                progress=lambda msg: self._update(step=msg),
            )
            self._update(
                state="done",
                view=f"/view/{job['id']}",
                n_frames=meta["n_frames"],
                finished=time.time(),
            )
        except Exception as exc:  # noqa: BLE001 - reported to the page
            self._update(state="error", step=f"{type(exc).__name__}: {exc}")

    def page(self, view_id: str) -> bytes | None:
        """Return a built page, marked as served by the helper."""
        path = self.root / f"{view_id}.html"
        if not VIEW_ID.match(view_id) or not path.exists():
            return None
        return (
            path.read_text().replace("<head>", f"<head>{SAME_ORIGIN_MARK}", 1).encode()
        )


def make_handler(
    cache: QaCache,
    viewer: ViewerPage | None = None,
    builds: ViewBuilds | None = None,
) -> type[BaseHTTPRequestHandler]:
    """Build the request handler bound to ``cache``."""

    class Handler(BaseHTTPRequestHandler):
        def _headers(
            self, status: int, content_type: str, length: int, cache: bool = False
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Access-Control-Allow-Origin", "*")
            # Chrome asks before a public page may reach a local server.
            self.send_header("Access-Control-Allow-Private-Network", "true")
            # Images never change; the status (and the login it reports) does.
            self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
            self.end_headers()

        def _json(self, payload: object, status: int = HTTPStatus.OK) -> None:
            body = json.dumps(payload).encode()
            self._headers(status, "application/json", len(body))
            self.wfile.write(body)

        def do_OPTIONS(self) -> None:  # noqa: N802 - http.server naming
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802 - http.server naming
            url = urlparse(self.path)
            parts = [p for p in url.path.split("/") if p]
            try:
                if parts in ([], ["index.html"]) and viewer is not None:
                    body = viewer.html()
                    self._headers(HTTPStatus.OK, "text/html; charset=utf-8", len(body))
                    self.wfile.write(body)
                elif len(parts) == 2 and parts[0] == "view" and builds is not None:
                    page = builds.page(parts[1])
                    if page is None:
                        self._json({"error": "no such view"}, HTTPStatus.NOT_FOUND)
                        return
                    self._headers(HTTPStatus.OK, "text/html; charset=utf-8", len(page))
                    self.wfile.write(page)
                elif parts == ["build"] and builds is not None:
                    self._json(builds.status())
                elif parts == ["health"]:
                    self._json(
                        {
                            "ok": True,
                            "service": "nisar_db qa_browse_server",
                            "auth": cache.auth.source,
                        }
                    )
                elif len(parts) == 3 and parts[0] == "qa" and GID.match(parts[1]):
                    gid, leaf = parts[1], parts[2]
                    names = cache.layers(gid)
                    if leaf == "index.json":
                        self._json(
                            {
                                "layers": [
                                    {"name": n, "label": LAYER_LABELS[n]} for n in names
                                ]
                            }
                        )
                        return
                    name = leaf.removesuffix(".png")
                    if name not in names:
                        self._json({"error": f"no layer {name}"}, HTTPStatus.NOT_FOUND)
                        return
                    thumb = parse_qs(url.query).get("thumb") == ["1"]
                    body = (
                        cache.root / gid / f"{name}{'_thumb' if thumb else ''}.png"
                    ).read_bytes()
                    self._headers(HTTPStatus.OK, "image/png", len(body), cache=True)
                    self.wfile.write(body)
                elif len(parts) == 2 and parts[0] == "corners":
                    gid = parts[1].removesuffix(".json")
                    if not GID.match(gid):
                        self._json({"error": "bad granule id"}, HTTPStatus.BAD_REQUEST)
                        return
                    self._json(cache.corners(gid))
                else:
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except PermissionError as exc:
                self._json({"error": str(exc)}, HTTPStatus.UNAUTHORIZED)
            except Exception as exc:  # noqa: BLE001 - report it to the page
                self._json(
                    {"error": f"{type(exc).__name__}: {str(exc).split('?', 1)[0]}"},
                    HTTPStatus.BAD_GATEWAY,
                )

        def do_POST(self) -> None:  # noqa: N802 - http.server naming
            path = urlparse(self.path).path.strip("/")
            if path == "build" and builds is not None:
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                    self._json(
                        builds.start(str(body.get("scope", "na")), body.get("bbox"))
                    )
                except (ValueError, TypeError) as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            if path == "logout":
                cache.auth.logout()
                self._json({"ok": True, "auth": cache.auth.source})
                return
            if path != "login":
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                username, password = str(body["username"]), str(body["password"])
            except (ValueError, KeyError):
                self._json(
                    {"error": "send username and password"}, HTTPStatus.BAD_REQUEST
                )
                return
            if not username or not password:
                self._json(
                    {"error": "send username and password"}, HTTPStatus.BAD_REQUEST
                )
                return
            try:
                cache.auth.login(username, password)
            except PermissionError as exc:
                self._json({"error": str(exc)}, HTTPStatus.UNAUTHORIZED)
                return
            except requests.RequestException as exc:
                self._json(
                    {"error": f"could not reach Earthdata: {type(exc).__name__}"},
                    HTTPStatus.BAD_GATEWAY,
                )
                return
            # Never echo or log the password; the request line has no body.
            self._json({"ok": True, "auth": cache.auth.source})

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            print(f"{self.address_string()} {format % args}", flush=True)

    return Handler


def main(argv: list[str] | None = None) -> None:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8797, help="Port to listen on.")
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path.home() / ".cache" / "nisar_db" / "qa_browse",
        help="Where extracted images and corners are kept.",
    )
    parser.add_argument(
        "--viewer-html",
        default=PUBLISHED_VIEWER,
        help="Viewer page to serve at / (a file, or a URL; default: the published one).",
    )
    parser.add_argument(
        "--trackframe-gpkg",
        type=Path,
        default=None,
        help="NISAR TrackFrame GeoPackage for the viewer's search; downloaded into "
        "--cache-dir on the first search when omitted.",
    )
    args = parser.parse_args(argv)
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(
        ("127.0.0.1", args.port),
        make_handler(
            QaCache(args.cache_dir, EarthdataAuth()),
            ViewerPage(args.viewer_html),
            ViewBuilds(args.cache_dir, args.trackframe_gpkg),
        ),
    )
    print(
        f"QA helper on http://127.0.0.1:{args.port} (cache {args.cache_dir})",
        flush=True,
    )
    print(f"Open the viewer at http://127.0.0.1:{args.port}/", flush=True)
    source = EarthdataAuth().source
    print(
        (
            "Earthdata login: found in ~/.netrc"
            if source == "netrc"
            else "No Earthdata login in ~/.netrc: log in from the viewer's key icon"
        ),
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
