"""Unit tests for collecting and attaching per-granule flags in the frame viewer."""

from __future__ import annotations

import gzip
import importlib.util
import json
from pathlib import Path
from types import ModuleType

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
GSLC = (
    "NISAR_L2_PR_GSLC_031_155_D_084_4005_DHDH_A_20260928T231125_20260928T231159"
    "_P05023_N_F_J_001"
)
GUNW = (
    "NISAR_L2_PR_GUNW_030_155_D_084_031_4000_SH_20260916T231125_20260916T231159"
    "_20260928T231125_20260928T231159_P05023_N_F_J_001"
)
FLAGS = {"j": 0, "f": 1, "o": "MOE", "r": 0, "m": 0, "d": 1}


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _frame_data(ifgs_as_string: bool = False) -> dict:
    ifgs = [{"gid": GUNW}, {"gid": "not collected"}]
    return {
        "features": [
            {
                "properties": {
                    "granules": [{"gid": GSLC}],
                    "gunw_ifgs": json.dumps(ifgs) if ifgs_as_string else ifgs,
                }
            }
        ]
    }


def test_attach_granule_flags_marks_only_collected_entries() -> None:
    data = _frame_data(ifgs_as_string=True)

    n = _load("generate_scope_viewer").attach_granule_flags(
        data, {GSLC: FLAGS, GUNW: {**FLAGS, "r": 1}}
    )

    props = data["features"][0]["properties"]
    assert n == 2
    assert props["granules"][0]["fl"] == FLAGS
    assert props["gunw_ifgs"][0]["fl"]["r"] == 1
    assert "fl" not in props["gunw_ifgs"][1]


def test_collector_lists_viewer_granules_and_round_trips_its_cache(
    tmp_path: Path,
) -> None:
    collector = _load("collect_granule_flags")
    html = tmp_path / "viewer.html"
    html.write_text(
        f"const FRAME_DATA = {json.dumps(_frame_data())};\nconst META = {{}};"
    )
    cache = tmp_path / "flags.json.gz"

    ids = collector.viewer_granule_ids(html)
    collector.save_cache(cache, {GUNW: FLAGS, GSLC: FLAGS})

    assert ids == [GSLC, GUNW, "not collected"]
    assert collector.load_cache(cache) == {GSLC: FLAGS, GUNW: FLAGS}
    with gzip.open(cache, "rt") as fh:
        assert list(json.load(fh)) == sorted([GSLC, GUNW])  # sorted for stable diffs
    assert collector.product_url(GUNW).endswith(
        f"NISAR_L2_GUNW_PROVISIONAL_V1/{GUNW}/{GUNW}.h5"
    )
