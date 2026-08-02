"""Regression tests for the public naming contract."""

import inspect
import tomllib
from pathlib import Path

from psn_transactions import parse, paths


PROJECT_ROOT = Path(__file__).parents[1]


def test_distribution_and_cli_names_match_contract():
    metadata = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    readme_heading = (PROJECT_ROOT / "README.md").read_text().splitlines()[0]

    assert readme_heading == "# PSN Transaction History"
    assert metadata["project"]["name"] == "psn-transactions"
    assert metadata["project"]["scripts"] == {
        "psn-transactions": "psn_transactions.cli:app"
    }


def test_export_defaults_match_contract():
    parameters = inspect.signature(parse.export).parameters

    assert parameters["json_path"].default == "psn_transactions.json"
    assert parameters["csv_path"].default == "psn_transactions.csv"


def test_app_directory_falls_back_to_legacy_name(tmp_path, monkeypatch):
    current_directory = tmp_path / ".psn-transactions"
    legacy_directory = tmp_path / ".psn-receipts"
    legacy_directory.mkdir()

    monkeypatch.setattr(paths, "APP_DIR", current_directory)
    monkeypatch.setattr(paths, "LEGACY_APP_DIR", legacy_directory)

    assert paths.app_dir() == legacy_directory


def test_app_directory_prefers_current_name(tmp_path, monkeypatch):
    current_directory = tmp_path / ".psn-transactions"
    legacy_directory = tmp_path / ".psn-receipts"
    current_directory.mkdir()
    legacy_directory.mkdir()

    monkeypatch.setattr(paths, "APP_DIR", current_directory)
    monkeypatch.setattr(paths, "LEGACY_APP_DIR", legacy_directory)

    assert paths.app_dir() == current_directory
