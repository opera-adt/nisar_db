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
let showQa = false;
let qaPairColor = "";
const META = {};
let operaOn = true;
// The plots size themselves to the chart card on the page; with no page here
// they get the card's default size.
function chartWidth(){ return Math.min(720, Math.max(420, window.innerWidth - 140)); }
function chartRoom(){ return 0; }
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


def test_plot_drops_blackout_windows_with_the_opera_switch_off() -> None:
    result = run_js(
        CHART,
        "const p = {has_blackout: true, blackout_label: 'Sep-May', blackout_ranges:"
        " JSON.stringify(['2025-09-28 -> 2026-05-26'])};"
        " operaOn = false;"
        " const svg = modeTimelineSvg(GRANULES, p);"
        " return {bands: svg.includes('chart-blackout'),"
        " legend: svg.includes('blackout (')};",
    )
    assert result == {"bands": False, "legend": False}


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
        ["toCsv", "qaValue", "qaCsvCols", "gunwCsv"],
        f"const IFGS = {json.dumps(IFGS)};"
        " return gunwCsv({frame_idx: 7, track: 1, frame: 4,"
        " passDirection: 'Ascending'}, IFGS);",
    )
    lines = csv.strip().splitlines()
    assert lines[0].startswith('"frame_id","track","frame","pass","ref_date"')
    assert len(lines) == 4
    assert lines[1].split(",")[4:7] == ['"2025-11-01"', '"2025-11-13"', '"12"']


def test_gunw_count_follows_the_gunw_chips() -> None:
    counts = run_js(
        ["inCycles", "selectedGunwCount"],
        f"const ifgs = {json.dumps(IFGS)}; const none = new Set();"
        " return [selectedGunwCount(ifgs, none, none, '', ''),"
        " selectedGunwCount(ifgs, none, new Set(['HV']), '', ''),"
        " selectedGunwCount(ifgs, new Set(['4000']), none, '', '')];",
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
            "hasLand": idx != 14,
            "gslc_modes": ["2005"],
            "gslc_pols": ["DHDH"],
        }
        return {"properties": props}

    shown = run_js(
        [
            "asArray",
            "parseIntSet",
            "cycleFilter",
            "matchesArrayFilter",
            "hasColorValue",
            "currentFiltered",
        ],
        "const state = {'f-track': {value: ''}, 'f-frame': {value: '14-15'},"
        " 'f-cycle': {value: ''},"
        " 'f-id': {value: ''}, 'f-calval': {checked: false},"
        " 'f-land': {checked: false}, 'f-selected-only': {checked: false}};"
        " globalThis.document = {getElementById: id => state[id],"
        " querySelector: () => ({value: 'all'})};"
        " globalThis.product = 'gslc'; globalThis.hideEmpty = false;"
        " globalThis.activeChips = {gslcMode: new Set(), gslcPol: new Set(),"
        " gslcCrid: new Set()};"
        " globalThis.selected = new Map([['47_15', {}], ['47_16', {}]]);"
        " globalThis.activeRollout = new Set();"
        " const ids = () => currentFiltered().map(f => f.properties.id);"
        " const all = ids(); state['f-selected-only'].checked = true;"
        " const only = ids(); selected.clear(); const none = ids();"
        " state['f-selected-only'].checked = false; state['f-land'].checked = true;"
        " return [all, only, none, ids()];",
        [frame(14), frame(15), frame(16)],
    )
    # the flags narrow the other filters rather than replacing them; frame 14
    # has no land
    assert shown == [["47_14", "47_15"], ["47_15"], [], ["47_15"]]


def test_rollout_filter_matches_any_option_and_none() -> None:
    def frame(idx: int, rollout: list[str]) -> dict:
        props = {
            "id": f"47_{idx}",
            "frame_idx": idx,
            "track": 47,
            "frame": idx,
            "passDirection": "Ascending",
            "isCalVal": False,
            "gslc_modes": ["2005"],
            "gslc_pols": ["DHDH"],
            "rollout": rollout,
        }
        return {"properties": props}

    shown = run_js(
        [
            "asArray",
            "parseIntSet",
            "cycleFilter",
            "matchesArrayFilter",
            "hasColorValue",
            "currentFiltered",
        ],
        "const state = {'f-track': {value: ''}, 'f-frame': {value: ''},"
        " 'f-cycle': {value: ''},"
        " 'f-id': {value: ''}, 'f-calval': {checked: false},"
        " 'f-land': {checked: false}, 'f-selected-only': {checked: false}};"
        " globalThis.document = {getElementById: id => state[id],"
        " querySelector: () => ({value: 'all'})};"
        " globalThis.product = 'gslc'; globalThis.hideEmpty = false;"
        " globalThis.activeChips = {gslcMode: new Set(), gslcPol: new Set(),"
        " gslcCrid: new Set()};"
        " globalThis.selected = new Map();"
        " globalThis.activeRollout = new Set(['P1']);"
        " const ids = o => currentFiltered(o).map(f => f.properties.frame_idx);"
        " const p1 = ids(); activeRollout.add('none'); const p1none = ids();"
        " return [p1, p1none, ids({ignoreRollout: true})];",
        [frame(1, ["P0", "P1"]), frame(2, ["P4"]), frame(3, [])],
    )
    assert shown == [[1], [1, 3], [1, 2, 3]]


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
        ["hasFlag", "flagStatus"],
        f"const items = {items};"
        " return [flagStatus(items, 'f'), flagStatus(items, 'm'),"
        " flagStatus(items, 'j'), flagStatus(items, 'o'),"
        " flagStatus(items.slice(0, 1), 'o'),"
        " flagStatus(items.slice(2), 'f')];",
    )
    assert result == ["all", "some", "none", "mixed", "MOE", "not collected"]


def test_flag_status_skips_flags_not_read_yet() -> None:
    # A granule known only from the catalog carries j f o r, not m d.
    items = json.dumps(
        [
            {"fl": {"j": 1, "f": 1, "o": "MOE", "r": 0, "m": 1, "d": 1}},
            {"fl": {"j": 0, "f": 1, "o": "MOE", "r": 0}},
        ]
    )
    result = run_js(
        ["hasFlag", "flagStatus"],
        f"const items = {items};"
        " return [flagStatus(items, 'm'), flagStatus(items, 'j'),"
        " flagStatus(items.slice(1), 'd')];",
    )
    assert result == ["all", "some", "not collected"]


def test_has_color_value_treats_none_unread_and_no_acquisitions_as_empty() -> None:
    frames = json.dumps(
        [
            {
                "cons_mode": "4005",
                "_flag_m": "some",
                "gslc_count_sel": 3,
                "_qa": 0.4,
                "n_duplicate_sel": 0,
            },
            {
                "cons_mode": "none",
                "_flag_m": "not collected",
                "gslc_count_sel": 0,
                "_qa": None,
                "n_duplicate_sel": 0,
            },
        ]
    )
    result = run_js(
        ["hasColorValue"],
        "globalThis.COLOR_BY_FIELDS = {"
        " cons_mode: {key: 'cons_mode', kind: 'cat'},"
        " flag_m: {key: '_flag_m', kind: 'cat'},"
        " gslc_count: {key: 'gslc_count_sel', kind: 'num'},"
        " qa: {key: '_qa', kind: 'num'},"
        " n_duplicate: {key: 'n_duplicate_sel', kind: 'num'}};"
        " globalThis.EMPTY_CATS = new Set(['none', 'not collected', 'no GUNW',"
        " 'undefined', 'null', '']);"
        " globalThis.ZERO_IS_EMPTY = new Set(['gslc_count', 'gunw_count', 'n_modes']);"
        f" const frames = {frames};"
        " return ['cons_mode', 'flag_m', 'gslc_count', 'qa', 'n_duplicate']"
        "   .map(k => frames.map(p => hasColorValue(p, k)));",
    )
    # zero duplicates is a value; zero acquisitions, none, not collected and an
    # unread QA metric are not
    assert result == [
        [True, False],
        [True, False],
        [True, False],
        [True, False],
        [True, True],
    ]


def test_selection_counts_follow_chips_and_date_range() -> None:
    rows = json.dumps(
        [
            [g["mode"], g["pol"], g["date"], f"{g['date']}|{g['mode']}|{g['cov']}"]
            for g in GRANULES
        ]
    )
    result = run_js(
        ["selectedGslcStats"],
        f"const rows = {rows}; const none = new Set();"
        " return [selectedGslcStats(rows, none, none, '', ''),"
        " selectedGslcStats(rows, new Set(['20']), none, '', ''),"
        " selectedGslcStats(rows, none, none, '2026-01-01', ''),"
        " selectedGslcStats(rows, none, none, '', '2025-12-31')];",
    )
    assert result == [
        {"acq": 2, "dup": 1, "modes": 2},
        {"acq": 1, "dup": 1, "modes": 1},
        {"acq": 1, "dup": 0, "modes": 1},
        {"acq": 1, "dup": 1, "modes": 1},
    ]


def test_gunw_count_filters_on_the_secondary_date() -> None:
    ifgs = json.dumps(
        [
            {"mode": "20", "pol": "HH", "ref": "2025-10-01", "sec": "2025-11-01"},
            {"mode": "20", "pol": "HH", "ref": "2025-11-01", "sec": "2026-02-01"},
        ]
    )
    result = run_js(
        ["inCycles", "selectedGunwCount"],
        f"const ifgs = {ifgs}; const none = new Set();"
        " return [selectedGunwCount(ifgs, none, none, '', ''),"
        " selectedGunwCount(ifgs, none, none, '2025-11-15', ''),"
        " selectedGunwCount(ifgs, new Set(['40']), none, '', '')];",
    )
    assert result == [2, 1, 0]


def test_blackout_month_shares_walk_windows_across_the_new_year() -> None:
    wrapping = ["2025-11-01 -> 2026-03-15", "2026-11-01 -> 2027-03-15"]
    short = ["2025-01-12 -> 2025-02-21"]
    result = run_js(
        ["blackoutMonthShares"],
        f"const r = s => s.map(x => Math.round(x * 100));"
        f" return [r(blackoutMonthShares({json.dumps(wrapping)})),"
        f" r(blackoutMonthShares({json.dumps(short)})), blackoutMonthShares([])];",
    )
    # Nov-Feb whole, half of March, nothing in between.
    assert result[0] == [100, 100, 48, 0, 0, 0, 0, 0, 0, 0, 100, 100]
    # 20 of January's 31 days, 21 of February's 28.
    assert result[1] == [65, 75, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
    assert result[2] == [0] * 12


def test_crid_is_read_from_the_granule_name_and_filters_counts() -> None:
    gslc = (
        "NISAR_L2_PR_GSLC_024_001_D_052_0505_SVSH_A_20260626T063101"
        "_20260626T063139_P05023_N_F_J_001"
    )
    gunw = (
        "NISAR_L2_PR_GUNW_024_001_D_061_025_2000_SH_20260626T063622_20260626T063654"
        "_20260708T063621_20260708T063653_P05024_N_F_J_001"
    )
    rows = json.dumps(
        [
            ["20", "HH", "2025-11-01", "a", "P05023"],
            ["20", "HH", "2025-11-13", "b", "P05024"],
        ]
    )
    result = run_js(
        ["cridOf", "selectedGslcStats"],
        f"const rows = {rows}; const none = new Set();"
        f" return [cridOf('{gslc}', 13), cridOf('{gunw}', 15), cridOf('short', 13),"
        " selectedGslcStats(rows, none, none, '', '', new Set(['P05024'])).acq,"
        " selectedGslcStats(rows, none, none, '', '', none).acq];",
    )
    assert result == ["P05023", "P05024", "", 1, 2]


# ``run_js`` wraps the body in a function, so the constants the extracted
# functions read are set on the global object.
QA_PRELUDE = (
    "globalThis.QA_FIELDS = {cm: {dir: -1, thr: 0.3}, n: {dir: 1, thr: 1},"
    " im: {dir: 0, thr: 20}, rl: {dir: 1, thr: 0.5}};"
    " globalThis.QA_GUNW = ['cm', 'n', 'rl'];"
)


def test_gunw_csv_adds_the_qa_columns() -> None:
    ifgs = [dict(IFGS[0], qa={"cm": 0.43, "n": 2}, rl=0.1), IFGS[1]]
    csv = run_js(
        ["toCsv", "qaValue", "qaCsvCols", "gunwCsv"],
        QA_PRELUDE + f" META.has_qa = true; const IFGS = {json.dumps(ifgs)};"
        " return gunwCsv({frame_idx: 7, track: 1, frame: 4,"
        " passDirection: 'Ascending'}, IFGS);",
    )
    lines = csv.strip().splitlines()
    assert lines[0].endswith('"granule_id","qa_cm","qa_n","qa_rl"')
    assert lines[1].split(",")[-3:] == ['"0.43"', '"2"', '"0.1"']
    # A pair whose QA was not read leaves its cells empty.
    assert lines[2].split(",")[-3:] == ['""', '""', '""']


def test_qa_statistic_follows_the_bad_direction() -> None:
    out = run_js(
        ["quantile", "qaIsBad", "qaAggregate"],
        QA_PRELUDE + " const coh = [0.1, 0.2, 0.5, 0.6, 0.9];"
        " const iono = [-30, -5, 2, 25];"
        " return [qaAggregate('cm', coh, 'median'), qaAggregate('cm', coh, 'worst'),"
        " qaAggregate('cm', coh, 'bad', 0.3), qaAggregate('n', [1, 1, 2, 4], 'worst'),"
        " qaAggregate('im', iono, 'bad', 20), qaAggregate('cm', [], 'median')];",
    )
    median, worst, bad, worst_n, iono_bad, empty = out
    assert median == 0.5
    # Low coherence is bad, so its worst is the 10th percentile ...
    assert abs(worst - 0.14) < 1e-9
    assert bad == 40
    # ... while more connected components is worse, so theirs is the 90th.
    assert abs(worst_n - 3.4) < 1e-9
    # The ionosphere mean is judged by its size, either sign.
    assert iono_bad == 50
    assert empty is None


def test_pair_rfi_is_the_worse_of_its_two_acquisitions() -> None:
    props = {
        "granules": [
            {"date": "2025-11-01", "qa": {"rl": 0.2}},
            {"date": "2025-11-13", "qa": {"rl": 0.7}},
            {"date": "2025-11-13", "qa": {"rl": 0.4}},
        ],
        "gunw_ifgs": [
            {"ref": "2025-11-01", "sec": "2025-11-13"},
            {"ref": "2025-11-01", "sec": "2025-11-25"},
            {"ref": "2025-10-20", "sec": "2025-11-25"},
        ],
    }
    out = run_js(
        ["asArray", "attachPairRfi"],
        f"const p = {json.dumps(props)}; attachPairRfi(p);"
        " return p.gunw_ifgs.map(g=>g.rl ?? null);",
    )
    assert out == [0.7, 0.2, None]


def test_cycle_filter_narrows_the_counts() -> None:
    rows = [
        ["4005", "DHDH", "2025-11-01", "a", "P05023", 23],
        ["4005", "DHDH", "2025-11-13", "b", "P05023", 24],
        ["4005", "DHDH", "2025-11-25", "c", "P05023", 25],
    ]
    ifgs = [
        {"mode": "4000", "pol": "SH", "sec": "2025-11-13", "cyc": [23, 24]},
        {"mode": "4000", "pol": "SH", "sec": "2025-11-25", "cyc": [24, 25]},
        {"mode": "4000", "pol": "SH", "sec": "2025-12-07", "cyc": [25, 26]},
    ]
    out = run_js(
        ["inCycles", "selectedGslcStats", "selectedGunwCount"],
        f"const rows = {json.dumps(rows)}; const ifgs = {json.dumps(ifgs)};"
        " const none = new Set(); const c = new Set([24]);"
        " return [selectedGslcStats(rows, none, none, '', '', none, c).acq,"
        " selectedGslcStats(rows, none, none, '', '', none, null).acq,"
        " selectedGunwCount(ifgs, none, none, '', '', none, c),"
        " selectedGunwCount(ifgs, none, none, '', '', none, new Set([26]))];",
    )
    # A pair matches on either acquisition's cycle.
    assert out == [1, 3, 2, 1]


def test_plate_label_sits_inside_the_plate_and_ignores_cuts() -> None:
    plates = {
        "features": [
            {
                "properties": {"PlateName": "Square"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[10, -5], [20, -5], [20, 5], [10, 5], [10, -5]]],
                },
            },
            {
                # A plate cut at the antimeridian: the cut edge must not pull
                # the label towards 180.
                "properties": {"PlateName": "Cut"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [[170, 0], [180, 0], [180, 10], [170, 10], [170, 0]]
                    ],
                },
            },
        ]
    }
    out = run_js(
        ["plateLabelPoints"],
        f"return plateLabelPoints({json.dumps(plates)}).features"
        ".map(f => [f.properties.name, ...f.geometry.coordinates]);",
    )
    by_name = {name: (lon, lat) for name, lon, lat in out}
    lon, lat = by_name["Square"]
    assert abs(lon - 15) < 0.5 and abs(lat) < 0.5
    lon, _ = by_name["Cut"]
    assert lon < 176


def _frame(lon: float, lat: float) -> dict:
    ring = [[lon, lat], [lon + 1, lat], [lon + 1, lat + 1], [lon, lat + 1], [lon, lat]]
    return {"geometry": {"type": "Polygon", "coordinates": [ring]}}


def test_page_area_reaches_the_frames_south_of_north_america() -> None:
    """Regression: a fixed North America box (south edge 14N) hid the M7.7 off
    Panama (7.6N) from the earthquake layer of a page whose frames reach 16S."""
    frames = [_frame(-81, 7), _frame(-60, 70), _frame(179.2, 51)]
    area = run_js(["pageArea"], "return pageArea();", frames)
    # The Aleutian frame is carried west of -180, so the box stays one piece.
    assert area == [-182.8, 5, -57, 73]


def test_page_area_prefers_the_views_own_box() -> None:
    area = run_js(
        ["pageArea"],
        "META.view_bbox = [1, 2, 3, 4]; return pageArea();",
        [_frame(0, 0)],
    )
    assert area == [1, 2, 3, 4]


def test_page_area_wrapping_the_world_asks_for_everything() -> None:
    frames = [_frame(x, 0) for x in range(-180, 180, 30)]
    assert run_js(["pageArea"], "return pageArea();", frames) is None


def test_in_area_wraps_round_the_antimeridian() -> None:
    result = run_js(
        ["inArea"],
        "const a = [-182.8, 5, -57, 73];"
        " return [inArea(179.5, 52, a), inArea(-80.8, 7.6, a),"
        " inArea(150, 52, a), inArea(-80, 80, a)];",
    )
    assert result == [True, True, False, False]


MCP_TOOL = {
    "name": "find_frames",
    "description": "Find frames.",
    "inputSchema": {"type": "object", "properties": {"cycle": {"type": "string"}}},
}


def test_ai_tools_take_each_providers_shape() -> None:
    result = run_js(
        ["aiToolsFor"],
        f"const t = [{json.dumps(MCP_TOOL)}];"
        " return [aiToolsFor('anthropic', t)[0], aiToolsFor('openai', t)[0]];",
    )
    claude, openai = result
    assert claude == {
        "name": "find_frames",
        "description": "Find frames.",
        "input_schema": MCP_TOOL["inputSchema"],
    }
    assert openai == {
        "type": "function",
        "function": {
            "name": "find_frames",
            "description": "Find frames.",
            "parameters": MCP_TOOL["inputSchema"],
        },
    }


def test_ai_result_text_is_capped() -> None:
    result = run_js(
        ["aiResultText"],
        "globalThis.AI_RESULT_CHARS = 10;"
        " return [aiResultText({a: 1}), aiResultText('x'.repeat(25))];",
    )
    assert result == ['{"a":1}', "xxxxxxxxxx... [truncated, 25 characters]"]


def test_ai_view_query_is_what_apply_url_state_reads() -> None:
    query = run_js(
        ["aiViewQuery"],
        "return aiViewQuery({product: 'gunw', opera: false, gps: true,"
        " cycle: '20-25', zoom: 3, color: null, track: ''}).toString();",
    )
    assert query == "product=gunw&opera=0&gps=1&cycle=20-25&zoom=3"


def test_ai_markdown_escapes_html_and_links_only_http() -> None:
    html = run_js(
        ["aiMarkdown"],
        "return aiMarkdown('**955** frames <script>x</script> `c`\\n"
        "[map](https://x.org/v?a=1) [bad](javascript:alert(1))');",
    )
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<b>955</b>" in html and "<code>c</code>" in html
    assert (
        '<a href="https://x.org/v?a=1" target="_blank" rel="noopener">map</a>' in html
    )
    assert 'href="javascript' not in html


def test_ai_finds_frames_by_index_or_track_frame() -> None:
    frames = [{"properties": {"id": "34_19", "frame_idx": 5826}}]
    result = run_js(
        ["aiFindFrame"],
        "return ['5826', '34_19', 'T34_F19', 't34_f19', '1_1']"
        ".map(k => (aiFindFrame(k) || {properties: {}}).properties.frame_idx || null);",
        frames,
    )
    assert result == [5826, 5826, 5826, 5826, None]
