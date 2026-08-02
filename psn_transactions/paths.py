"""Application data paths."""

from pathlib import Path

APP_DIR = Path.home() / ".psn-transactions"


def app_dir() -> Path:
    """Return the application data directory."""
    return APP_DIR
