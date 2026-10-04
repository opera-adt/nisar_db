"""Unit tests for collecting and attaching per-granule QA metrics in the viewer."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np
import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
GSLC = (
    "NISAR_L2_PR_GSLC_031_155_D_084_4005_DHDH_A_20260928T231125_20260928T231159"
    "_P05023_N_F_J_001"
)
GUNW = (
    "NISAR_L2_PR_GUNW_030_155_D_084_031_4000_SH_20260916T231125_20260916T231159"
    "_20260928T231125_20260928T231159_P05023_N_F_J_001"
)


def _load(name: str) -> ModuleType:
    # The QA collector imports its HTTP helpers from the flag collector, the way
    # it does when run from scripts/.
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _h5py() -> Any:
    # Only the QA readers need h5py, which the CI test environment lacks.
    return pytest.importorskip("h5py")


def _histogram(group: Any, edges: np.ndarray, density: np.ndarray) -> None:
    group["histogramBins"] = edges
    group["histogramDensity"] = density


def test_hist_stats_reads_the_median_inside_its_bin() -> None:
    qa = _load("collect_granule_qa")
    edges = np.linspace(0.0, 1.0, 11)
    density = np.zeros(10)
    density[[2, 7]] = [1.0, 3.0]  # a quarter of the weight at 0.25, the rest at 0.75

    stats = qa.hist_stats(edges, density)

    assert stats["mean"] == pytest.approx(0.625)
    assert stats["median"] == pytest.approx(0.7 + 0.1 / 3)
    assert stats["std"] == pytest.approx(np.sqrt(0.25 * 0.375**2 + 0.75 * 0.125**2))
    assert qa.hist_stats(edges, np.zeros(10)) is None


def test_gunw_metrics_scale_coverage_to_the_data_area(tmp_path: Path) -> None:
    h5py = _h5py()
    qa = _load("collect_granule_qa")
    path = tmp_path / "qa.h5"
    with h5py.File(path, "w") as h5:
        grp = h5.create_group(
            "science/LSAR/QA/data/frequencyA/unwrappedInterferogram/HH"
        )
        grp["unwrappedPhase/percentNan"] = 60.0
        cc = grp.create_group("connectedComponents")
        cc["numValidConnectedComponents"] = 3
        cc["percentPixelsWithNonZeroCC"] = 30.0
        cc["percentPixelsInLargestCC"] = 20.0
        edges = np.linspace(0.0, 1.0, 5)
        _histogram(
            grp.create_group("coherenceMagnitude"), edges, np.array([0, 1, 1, 0.0])
        )
        # The QA software's own mean of this layer reads zero for some granules;
        # the collector must not use it.
        grp["coherenceMagnitude/mean_value"] = 0.0
        iono = np.linspace(-10.0, 10.0, 5)
        _histogram(
            grp.create_group("ionospherePhaseScreen"), iono, np.array([0, 0, 1, 0.0])
        )
        _histogram(
            grp.create_group("ionospherePhaseScreenUncertainty"),
            np.linspace(0.0, 4.0, 5),
            np.array([0, 1, 0, 0.0]),
        )
        out = qa.gunw_metrics(h5)

    assert out["n"] == 3
    assert out["v"] == 75.0  # 30 % of the raster over a 40 % data area
    assert out["l"] == 50.0
    assert out["cm"] == pytest.approx(0.5)
    assert out["ca"] == pytest.approx(0.5)
    assert out["im"] == pytest.approx(2.5)
    assert out["is"] == pytest.approx(0.0)
    assert out["iu"] == pytest.approx(1.5)


def test_gslc_metrics_take_the_worst_rfi_likelihood(tmp_path: Path) -> None:
    h5py = _h5py()
    qa = _load("collect_granule_qa")
    with h5py.File(tmp_path / "qa.h5", "w") as h5:
        h5["science/LSAR/RFI/data/frequencyA/HH/rfiLikelihood"] = 0.2
        h5["science/LSAR/RFI/data/frequencyA/HV/rfiLikelihood"] = np.nan
        h5["science/LSAR/RFI/data/frequencyB/VV/rfiLikelihood"] = 1.25
        assert qa.gslc_metrics(h5) == {"rl": 1.25}
    with h5py.File(tmp_path / "empty.h5", "w") as h5:
        assert qa.gslc_metrics(h5) == {}


def test_qa_url_points_next_to_the_product() -> None:
    url = _load("collect_granule_qa").qa_url(GUNW)
    assert url.endswith(f"NISAR_L2_GUNW_PROVISIONAL_V1/{GUNW}/{GUNW}_QA_STATS.h5")


def test_attach_granule_qa_skips_withdrawn_granules() -> None:
    data = {
        "features": [
            {
                "properties": {
                    "granules": [{"gid": GSLC}],
                    "gunw_ifgs": [{"gid": GUNW}],
                }
            }
        ]
    }

    n = _load("generate_scope_viewer").attach_granule_qa(
        data, {GSLC: {}, GUNW: {"cm": 0.4, "n": 1}}
    )

    props = data["features"][0]["properties"]
    assert n == 1
    assert "qa" not in props["granules"][0]
    assert props["gunw_ifgs"][0]["qa"] == {"cm": 0.4, "n": 1}


def _qa_report(path: Path) -> None:
    # A page laid out like the QA report's wrapped-group page: its title, two
    # rasters drawn with imshow (which matplotlib stores bottom row first) and a
    # colourbar too small to be a layer.
    matplotlib = pytest.importorskip("matplotlib")
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # Dark in the north, bright in the south; enough levels that the raster is
    # stored as 8-bit, like the QA report's.
    phase = np.tile(np.linspace(0.0, 1.0, 120)[:, None], (1, 130))
    fig, axes = plt.subplots(1, 2)
    fig.suptitle("Wrapped Phase Image Group")
    im = axes[0].imshow(phase, cmap="gray", interpolation="none")
    axes[1].imshow(phase.T, cmap="gray", interpolation="none")
    fig.colorbar(im, ax=axes[1])
    fig.savefig(path, dpi=300)
    plt.close(fig)


def test_extract_layers_keeps_the_rasters_north_up(tmp_path: Path) -> None:
    pytest.importorskip("pypdf")
    pdf = tmp_path / "report.pdf"
    _qa_report(pdf)

    layers = _load("qa_browse_server").extract_layers(pdf.read_bytes())

    assert sorted(layers) == ["coherence_wrapped", "wrapped"]
    wrapped = layers["wrapped"].convert("L")
    w, h = wrapped.size
    assert (w, h) == (130, 120)
    assert wrapped.getpixel((w // 2, 2)) < 50
    assert wrapped.getpixel((w // 2, h - 3)) > 200


def test_earthdata_session_sends_the_login_to_earthdata_only() -> None:
    import requests

    helper = _load("qa_browse_server")
    session = helper.EarthdataSession(("user", "secret"))

    def hop(url: str) -> requests.PreparedRequest:
        # A redirect from the ASF download host to ``url``.
        prepared = requests.Request("GET", url).prepare()
        response = requests.Response()
        response.request = requests.Request(
            "GET", "https://nisar.asf.earthdatacloud.nasa.gov/NISAR/x.pdf"
        ).prepare()
        session.rebuild_auth(prepared, response)
        return prepared

    assert (
        hop("https://urs.earthdata.nasa.gov/oauth/authorize")
        .headers["Authorization"]
        .startswith("Basic ")
    )
    assert "Authorization" not in hop("https://d1.cloudfront.net/x.pdf").headers


def test_earthdata_auth_prefers_the_page_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _load("qa_browse_server")
    rc = tmp_path / "netrc"
    rc.write_text("machine urs.earthdata.nasa.gov login me password pw\n")
    rc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(rc))
    auth = helper.EarthdataAuth()
    assert auth.source == "netrc"
    assert auth.session().login == ("me", "pw")

    # A login checked with Earthdata wins until logout.
    ok = type("R", (), {"status_code": 200, "raise_for_status": lambda _: None})
    monkeypatch.setattr(helper.requests, "get", lambda *a, **k: ok())
    auth.login("other", "pw2")
    assert auth.source == "page"
    assert auth.session().login == ("other", "pw2")
    auth.logout()
    assert auth.source == "netrc"

    monkeypatch.setenv("NETRC", str(tmp_path / "missing"))
    assert auth.source == "none"
    with pytest.raises(PermissionError):
        auth.session()


def test_earthdata_auth_rejects_a_wrong_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = _load("qa_browse_server")
    monkeypatch.setattr(
        helper.requests, "get", lambda *a, **k: type("R", (), {"status_code": 401})()
    )
    auth = helper.EarthdataAuth()
    with pytest.raises(PermissionError):
        auth.login("me", "wrong")
    assert auth._page is None


def test_viewer_page_is_marked_as_served_by_the_helper(tmp_path: Path) -> None:
    helper = _load("qa_browse_server")
    page = tmp_path / "viewer.html"
    page.write_text("<!DOCTYPE html>\n<html><head>\n<title>v</title></head></html>")

    html = helper.ViewerPage(str(page)).html().decode()

    assert html.startswith(f"<!DOCTYPE html>\n<html><head>{helper.SAME_ORIGIN_MARK}")


def test_viewer_page_follows_a_changed_file(tmp_path: Path) -> None:
    import os

    helper = _load("qa_browse_server")
    page = tmp_path / "viewer.html"
    page.write_text("<html><head></head>one</html>")
    viewer = helper.ViewerPage(str(page))
    assert b"one" in viewer.html()

    page.write_text("<html><head></head>two</html>")
    os.utime(page, (1, 1))  # a different modification time, whatever the clock
    assert b"two" in viewer.html()
