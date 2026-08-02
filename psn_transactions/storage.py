"""Safe local storage helpers for credentials and export files."""

import csv
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from psn_transactions.errors import PSNTransactionsError


def secure_private_directory(directory: Path, description: str) -> None:
    if directory.is_symlink():
        raise PSNTransactionsError(
            f"Refusing to store {description} in symlinked directory {directory}."
        )
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
    except OSError as exc:
        raise PSNTransactionsError(
            f"Could not secure the {description} directory {directory}: {exc}"
        ) from exc


def secure_private_file(file_path: Path, description: str) -> None:
    secure_private_directory(file_path.parent, description)
    if file_path.is_symlink() or not file_path.is_file():
        raise PSNTransactionsError(
            f"{description.capitalize()} {file_path} must be a regular file, "
            "not a symlink or directory."
        )
    try:
        file_path.chmod(0o600)
    except OSError as exc:
        raise PSNTransactionsError(
            f"Could not restrict access to the {description} {file_path}: {exc}"
        ) from exc


def secure_auth_directory(auth_directory: Path) -> None:
    if auth_directory.is_symlink():
        raise PSNTransactionsError(
            f"Refusing to store authentication data in symlinked directory {auth_directory}."
        )
    try:
        auth_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        auth_directory.chmod(0o700)
    except OSError as exc:
        raise PSNTransactionsError(
            f"Could not secure the authentication directory {auth_directory}: {exc}"
        ) from exc


def secure_auth_file(auth_file: Path) -> None:
    secure_auth_directory(auth_file.parent)
    if auth_file.is_symlink() or not auth_file.is_file():
        raise PSNTransactionsError(
            f"Saved session {auth_file} must be a regular file, not a symlink or directory."
        )
    try:
        auth_file.chmod(0o600)
    except OSError as exc:
        raise PSNTransactionsError(
            f"Could not restrict access to the saved session {auth_file}: {exc}"
        ) from exc


def atomic_write_json(
    output_path: Path, payload: Any, description: str = "transaction JSON"
) -> None:
    """Replace a JSON file only after its complete contents reach disk."""
    temporary_path = None
    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            dir=output_path.parent,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary_file:
            json.dump(payload, temporary_file, indent=2)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, output_path)
    except (OSError, TypeError, ValueError) as exc:
        raise PSNTransactionsError(
            f"Could not save {description} to {output_path}: {exc}"
        ) from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def atomic_write_csv(
    output_path: Path,
    rows: list[dict],
    fieldnames: list[str],
    description: str = "CSV export",
) -> None:
    """Replace a CSV file only after its complete contents reach disk."""
    temporary_path = None
    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output_path.name}.",
            suffix=".tmp",
            dir=output_path.parent,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(
            file_descriptor, "w", newline="", encoding="utf-8"
        ) as temporary_file:
            writer = csv.DictWriter(temporary_file, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, output_path)
    except (OSError, csv.Error, ValueError) as exc:
        raise PSNTransactionsError(
            f"Could not save {description} to {output_path}: {exc}"
        ) from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
