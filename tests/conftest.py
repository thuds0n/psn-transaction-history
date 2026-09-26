import pytest

from psn_transactions import overrides


@pytest.fixture(autouse=True)
def isolated_overrides_file(tmp_path, monkeypatch):
    """Keep tests away from the user's private relationship overrides."""
    path = tmp_path / "private" / "relationship_overrides.csv"
    monkeypatch.setattr(overrides, "OVERRIDES_FILE", path)
    return path
