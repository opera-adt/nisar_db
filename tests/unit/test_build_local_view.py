"""Unit tests for building a viewer page for another scope on demand."""

from __future__ import annotations

import contextlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import geopandas as gpd
import pytest
from shapely.geometry import box

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
GUNW = (
    "NISAR_L2_PR_GUNW_030_155_D_084_031_4000_SH_20260916T231125_20260916T231159"
    "_20260928T231125_20260928T231159_P05023_N_F_J_001"
)


def _load(name: str) -> ModuleType:
    # The builder imports the viewer generator the way it does from scripts/.
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _trackframe(tmp_path: Path) -> Path:
    # Three frames: two in California, one over Africa.
    gdf = gpd.GeoDataFrame(
        {
            "track": [1, 2, 3],
            "frame": [10, 11, 12],
            "passDirection": ["Ascending", "Descending", "Ascending"],
            "isCalVal": [False] * 3,
            "isSNWG": [False] * 3,
            "isDNC": [False] * 3,
            "hasLand": [True, False, True],
        },
        geometry=[
            box(-122.5, 36.0, -121.5, 37.0),
            box(-120.5, 38.0, -119.5, 39.0),
            box(10.0, 20.0, 11.0, 21.0),
        ],
        crs=4326,
    )
    path = tmp_path / "tf.gpkg"
    gdf.to_file(path, driver="GPKG")
    return path


def test_select_frames_keeps_the_trackframe_index(tmp_path: Path) -> None:
    builder = _load("build_local_view")
    gpkg = _trackframe(tmp_path)

    globe = builder.select_frames(gpkg, "globe")
    boxed = builder.select_frames(gpkg, "bbox", (-123.0, 35.0, -121.0, 37.5))

    assert list(globe["frame_idx"]) == [0, 1, 2]
    assert list(globe["direction"]) == ["A", "D", "A"]
    # The frame number OPERA uses is the database row, whatever the scope.
    assert list(boxed["frame_idx"]) == [0]
    with pytest.raises(ValueError):
        builder.select_frames(gpkg, "bbox")
    # Land only drops the frame with no land, keeping the others' numbers.
    assert list(builder.select_frames(gpkg, "globe", land_only=True)["frame_idx"]) == [
        0,
        2,
    ]


def test_gunw_rows_parse_the_granule_names() -> None:
    rows = _load("build_local_view").gunw_rows([GUNW, GUNW, "not a gunw"])

    assert len(rows) == 1
    row = rows.iloc[0]
    assert (row["track"], row["frame"], row["direction"]) == (155, 84, "D")
    assert (row["ref"], row["sec"]) == ("2026-09-16", "2026-09-28")
    assert (row["mode"], row["polarization"], row["coverage"]) == ("4000", "SH", "F")


def test_build_requests_are_checked(tmp_path: Path) -> None:
    helper = _load("qa_browse_server")
    builds = helper.ViewBuilds(tmp_path)

    assert builds.status() == {"state": "idle"}
    with pytest.raises(ValueError):
        builds.start("moon", None)
    with pytest.raises(ValueError):
        builds.start("bbox", [10, 0, 5, 1])  # west east of east
    # Only ids the helper writes are served.
    assert builds.page("../secrets") is None


def test_merged_cache_prefers_the_local_cache(tmp_path: Path) -> None:
    import gzip
    import json

    builder = _load("build_local_view")
    repo = tmp_path / "repo.json.gz"
    local = tmp_path / "local.json.gz"
    for path, value in (
        (repo, {"A": {"r": 1}, "B": {"r": 1}}),
        (local, {"B": {"r": 0}}),
    ):
        with gzip.open(path, "wt") as fh:
            json.dump(value, fh)

    assert builder.merged_cache(repo, local) == {"A": {"r": 1}, "B": {"r": 0}}
    assert builder.merged_cache(repo, tmp_path / "missing.json.gz") == {
        "A": {"r": 1},
        "B": {"r": 1},
    }


def test_catalog_flags_come_from_the_cmr_record() -> None:
    attrs = [
        {"Name": "JOINT_OBSERVATION", "Values": ["TRUE"]},
        {"Name": "FULL_FRAME", "Values": ["FALSE"]},
        {"Name": "ORBIT_TYPE", "Values": ["MOE"]},
    ]
    product = SimpleNamespace(name=GUNW, metadata={"AdditionalAttributes": attrs})
    bare = SimpleNamespace(name="other", metadata={})

    flags = _load("build_local_view").catalog_flags([product, bare])

    assert flags == {GUNW: {"j": 1, "f": 0, "o": "MOE"}}


def test_cached_flags_win_over_the_catalog() -> None:
    builder = _load("build_local_view")
    read = GUNW.replace("_001", "_002")
    new_release = GUNW.replace("P05023", "P05030")
    cached = {read: {"j": 0, "f": 1, "o": "POE", "r": 1, "m": 0, "d": 1}}
    from_catalog = {
        GUNW: {"j": 1, "f": 1, "o": "MOE"},
        read: {"j": 1, "f": 1, "o": "MOE"},
        new_release: {"j": 0, "f": 0, "o": "MOE"},
    }

    flags = builder.with_catalog_flags(cached, from_catalog)

    assert flags[read] == cached[read]
    # Never read: the catalog's flags and the release's RFI flag, in key order.
    assert list(flags[GUNW].items()) == [("j", 1), ("f", 1), ("o", "MOE"), ("r", 1)]
    # A release no cached granule belongs to gets no RFI flag.
    assert flags[new_release] == {"j": 0, "f": 0, "o": "MOE"}


def _fake_build(lines: list[str], returncode: int) -> contextlib.nullcontext:
    """Stand in for ``subprocess.Popen`` of build_local_view.py."""
    return contextlib.nullcontext(
        SimpleNamespace(
            stdout=iter(line + "\n" for line in lines), returncode=returncode
        )
    )


def test_build_runs_as_a_child_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _load("qa_browse_server")
    builds = helper.ViewBuilds(tmp_path)
    seen: list[list[str]] = []
    lines = [
        "Selecting frames",
        "Building 3 frames",
        helper.META_LINE + '{"n_frames": 3}',
    ]

    def popen(cmd: list[str], **_kwargs: object) -> contextlib.nullcontext:
        seen.append(cmd)
        return _fake_build(lines, 0)

    monkeypatch.setattr(helper.subprocess, "Popen", popen)
    job = {
        "id": "bbox-1",
        "scope": "bbox",
        "bbox": [1, 2, 3, 4],
        "collect_flags": True,
        "collect_qa": False,
    }
    builds._job = dict(job, state="running")
    builds._run(job)

    status = builds.status()
    assert status["state"] == "done" and status["n_frames"] == 3
    assert status["view"] == "/view/bbox-1"
    assert "--collect-flags" in seen[0] and "--collect-qa" not in seen[0]
    assert "--bbox=1,2,3,4" in seen[0]


def test_a_killed_build_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _load("qa_browse_server")
    builds = helper.ViewBuilds(tmp_path)
    monkeypatch.setattr(
        helper.subprocess,
        "Popen",
        lambda _cmd, **_kw: _fake_build(["flags: 200/900 read"], -9),
    )
    job = {
        "id": "globe-1",
        "scope": "globe",
        "bbox": None,
        "collect_flags": False,
        "collect_qa": False,
    }
    builds._job = dict(job, state="running")
    builds._run(job)

    status = builds.status()
    assert status["state"] == "error"
    assert "killed (signal 9)" in status["step"]


def _saved_catalog(tmp_path: Path, bbox: list[float] | None) -> tuple[ModuleType, Path]:
    import pandas as pd

    builder = _load("build_local_view")
    gslc = pd.DataFrame(
        {
            "track": [1],
            "frame": [10],
            "direction": ["A"],
            "mode": ["4005"],
            "coverage": ["F"],
            "polarization": ["DHDH"],
            "cycle": ["031"],
            "granule_id": ["NISAR_L2_PR_GSLC_X"],
            "start_datetime": ["2026-09-28T23:11:25"],
        }
    )
    gunw = builder.gunw_rows([GUNW])
    directory = tmp_path / "saved"
    builder.save_catalog(
        directory,
        gslc,
        gunw,
        {GUNW: {"o": "MOE"}},
        {"queried_at": "2026-10-21T18:24:08+00:00", "bbox": bbox},
    )
    return builder, directory


def test_saved_catalog_round_trips(tmp_path: Path) -> None:
    builder, directory = _saved_catalog(tmp_path, None)

    gslc, gunw, flags, info = builder.load_catalog(directory, None)

    assert gslc["granule_id"].tolist() == ["NISAR_L2_PR_GSLC_X"]
    assert gslc["mode"].tolist() == ["4005"]  # a code, not a number
    assert gslc["date"].tolist() == ["2026-09-28"]
    assert gunw.to_dict("records") == builder.gunw_rows([GUNW]).to_dict("records")
    assert flags == {GUNW: {"o": "MOE"}}
    assert (info["n_gslc"], info["n_gunw"]) == (1, 1)


def test_box_catalog_only_serves_pages_inside_its_box(tmp_path: Path) -> None:
    builder, directory = _saved_catalog(tmp_path, [-125.0, 32.0, -114.0, 42.0])

    assert builder.load_catalog(directory, (-123.0, 35.0, -120.0, 38.0))[3]["bbox"]
    with pytest.raises(ValueError, match="covers only the box"):
        builder.load_catalog(directory, None)
    with pytest.raises(ValueError, match="covers only the box"):
        builder.load_catalog(directory, (0.0, 0.0, 1.0, 1.0))


def test_builds_use_the_servers_saved_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _load("qa_browse_server")
    builds = helper.ViewBuilds(tmp_path, catalog_dir=tmp_path / "saved")
    seen: list[list[str]] = []

    def popen(cmd: list[str], **_kwargs: object) -> contextlib.nullcontext:
        seen.append(cmd)
        return _fake_build([helper.META_LINE + '{"n_frames": 1}'], 0)

    monkeypatch.setattr(helper.subprocess, "Popen", popen)
    job = {
        "id": "globe-1",
        "scope": "globe",
        "bbox": None,
        "collect_flags": False,
        "collect_qa": False,
    }
    builds._job = dict(job, state="running")
    builds._run(job)

    i = seen[0].index("--catalog")
    assert seen[0][i + 1] == str(tmp_path / "saved")
