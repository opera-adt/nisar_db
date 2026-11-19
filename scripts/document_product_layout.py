#!/usr/bin/env python
"""Write a Markdown page describing the HDF5 layout of NISAR products.

For each granule named on the command line, the product's ``.h5`` is opened
remotely with byte-range reads (only the HDF5 metadata is fetched, so a 20 GB
RSLC takes seconds), every group and dataset is listed with its shape, type,
units and description, and one page per product type is written::

    python scripts/document_product_layout.py \\
        NISAR_L2_PR_GSLC_033_117_A_165_4005_DHDH_A_20261020T084224_20261020T084229_P05023_N_P_J_001 \\
        --output-dir docs/products

Paths that repeat per frequency, polarization or offset layer are folded into
one row (``frequency{A,B}``, ``{pol}``, ``layer{n}``), with the values seen
listed in the row. The Earthdata login comes from ``~/.netrc``, as for the QA
helper.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

import numpy as np

CMR = "https://cmr.earthdata.nasa.gov/search"
POLARIZATIONS = {"HH", "HV", "VH", "VV", "RH", "RV", "LH", "LV"}
#: Datasets this small have their value shown on the page.
SHOW_VALUE_MAX_SIZE = 4


def fold(path: str) -> tuple[str, dict[str, str]]:
    """Fold the per-frequency, per-polarization and per-layer parts of a path.

    Examples
    --------
    >>> fold("science/LSAR/GSLC/grids/frequencyA/HH")
    ('science/LSAR/GSLC/grids/frequency{f}/{pol}', {'f': 'A', 'pol': 'HH'})
    >>> fold("science/LSAR/GOFF/grids/frequencyA/pixelOffsets/HH/layer2/x")
    ('science/LSAR/GOFF/grids/frequency{f}/pixelOffsets/{pol}/layer{n}/x', {'f': 'A', 'pol': 'HH', 'n': '2'})

    """
    seen: dict[str, str] = {}
    parts = []
    for part in path.split("/"):
        if m := re.fullmatch(r"frequency([AB])", part):
            seen["f"] = m.group(1)
            part = "frequency{f}"
        elif part in POLARIZATIONS:
            seen["pol"] = part
            part = "{pol}"
        elif m := re.fullmatch(r"layer(\d+)", part):
            seen["n"] = m.group(1)
            part = "layer{n}"
        parts.append(part)
    return "/".join(parts), seen


def _plain(value: Any) -> Any:
    """Make an HDF5 attribute or small dataset value printable."""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    if isinstance(value, np.ndarray):
        if value.size > SHOW_VALUE_MAX_SIZE:
            return None
        return [_plain(v) for v in value.tolist()]
    if isinstance(value, np.generic):
        return value.item()
    return value


def dtype_name(dtype: np.dtype) -> str:
    """Name an HDF5 dataset type for a Markdown table.

    Examples
    --------
    >>> dtype_name(np.dtype("S6")), dtype_name(np.dtype("complex64"))
    ('string[6]', 'complex64')

    """
    if dtype.kind == "S":
        return f"string[{dtype.itemsize}]"
    if dtype.kind == "O":
        return "string"
    return str(dtype)


def read_layout(h5: Any) -> list[dict]:
    """List every group and dataset of an open ``h5py.File``."""
    import h5py

    rows: list[dict] = []

    def visit(name: str, obj: Any) -> None:
        row: dict[str, Any] = {"path": name, "attrs": dict(obj.attrs)}
        if isinstance(obj, h5py.Dataset):
            row["shape"] = tuple(obj.shape)
            row["dtype"] = dtype_name(obj.dtype)
            if obj.size <= SHOW_VALUE_MAX_SIZE and obj.dtype.kind in "iufbSO":
                row["value"] = _plain(obj[()])
        rows.append(row)

    h5.visititems(visit)
    return rows


def _attr(row: dict, key: str) -> str:
    value = _plain(row["attrs"].get(key, ""))
    return " ".join(str(value).split()) if value is not None else ""


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def render(product: str, granule: str, size_bytes: int | None, rows: list[dict]) -> str:
    """Render one product's layout as a Markdown page.

    Rows whose paths fold to the same pattern are merged, keeping the first
    one's shape, type, units and description, and listing every frequency,
    polarization and layer seen.
    """
    folded: dict[str, dict] = {}
    for row in rows:
        if "dtype" not in row:
            continue
        pattern, seen = fold(row["path"])
        entry = folded.setdefault(pattern, {"row": row, "seen": {}})
        for key, value in seen.items():
            entry["seen"].setdefault(key, set()).add(value)

    sections: dict[str, list[str]] = {}
    for pattern, entry in folded.items():
        row = entry["row"]
        prefix = f"science/LSAR/{product}/"
        rel = pattern.removeprefix(prefix).removeprefix("science/LSAR/")
        parts = rel.split("/")
        if len(parts) == 1:
            group = "(top level)"
        elif parts[0] == "metadata" and len(parts) > 2:
            group = "/".join(parts[:2])  # metadata is large: one section per subgroup
        else:
            group = parts[0]
        name = rel
        for key, values in sorted(entry["seen"].items()):
            name = name.replace("{" + key + "}", "{" + ",".join(sorted(values)) + "}")
        value = row.get("value")
        shape = "scalar" if row["shape"] == () else " × ".join(map(str, row["shape"]))
        sections.setdefault(group, []).append(
            "| `{}` | {} | `{}` | {} | {}{} |".format(
                _cell(name),
                shape,
                row["dtype"],
                _cell(_attr(row, "units")),
                _cell(_attr(row, "description")),
                (
                    f" Example: `{_cell(str(value))[:80]}`"
                    if value not in (None, "")
                    else ""
                ),
            )
        )

    size = f", {size_bytes / 1e9:.1f} GB" if size_bytes else ""
    lines = [
        f"# {product} product layout",
        "",
        f"Generated by `scripts/document_product_layout.py` from `{granule}`{size}.",
        "Paths are relative to `science/LSAR/" + product + "/` (or `science/LSAR/`",
        "for `identification`). Rows that repeat per frequency, polarization or",
        "offset layer are folded, with the values seen in braces. Shapes are from",
        "this granule and vary with the frame and mode.",
        "",
    ]
    for group in sorted(sections, key=lambda g: (g != "identification", g)):
        lines += [
            f"## {group}",
            "",
            "| Dataset | Shape | Type | Units | Description |",
            "|---|---|---|---|---|",
            *sorted(sections[group]),
            "",
        ]
    return "\n".join(lines)


def data_url(granule: str, session: Any = None) -> tuple[str, str, int | None]:
    """Return (product type, HTTPS ``.h5`` URL, size) of a granule from CMR.

    The concepts endpoint is used for the record because CMR's UMM-JSON search
    drops and mislabels NISAR granules (see the CMR UMM-JSON drops page).
    """
    import requests

    http = session or requests
    entries = http.get(
        f"{CMR}/granules.json",
        params={"granule_ur": granule, "provider": "ASF"},
        timeout=120,
    ).json()["feed"]["entry"]
    if not entries:
        raise ValueError(f"{granule} is not in CMR")
    umm = http.get(f"{CMR}/concepts/{entries[0]['id']}", timeout=120).json()
    url = next(
        u["URL"]
        for u in umm["RelatedUrls"]
        if u["URL"].startswith("https") and u["URL"].endswith(f"{granule}.h5")
    )
    files = umm.get("DataGranule", {}).get("ArchiveAndDistributionInformation", [])
    size = next(
        (f.get("SizeInBytes") for f in files if f.get("Name") == f"{granule}.h5"), None
    )
    return granule.split("_")[3], url, size


def main(argv: list[str] | None = None) -> None:
    """Command-line entry point."""
    import fsspec
    import h5py

    from qa_browse_server import EarthdataAuth, EarthdataSession, _get

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("granules", nargs="+", help="Granule names (GranuleUR).")
    parser.add_argument("--output-dir", type=Path, default=Path("docs/products"))
    args = parser.parse_args(argv)

    login = EarthdataAuth._netrc_login()
    if login is None:
        parser.error("needs an urs.earthdata.nasa.gov entry in ~/.netrc")
    session = EarthdataSession(login)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for granule in args.granules:
        product, url, size = data_url(granule)
        # Resolve the login redirects once, then range-read the signed URL.
        signed = _get(session, url, headers={"Range": "bytes=0-0"}).url
        with (
            fsspec.filesystem("http").open(
                signed, block_size=4 * 2**20, cache_type="blockcache"
            ) as fo,
            h5py.File(fo) as h5,
        ):
            rows = read_layout(h5)
        page = args.output_dir / f"{product.lower()}.md"
        page.write_text(render(product, granule, size, rows))
        print(f"{product}: {len(rows)} objects -> {page}")


if __name__ == "__main__":
    main()
