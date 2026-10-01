"""Behavioural tests for the frame viewer's JavaScript, run under node.

The viewer is a single template string, so each test lifts the functions it
needs out of ``APP_JS`` and runs them on a few fake granules. Skipped where
node is not installed.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "generate_scope_viewer.py"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

PRELUDE = """
const DAY_MS = 86400000;
const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun",
                "Jul","Aug","Sep","Oct","Nov","Dec"];
const CHART_PALETTE = ["#111111", "#222222"];
const DIR_LABEL = {A: "Ascending", D: "Descending"};
const window = {innerWidth: 900};
let chartPoints = [];
let modeKeyOrder = null;
let archiveSpan = null;
let showFlags = false;
const META = {};
"""

GRANULES = [
    {
        "date": "2025-11-01",
        "mode": "20",
        "cov": "F",
        "pol": "HH",
        "cycle": 1,
        "dir": "A",
        "gid": "g1",
    },
    {
        "date": "2025-11-01",
        "mode": "20",
        "cov": "F",
        "pol": "HH",
        "cycle": 1,
        "dir": "A",
        "gid": "g2",
    },
    {
        "date": "2026-08-01",
        "mode": "40",
        "cov": "F",
        "pol": "HH",
        "cycle": 9,
        "dir": "D",
        "gid": "g3",
    },
]


def _app_js() -> str:
    spec = importlib.util.spec_from_file_location("generate_scope_viewer", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module.APP_JS)


def _function(js: str, name: str) -> str:
    match = re.search(rf"\n  function {name}\(.*?\n  \}}\n", js, re.S)
    assert match, f"function {name} not found in APP_JS"
    return match.group(0)


def run_js(names: list[str], body: str, frames: list | None = None) -> Any:
    """Run ``body`` after defining ``names`` from APP_JS; return its printed JSON."""
    js = _app_js()
    consts = "".join(
        m.group(0) + "\n" for m in re.finditer(r"  const DUP_ROW = [^\n]*;", js)
    )
    source = (
        PRELUDE
        + consts
        + f"const FRAME_DATA = {json.dumps({'features': frames or []})};\n"
        + f"const GRANULES = {json.dumps(GRANULES)};\n"
        + "".join(_function(js, n) for n in names)
        + f"console.log(JSON.stringify((() => {{ {body} }})()));\n"
    )
    assert NODE is not None
    out = subprocess.run(
        [NODE, "-e", source], capture_output=True, text=True, check=False
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


CHART = [
    "uniqSorted",
    "asArray",
    "modeKeys",
    "modeColor",
    "timeTicks",
    "blackoutWindowsOf",
    "archiveBounds",
    "spanWithBlackouts",
    "blackoutBands",
    "modeTimelineSvg",
]
LANES = (
    "const lanes = [...svg.matchAll(/chart-row-label[^>]*>([^<]*)/g)]"
    ".map(m => m[1]);"
)


def test_duplicate_groups_share_date_mode_and_coverage() -> None:
    groups = run_js(
        ["duplicateGroups"],
        "return duplicateGroups(GRANULES).map(gs => gs.map(g => g.gid));",
    )
    assert groups == [["g1", "g2"]]


def test_duplicate_list_names_every_granule_in_a_group() -> None:
    html = run_js(
        ["duplicateGroups", "duplicateRowsHtml"],
        "return duplicateRowsHtml(duplicateGroups(GRANULES));",
    )
    assert "2 granules" in html
    assert "g1" in html and "g2" in html and "g3" not in html


def test_plot_adds_a_duplicates_row_only_when_there_are_duplicates() -> None:
    result = run_js(
        CHART,
        "const svg = modeTimelineSvg(GRANULES); "
        + LANES
        + " const dups = chartPoints.filter(p => p.group)"
        + ".map(p => p.group.map(g => g.gid));"
        + " const svg2 = modeTimelineSvg(GRANULES.slice(2));"
        + " return {lanes, dups, single: svg2.includes('>duplicates<')};",
    )
    assert result["lanes"] == ["20_F", "40_F", "duplicates"]
    assert result["dups"] == [["g1", "g2"]]
    assert result["single"] is False


def test_search_reads_lat_lon_in_either_order() -> None:
    result = run_js(
        ["parseCoords"],
        "return [parseCoords('34.2 -118.2'), parseCoords('-118.2, 34.2'),"
        " parseCoords('Los Angeles')];",
    )
    assert result == [{"lat": 34.2, "lon": -118.2}, {"lat": 34.2, "lon": -118.2}, None]


def test_search_matches_frames_by_id_or_track_and_frame() -> None:
    frames = [
        {"properties": {"frame_idx": 8109, "track": 47, "frame": 14}},
        {"properties": {"frame_idx": 8110, "track": 47, "frame": 15}},
        {"properties": {"frame_idx": 19776, "track": 113, "frame": 65}},
    ]
    result = run_js(
        ["matchFrames"],
        "const ids = q => matchFrames(q).map(f => f.properties.frame_idx);"
        " return [ids('81'), ids('T113_F65'), ids('t47 f15'), ids('Denver')];",
        frames,
    )
    assert result == [[8109, 8110], [19776], [8110], []]


def test_plot_shades_blackout_windows_clipped_to_the_plotted_span() -> None:
    result = run_js(
        CHART,
        "const p = {has_blackout: true, blackout_label: 'Sep-May', blackout_ranges:"
        " JSON.stringify(['2025-09-28 -> 2026-05-26', '2027-09-28 -> 2028-05-26'])};"
        " const svg = modeTimelineSvg(GRANULES, p);"
        " const none = modeTimelineSvg(GRANULES, {has_blackout: false});"
        ' return {bands: (svg.match(/class="chart-blackout"/g) || []).length,'
        " legend: svg.includes('blackout (Sep-May)'),"
        " none: none.includes('chart-blackout')};",
    )
    assert result == {"bands": 1, "legend": True, "none": False}


IFGS = [
    {
        "ref": "2025-11-01",
        "sec": "2025-11-13",
        "dt": 12,
        "mode": "2000",
        "cov": "F",
        "pol": "HH",
        "gid": "i1",
    },
    {
        "ref": "2025-11-01",
        "sec": "2025-11-13",
        "dt": 12,
        "mode": "2000",
        "cov": "F",
        "pol": "HV",
        "gid": "i2",
    },
    {
        "ref": "2025-11-13",
        "sec": "2025-12-07",
        "dt": 24,
        "mode": "2000",
        "cov": "F",
        "pol": "HH",
        "gid": "i3",
    },
]


def test_gunw_plot_draws_one_segment_per_pair_over_blackouts() -> None:
    result = run_js(
        [
            "uniqSorted",
            "asArray",
            "timeTicks",
            "blackoutWindowsOf",
            "archiveBounds",
            "spanWithBlackouts",
            "blackoutBands",
            "gunwNetwork",
            "gunwPlotSvg",
        ],
        f"const IFGS = {json.dumps(IFGS)};"
        " const p = {has_blackout: true, blackout_label: 'Nov-Nov',"
        " blackout_ranges: ['2025-11-20 -> 2025-11-30']};"
        " const svg = gunwPlotSvg(IFGS, p);"
        ' return {segments: (svg.match(/class="chart-ifg"/g) || []).length,'
        ' bands: (svg.match(/class="chart-blackout"/g) || []).length,'
        " pols: chartPoints.map(pt => pt.group.map(g => g.pol))};",
    )
    assert result == {"segments": 2, "bands": 1, "pols": [["HH", "HV"], ["HH"]]}


def test_gunw_csv_has_one_row_per_granule() -> None:
    csv = run_js(
        ["toCsv", "gunwCsv"],
        f"const IFGS = {json.dumps(IFGS)};"
        " return gunwCsv({frame_idx: 7, track: 1, frame: 4,"
        " passDirection: 'Ascending'}, IFGS);",
    )
    lines = csv.strip().splitlines()
    assert lines[0].startswith('"frame_id","track","frame","pass","ref_date"')
    assert len(lines) == 4
    assert lines[1].split(",")[4:7] == ['"2025-11-01"', '"2025-11-13"', '"12"']


def test_gunw_count_follows_the_gunw_chips() -> None:
    frames = [{"properties": {"gunw_ifgs": IFGS}}]
    counts = run_js(
        ["asArray", "updateSelectedGunwCounts"],
        "globalThis.activeChips = {gunwMode: new Set(), gunwPol: new Set()};"
        " const n = () => (updateSelectedGunwCounts(),"
        " FRAME_DATA.features[0].properties.gunw_count_sel);"
        " const all = n(); activeChips.gunwPol.add('HV'); const hv = n();"
        " activeChips.gunwPol.clear(); activeChips.gunwMode.add('4000');"
        " return [all, hv, n()];",
        frames,
    )
    assert counts == [3, 1, 0]


def test_plot_widens_to_a_blackout_before_the_first_acquisition() -> None:
    # The frame was only imaged in summer; its winter blackout falls inside the
    # archive, so the plot must reach back far enough to show it.
    frames = [
        {"properties": {"granules": [{"date": "2025-10-01"}, {"date": "2026-09-01"}]}}
    ]
    summer = [
        dict(g, date=d)
        for g, d in zip(GRANULES, ["2026-06-20", "2026-07-02", "2026-08-01"])
    ]
    result = run_js(
        ["parseGranules", *CHART],
        f"const SUMMER = {json.dumps(summer)};"
        " const p = {has_blackout: true, blackout_label: 'Nov-Apr', blackout_ranges:"
        " ['2025-11-12 -> 2026-04-22', '2026-11-12 -> 2027-04-22']};"
        " const svg = modeTimelineSvg(SUMMER, p);"
        ' return {bands: (svg.match(/class="chart-blackout"/g) || []).length,'
        ' x: Number(svg.match(/class="chart-blackout" x="([0-9.]+)/)[1])};',
        frames,
    )
    assert result["bands"] == 1  # the 2026-27 window lies past the archive
    assert result["x"] < 200  # the band sits at the left, before the data


GUNW_CHART = [
    "uniqSorted",
    "asArray",
    "timeTicks",
    "blackoutWindowsOf",
    "archiveBounds",
    "spanWithBlackouts",
    "blackoutBands",
    "gunwNetwork",
    "gunwPlotSvg",
]


def _pair(ref: str, sec: str) -> str:
    return f"{{ta: Date.parse('{ref}T00:00:00Z'), tb: Date.parse('{sec}T00:00:00Z')}}"


@pytest.mark.parametrize(
    ("pairs", "expected"),
    [
        # a daisy chain: one piece, no gap
        ([("2025-11-01", "2025-11-13"), ("2025-11-13", "2025-11-25")], [1, 0]),
        # a missing link: two pieces split by a gap no pair bridges
        ([("2025-11-01", "2025-11-13"), ("2025-11-25", "2025-12-07")], [2, 1]),
        # a long pair spans the missing link, so the chain holds
        (
            [
                ("2025-11-01", "2025-11-13"),
                ("2025-11-25", "2025-12-07"),
                ("2025-11-13", "2025-11-25"),
            ],
            [1, 0],
        ),
        # two chains that interleave in time: two pieces but no gap to shade
        ([("2025-11-01", "2025-11-25"), ("2025-11-13", "2025-12-07")], [2, 0]),
    ],
)
def test_gunw_network_finds_pieces_and_gaps(pairs: list, expected: list) -> None:
    js_pairs = "[" + ", ".join(_pair(a, b) for a, b in pairs) + "]"
    result = run_js(
        ["gunwNetwork"],
        f"const net = gunwNetwork({js_pairs});"
        " return [net.components, net.breaks.length];",
    )
    assert result == expected


def test_gunw_plot_marks_the_gap_and_the_cut_off_pairs() -> None:
    def ifg(ref: str, sec: str) -> dict:
        return {
            "ref": ref,
            "sec": sec,
            "dt": 12,
            "mode": "4000",
            "cov": "F",
            "pol": "SH",
            "gid": ref,
        }

    ifgs = [
        ifg("2025-11-01", "2025-11-13"),
        ifg("2025-11-25", "2025-12-07"),
        ifg("2025-12-07", "2025-12-19"),
    ]
    result = run_js(
        GUNW_CHART,
        f"const svg = gunwPlotSvg({json.dumps(ifgs)}, {{}});"
        ' return {gaps: (svg.match(/class="chart-gap"/g) || []).length,'
        ' halos: (svg.match(/class="chart-ifg-off"/g) || []).length};',
    )
    # the lone first pair is the cut-off piece; the two-pair chain is the main one
    assert result == {"gaps": 1, "halos": 1}


def test_selected_only_flag_keeps_just_the_selected_frames() -> None:
    def frame(idx: int) -> dict:
        props = {
            "id": f"47_{idx}",
            "frame_idx": idx,
            "track": 47,
            "frame": idx,
            "passDirection": "Ascending",
            "isCalVal": False,
            "gslc_modes": ["2005"],
            "gslc_pols": ["DHDH"],
        }
        return {"properties": props}

    shown = run_js(
        ["parseIntSet", "matchesArrayFilter", "currentFiltered"],
        "const state = {'f-track': {value: ''}, 'f-frame': {value: '14-15'},"
        " 'f-id': {value: ''}, 'f-calval': {checked: false},"
        " 'f-selected-only': {checked: false}};"
        " globalThis.document = {getElementById: id => state[id],"
        " querySelector: () => ({value: 'all'})};"
        " globalThis.product = 'gslc';"
        " globalThis.activeChips = {gslcMode: new Set(), gslcPol: new Set()};"
        " globalThis.selected = new Map([['47_15', {}], ['47_16', {}]]);"
        " const ids = () => currentFiltered().map(f => f.properties.id);"
        " const all = ids(); state['f-selected-only'].checked = true;"
        " const only = ids(); selected.clear(); return [all, only, ids()];",
        [frame(14), frame(15), frame(16)],
    )
    # the flag narrows the other filters rather than replacing them
    assert shown == [["47_14", "47_15"], ["47_15"], []]


def test_gunw_network_status_names_connected_and_disconnected_frames() -> None:
    def ifg(ref: str, sec: str) -> dict:
        return {"ref": ref, "sec": sec}

    chain = [ifg("2025-11-01", "2025-11-13"), ifg("2025-11-13", "2025-11-25")]
    split = [ifg("2025-11-01", "2025-11-13"), ifg("2025-12-07", "2025-12-19")]
    result = run_js(
        ["gunwNetwork", "gunwNetworkStatus"],
        f"return [gunwNetworkStatus({json.dumps(chain)}),"
        f" gunwNetworkStatus({json.dumps(split)}), gunwNetworkStatus([])];",
    )
    assert result == ["connected", "disconnected", "no GUNW"]


def test_flag_status_reads_all_some_none_and_orbit() -> None:
    def g(**fl: object) -> dict:
        return {"fl": {"j": 0, "f": 1, "o": "MOE", "r": 0, "m": 0, "d": 1, **fl}}

    items = json.dumps([g(), g(m=1, o="POE"), {"gid": "no flags yet"}])
    result = run_js(
        ["flagStatus"],
        f"const items = {items};"
        " return [flagStatus(items, 'f'), flagStatus(items, 'm'),"
        " flagStatus(items, 'j'), flagStatus(items, 'o'),"
        " flagStatus(items.slice(0, 1), 'o'),"
        " flagStatus(items.slice(2), 'f')];",
    )
    assert result == ["all", "some", "none", "mixed", "MOE", "not collected"]
