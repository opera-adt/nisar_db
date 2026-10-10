"""Viewer links: the API documents exactly the parameters the page reads."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from nisar_db.api.viewer import VIEWER_PARAMS

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "generate_scope_viewer.py"


def _app_js() -> str:
    spec = importlib.util.spec_from_file_location("generate_scope_viewer", SCRIPT)
    assert spec is not None and spec.loader is not None
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    return gen.APP_JS


def _url_state_names(js: str) -> set[str]:
    body = js[
        js.index("function applyUrlState(") : js.index(
            'map.on("load", ()=> setTimeout(applyUrlState'
        )
    ]
    names = set(re.findall(r'(?:has|truthy|q\.has|q\.get)\("([a-z_]+)"\)', body))
    # Filter inputs are set from a [element id, parameter] table.
    names |= set(re.findall(r'\["f-[a-z-]+", "([a-z_]+)"\]', body))
    names |= set(re.findall(r'chips\("([a-z_]+)"', body))
    return names


def test_documented_parameters_match_the_page():
    assert _url_state_names(_app_js()) == set(VIEWER_PARAMS)
