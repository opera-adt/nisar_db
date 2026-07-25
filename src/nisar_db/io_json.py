"""Shared JSON writers for NISAR catalog / frame products.

Two output conventions are used across the package:

- Plain ``.json`` sidecar plus a compressed ``.json.zip`` (the frame-to-bound,
  consistent-GSLC, blackout and reference-date artifacts), written by
  :func:`write_zipped_json`.
- A single indented ``.json`` stamped with a ``generated_at`` field (the GSLC/
  GUNW catalog JSONs), written by :func:`write_catalog_json`.
"""

from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path


def write_zipped_json(
    json_path: str | Path,
    data: dict,
    level: int = 6,
    write_plain: bool = True,
) -> str:
    """Write ``data`` as an (optional) plain ``.json`` and a ``.json.zip``.

    Parameters
    ----------
    json_path : str or Path
        Destination path for the plain JSON; the zip is written alongside as
        ``<json_path>.zip``.
    data : dict
        JSON-serialisable payload. Non-serialisable values fall back to ``str``.
    level : int
        DEFLATE compression level for the zip.
    write_plain : bool
        Also write the uncompressed ``.json`` sidecar (default True).

    Returns
    -------
    str
        Path to the written ``.json.zip``.

    """
    json_path = Path(json_path)
    if write_plain:
        with open(json_path, "w") as f:
            json.dump(data, f, indent=2, default=str)

    zip_path = str(json_path) + ".zip"
    with zipfile.ZipFile(
        zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=level
    ) as zf:
        zf.writestr(json_path.name, json.dumps(data, default=str))

    return zip_path


def write_catalog_json(output_dir: str | Path, filename: str, key: str, value) -> None:
    """Write ``{key: value, "generated_at": <utc-now>}`` to output_dir/filename."""
    with (Path(output_dir) / filename).open("w") as f:
        json.dump(
            {key: value, "generated_at": datetime.now(timezone.utc).isoformat()},
            f,
            indent=2,
        )
