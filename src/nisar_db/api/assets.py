"""DISP-NISAR assets: where to get them, and per-frame lookups in them.

Three sources, always labelled:

* ``published`` -- the latest GitHub release of opera-adt/nisar_db;
* ``built`` -- the newest finished ``build-disp-assets`` job on this server;
* ``repo`` -- the blackout dates kept in the checkout
  (``catalog/opera-nisar-disp-blackout-dates.json``).

The per-frame lookups read the real asset files (consistent-GSLC
``data[frame_idx]``, blackout ``blackout_dates[frame_idx]``, reference dates),
never a reconstruction from the viewer's data.
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Any

import requests

RELEASES_URL = "https://api.github.com/repos/opera-adt/nisar_db/releases/latest"

#: Asset kinds by file-name pattern (dates and versions vary).
KINDS = {
    "consistent_gslc": re.compile(
        r"^opera-nisar-disp-consistent-gslc-\d{4}-\d{2}-\d{2}\.json$"
    ),
    "consistent_gslc_no_blackout": re.compile(
        r"^opera-nisar-disp-consistent-gslc-no-blackout\.json$"
    ),
    "consistent_with_processing_mode": re.compile(
        r"^opera-nisar-disp-consistent-gslc-with-processing-mode-.*\.json$"
    ),
    "blackout_dates": re.compile(r"^opera-nisar-disp-blackout-dates.*\.json$"),
    "reference_dates": re.compile(r"^opera-nisar-disp-reference-dates-.*\.json$"),
    "frame_to_bounds": re.compile(r"^opera-nisar-disp-.*frame-to-bounds.*\.json$"),
    "frame_geometries": re.compile(
        r"^opera-nisar-disp-frame-geometries-.*\.geojson(\.zip)?$"
    ),
    "frames_gpkg": re.compile(r"^opera-nisar-disp-frames\.gpkg$"),
    "gslc_catalog": re.compile(r"^gslc_catalog\.csv$"),
}


def kind_of(name: str) -> str | None:
    """Return an asset file's kind, or None.

    >>> kind_of("opera-nisar-disp-consistent-gslc-2026-10-09.json")
    'consistent_gslc'
    >>> kind_of("opera-nisar-disp-consistent-gslc-2026-10-09.json.zip") is None
    True
    """
    return next((k for k, rx in KINDS.items() if rx.match(name)), None)


def published(session: requests.Session | None = None) -> dict[str, Any]:
    """Return the latest GitHub release's tag and assets (or why there are none)."""
    try:
        r = (session or requests).get(
            RELEASES_URL, timeout=30, headers={"Accept": "application/vnd.github+json"}
        )
        r.raise_for_status()
        rel = r.json()
    except requests.RequestException as exc:
        return {
            "tag": None,
            "assets": [],
            "error": f"GitHub did not answer: {type(exc).__name__}",
        }
    return {
        "tag": rel.get("tag_name"),
        "published_at": rel.get("published_at"),
        "url": rel.get("html_url"),
        "assets": [
            {
                "name": a["name"],
                "kind": kind_of(a["name"]),
                "size": a.get("size"),
                "url": a.get("browser_download_url"),
                "updated": a.get("updated_at"),
            }
            for a in rel.get("assets", [])
        ],
    }


def built(jobs: Any) -> dict[str, Any] | None:
    """Return the newest finished ``build-disp-assets`` job's files, or None."""
    for job in jobs.listed():
        if job.kind == "build-disp-assets" and job.state == "done":
            files = [
                {
                    "name": n,
                    "kind": kind_of(Path(n).name),
                    "path": str(jobs.folder(job.id) / n),
                }
                for n in job.outputs
            ]
            return {"job": job.id, "finished": job.finished, "assets": files}
    return None


def repo_blackout(repo_dir: Path | None) -> Path | None:
    """Return the checkout's blackout-dates JSON, if there is one."""
    if repo_dir is None:
        return None
    path = Path(repo_dir) / "catalog" / "opera-nisar-disp-blackout-dates.json"
    return path if path.exists() else None


def overview(
    *, repo_dir: Path | None, jobs: Any, session: requests.Session | None = None
) -> dict[str, Any]:
    """Return every source of DISP-NISAR assets."""
    repo = repo_blackout(repo_dir)
    return {
        "published": published(session),
        "built": built(jobs),
        "repo": (
            [{"name": repo.name, "kind": "blackout_dates", "path": str(repo)}]
            if repo
            else []
        ),
        "build": (
            "start a 'build-disp-assets' job (POST /api/v1/jobs or start_job) "
            "for a fresh set from CMR"
        ),
    }


def load_json(path: Path) -> dict:
    """Read an asset JSON, or the JSON inside its ``.json.zip``."""
    path = Path(path)
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as z:
            return json.loads(z.read(z.namelist()[0]))
    return json.loads(path.read_text())


def local_file(kind: str, *, repo_dir: Path | None, jobs: Any) -> Path | None:
    """Return the newest local file of ``kind``: a built job's, else the repo's."""
    b = built(jobs)
    if b:
        for a in b["assets"]:
            if a["kind"] == kind:
                return Path(a["path"])
    if kind == "blackout_dates":
        return repo_blackout(repo_dir)
    return None


def frame_entry(doc: dict, frame_idx: int | str) -> Any:
    """Return one frame's entry from an asset JSON (``data`` or ``blackout_dates``).

    >>> frame_entry({"data": {"60": {"common_mode": "2005"}}}, 60)
    {'common_mode': '2005'}
    """
    body = doc.get("data", doc.get("blackout_dates", {}))
    return body.get(str(frame_idx))
