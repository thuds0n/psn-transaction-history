"""Application data paths, including the legacy-name compatibility fallback."""

from pathlib import Path

APP_DIR = Path.home() / ".psn-transactions"
LEGACY_APP_DIR = Path.home() / ".psn-receipts"


def app_dir() -> Path:
    """Use existing legacy data until the user performs the one-time rename."""
    if APP_DIR.exists() or not LEGACY_APP_DIR.exists():
        return APP_DIR
    return LEGACY_APP_DIR
