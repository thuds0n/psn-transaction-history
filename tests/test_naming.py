"""Regression tests for the public naming contract."""

import inspect
import tomllib
from pathlib import Path

from psn_transactions import export, fetch, paths


PROJECT_ROOT = Path(__file__).parents[1]


def test_distribution_and_cli_names_match_contract():
    metadata = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    readme_heading = (PROJECT_ROOT / "README.md").read_text().splitlines()[0]

    assert readme_heading == "# PSN Transaction History"
    assert metadata["project"]["name"] == "psn-transactions"
    assert metadata["project"]["scripts"] == {
        "psn-transactions": "psn_transactions.cli:app"
    }


def test_fetch_and_export_defaults_match_contract():
    fetch_parameters = inspect.signature(fetch.fetch_all).parameters
    export_parameters = inspect.signature(export.export_csv).parameters
    enrich_parameters = inspect.signature(export.enrich_csv).parameters

    assert fetch_parameters["output_path"].default == "psn_transactions_raw.json"
    assert export_parameters["json_path"].default == "psn_transactions_raw.json"
    assert export_parameters["csv_path"].default == "psn_transactions.csv"
    assert enrich_parameters["json_path"].default == "psn_transactions_raw.json"
    assert enrich_parameters["csv_path"].default == "psn_transactions_enriched.csv"


def test_app_directory_matches_contract(tmp_path, monkeypatch):
    application_directory = tmp_path / ".psn-transactions"
    monkeypatch.setattr(paths, "APP_DIR", application_directory)

    assert paths.app_dir() == application_directory
