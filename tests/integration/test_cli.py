"""Integration tests for the click CLI wiring (no heavy work triggered)."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from nisar_db.cli import cli_app

_SUBCOMMANDS = [
    "append-blackout-dates",
    "build-s3-catalog",
    "create-blackout-dates",
    "create-consistent",
    "create-frame-to-bound",
    "create-gslc-csv",
    "create-nisar-catalog",
    "download",
    "download-frame-db",
    "query-catalog",
    "search",
]


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_top_level_help_lists_all_subcommands(runner: CliRunner) -> None:
    result = runner.invoke(cli_app, ["--help"])
    assert result.exit_code == 0
    for name in _SUBCOMMANDS:
        assert name in result.output


@pytest.mark.parametrize("name", _SUBCOMMANDS)
def test_subcommand_help_exits_zero(runner: CliRunner, name: str) -> None:
    result = runner.invoke(cli_app, [name, "--help"])
    assert result.exit_code == 0, result.output
    assert result.output


def test_deprecated_alias_still_resolves_but_is_hidden(runner: CliRunner) -> None:
    """``create-catalog`` keeps working after the rename to ``create-gslc-csv``."""
    result = runner.invoke(cli_app, ["create-catalog", "--help"])
    assert result.exit_code == 0, result.output

    top_level = runner.invoke(cli_app, ["--help"])
    assert "create-catalog" not in top_level.output


def test_deprecated_alias_warns(runner: CliRunner, tmp_path) -> None:
    file_list = tmp_path / "gslc_files.txt"
    file_list.write_text("")
    result = runner.invoke(
        cli_app,
        [
            "create-catalog",
            "--input",
            str(file_list),
            "--output",
            str(tmp_path / "o.csv"),
        ],
    )
    assert "deprecated" in result.output
    assert "create-gslc-csv" in result.output


def test_query_catalog_requires_existing_path(runner: CliRunner) -> None:
    result = runner.invoke(cli_app, ["query-catalog", "/no/such/catalog.csv"])
    assert result.exit_code != 0
