"""Private manual classification and base-game decisions.

Decisions live outside the repository in a CSV file that can be edited in a
spreadsheet. They are keyed by locale-scoped product key and always take
precedence over Store evidence, so they survive cache refreshes and re-exports.
"""

import csv
from pathlib import Path

from psn_transactions.errors import PSNTransactionsError
from psn_transactions.paths import app_dir
from psn_transactions.storage import (
    atomic_write_csv,
    secure_private_directory,
    secure_private_file,
)

OVERRIDES_FILE = app_dir() / "relationship_overrides.csv"
OVERRIDE_SOURCE = "manual"
NO_BASE_GAME = "none"

# Reference columns help decide; only the decision columns are applied.
REFERENCE_FIELDS = ["product_key", "product", "current_category", "reason"]
DECISION_FIELDS = ["category", "base_game_key", "base_game_title", "notes"]
OVERRIDE_FIELDS = REFERENCE_FIELDS + DECISION_FIELDS

CATEGORIES = {
    "Bundle",
    "DLC / Add-on",
    "Full Game",
    "In-Game Currency",
    "Other",
    "PS Plus Item",
    "PS Plus Monthly",
    "PS Plus Pack",
    "Pre-order",
    "Promotion",
    "Subscription",
    "Voucher",
}
_ADD_ON_CATEGORIES = {"DLC / Add-on", "In-Game Currency"}

# Rows that still need a human decision when seeding the overrides file.
_CURATION_STATUSES = {"unresolved", "conflict"}


def _is_product_key(value: str) -> bool:
    locale, _, product_id = value.partition("|")
    return bool(locale and product_id)


def _read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    try:
        with path.open(newline="", encoding="utf-8-sig") as file:
            reader = csv.DictReader(file)
            fieldnames = list(reader.fieldnames or [])
            rows = list(reader)
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        raise PSNTransactionsError(
            f"Could not read relationship overrides from {path}: {exc}"
        ) from exc
    if "product_key" not in fieldnames:
        raise PSNTransactionsError(
            f"Relationship overrides at {path} must have a product_key column."
        )
    return fieldnames, rows


def load_overrides(path: Path | None = None) -> dict[str, dict[str, str]]:
    """Return validated manual decisions keyed by product key."""
    path = path or OVERRIDES_FILE
    if not path.exists():
        return {}
    _fieldnames, rows = _read_rows(path)

    overrides: dict[str, dict[str, str]] = {}
    for row_number, row in enumerate(rows, start=2):
        def value(field: str) -> str:
            return (row.get(field) or "").strip()

        key = value("product_key")
        decision = {field: value(field) for field in DECISION_FIELDS}
        if not key:
            if any(decision.values()):
                raise PSNTransactionsError(
                    f"Relationship overrides row {row_number} has a decision "
                    "but no product_key."
                )
            continue
        if not _is_product_key(key):
            raise PSNTransactionsError(
                f"Relationship overrides row {row_number} has an invalid "
                "product_key; copy it from the enriched CSV."
            )
        if key in overrides:
            raise PSNTransactionsError(
                f"Relationship overrides row {row_number} repeats a product_key."
            )
        if decision["category"] and decision["category"] not in CATEGORIES:
            allowed = ", ".join(sorted(CATEGORIES))
            raise PSNTransactionsError(
                f"Relationship overrides row {row_number} has an unknown "
                f"category; use one of: {allowed}."
            )
        base_key = decision["base_game_key"]
        if base_key.lower() == NO_BASE_GAME:
            decision["base_game_key"] = NO_BASE_GAME
            if decision["base_game_title"]:
                raise PSNTransactionsError(
                    f"Relationship overrides row {row_number} sets base_game_key "
                    "to none and also gives a base_game_title."
                )
        elif base_key and not _is_product_key(base_key):
            raise PSNTransactionsError(
                f"Relationship overrides row {row_number} has an invalid "
                "base_game_key; use a product_key from the enriched CSV or none."
            )
        if not any(
            decision[field]
            for field in ("category", "base_game_key", "base_game_title")
        ):
            continue
        overrides[key] = decision
    return overrides


def apply_classification_override(
    override: dict[str, str] | None,
    category: str,
    source: str,
    evidence: str,
) -> tuple[str, str, str]:
    """Return category, source and evidence after any manual category."""
    if override and override["category"]:
        return override["category"], OVERRIDE_SOURCE, "relationship_overrides.csv"
    return category, source, evidence


def apply_relationship_override(
    fields: dict[str, str],
    override: dict[str, str] | None,
    category: str,
) -> None:
    """Replace Store relationship fields with a manual base-game decision."""
    if not override:
        return
    base_key = override["base_game_key"]
    base_title = override["base_game_title"]
    evidence = override["notes"] or "relationship_overrides.csv"

    if base_key == NO_BASE_GAME:
        fields.update(
            {
                "relationship_type": "",
                "base_game_key": "",
                "base_game_title": "",
                "relationship_status": "not_applicable",
                "relationship_source": OVERRIDE_SOURCE,
                "relationship_evidence": evidence,
            }
        )
        return
    if not base_key and not base_title:
        return

    if not base_title and base_key == fields.get("base_game_key"):
        base_title = fields.get("base_game_title", "")
    fields.update(
        {
            "relationship_type": (
                "add_on_of" if category in _ADD_ON_CATEGORIES else "edition_of"
            ),
            "base_game_key": base_key,
            "base_game_title": base_title,
            "relationship_status": "manual",
            "relationship_source": OVERRIDE_SOURCE,
            "relationship_evidence": evidence,
        }
    )


def _curation_reason(row: dict[str, str]) -> str:
    status = row.get("relationship_status", "")
    if status in _CURATION_STATUSES:
        evidence = row.get("relationship_evidence", "")
        return f"{status}: {evidence}" if evidence else status
    if row.get("category") == "Other":
        return f"unclassified: enrichment_status={row.get('enrichment_status', '')}"
    return ""


def seed_overrides(
    enriched_csv: Path,
    path: Path | None = None,
) -> tuple[int, int]:
    """Add products needing a decision; return added and already-listed counts."""
    path = path or OVERRIDES_FILE
    try:
        with enriched_csv.open(newline="", encoding="utf-8-sig") as file:
            reader = csv.DictReader(file)
            if "product_key" not in (reader.fieldnames or []):
                raise PSNTransactionsError(
                    f"{enriched_csv} has no product_key column; regenerate it "
                    "with psn-transactions enrich."
                )
            enriched_rows = list(reader)
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        raise PSNTransactionsError(
            f"Could not read enriched CSV {enriched_csv}: {exc}"
        ) from exc

    fieldnames = list(OVERRIDE_FIELDS)
    existing_rows: list[dict[str, str]] = []
    if path.exists():
        existing_fields, existing_rows = _read_rows(path)
        fieldnames += [field for field in existing_fields if field not in fieldnames]
        load_overrides(path)  # Refuse to extend a file that does not validate.
    listed = {(row.get("product_key") or "").strip() for row in existing_rows}

    added: list[dict[str, str]] = []
    already_listed: set[str] = set()
    for row in enriched_rows:
        key = row.get("product_key", "")
        reason = _curation_reason(row)
        if not key or not reason:
            continue
        if key in listed:
            if not any(item["product_key"] == key for item in added):
                already_listed.add(key)
            continue
        listed.add(key)
        added.append(
            {
                "product_key": key,
                "product": row.get("product", ""),
                "current_category": row.get("category", ""),
                "reason": reason,
            }
        )

    if added or not path.exists():
        secure_private_directory(path.parent, "relationship overrides")
        atomic_write_csv(
            path,
            existing_rows + added,
            fieldnames,
            description="relationship overrides",
        )
        secure_private_file(path, "relationship overrides")
    return len(added), len(already_listed)
