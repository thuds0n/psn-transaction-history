"""Private manual classification and base-game decisions.

Decisions live outside the repository in a CSV file that can be edited in a
spreadsheet. They are keyed by locale-scoped product key and always take
precedence over Store evidence, so they survive cache refreshes and re-exports.
"""

import csv
from pathlib import Path

from psn_transactions.errors import PSNTransactionsError
from psn_transactions.paths import app_dir
from psn_transactions.relationships import (
    BASE_GAME_OWNER_CATEGORIES,
    GAME_CATEGORIES,
    NON_RELATIONSHIP_CATEGORIES,
    clean_game_title,
    conflict_parent_ids,
    loose_game_key,
    title_id,
)
from psn_transactions.storage import (
    atomic_write_csv,
    secure_private_directory,
    secure_private_file,
)

OVERRIDES_FILE = app_dir() / "relationship_overrides.csv"
OVERRIDE_SOURCE = "manual"
NO_BASE_GAME = "none"
# Evidence prefix for a base game given where one can never apply.
IGNORED_BASE_GAME = "base game ignored"

# Reference columns help decide; only the decision columns are applied.
REFERENCE_FIELDS = [
    "product_key",
    "product",
    "current_category",
    "reason",
    "candidates",
]
DECISION_FIELDS = ["category", "base_game_key", "base_game_title", "game", "notes"]
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

_DECIDING_FIELDS = ("category", "base_game_key", "base_game_title", "game")

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
        if not any(decision[field] for field in _DECIDING_FIELDS):
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
) -> bool:
    """Replace Store relationship fields with a manual base-game decision.

    Returns True when a base game was given for a category that never links
    to one, such as a subscription, so the caller can report it.
    """
    if not override:
        return False
    base_key = override["base_game_key"]
    base_title = override["base_game_title"]
    evidence = override["notes"] or "relationship_overrides.csv"

    if category in NON_RELATIONSHIP_CATEGORIES and (
        base_title or (base_key and base_key != NO_BASE_GAME)
    ):
        fields.update(
            {
                "relationship_source": OVERRIDE_SOURCE,
                "relationship_evidence": (
                    f"{IGNORED_BASE_GAME}: {category} products never link to "
                    "a base game; set game instead"
                ),
            }
        )
        return True
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
        return False
    if not base_key and not base_title:
        return False

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
    return False


def _curation_reason(row: dict[str, str]) -> str:
    status = row.get("relationship_status", "")
    if status in _CURATION_STATUSES:
        evidence = row.get("relationship_evidence", "")
        return f"{status}: {evidence}" if evidence else status
    if row.get("category") == "Other":
        return f"unclassified: enrichment_status={row.get('enrichment_status', '')}"
    return ""


# Conflicts list their Store parents, so they are quickest to decide; products
# the Store no longer describes need the most outside research.
_REASON_ORDER = {"conflict": 0, "unresolved": 1, "duplicate": 2, "unclassified": 3}


def _sort_key(row: dict[str, str]) -> tuple:
    decided = any((row.get(field) or "").strip() for field in _DECIDING_FIELDS)
    kind = (row.get("reason") or "").partition(":")[0]
    return (
        decided,
        _REASON_ORDER.get(kind, len(_REASON_ORDER)),
        (row.get("product") or "").casefold(),
        row.get("product_key") or "",
    )


def _candidates(
    row: dict[str, str],
    titles: dict[str, str],
    owners_by_title_id: dict[str, list[str]],
) -> str:
    """Describe possible base games: Store parents, then owned same-title games.

    Same-title suggestions are for add-ons and unclassified products; a full
    game or bundle sharing a title ID is an edition, offered as a game merge.
    """
    key = row.get("product_key", "")
    locale = key.partition("|")[0]
    described: dict[str, str] = {}
    for parent_id in conflict_parent_ids(row.get("relationship_evidence", "")):
        parent_key = f"{locale}|{parent_id}"
        owned = titles.get(parent_key)
        described[parent_key] = (
            f"Store parent, owned: {owned}" if owned else "Store parent, not in history"
        )
    if row.get("category") in BASE_GAME_OWNER_CATEGORIES:
        return "; ".join(f"{candidate} ({note})" for candidate, note in described.items())
    for owner_key in owners_by_title_id.get(title_id(key), []):
        if owner_key != key and owner_key not in described:
            described[owner_key] = f"same title ID, owned: {titles[owner_key]}"
    return "; ".join(f"{candidate} ({note})" for candidate, note in described.items())


def _possible_duplicates(enriched_rows: list[dict[str, str]]) -> dict[str, list[str]]:
    """Return, per product key, owned games that may be the same game.

    Games match when their names agree once edition wording such as
    "Remastered" or "Director's Cut" is ignored but they are still counted as
    different games, so a ``game`` decision would merge their totals.
    """
    groups: dict[str, dict[str, set[str]]] = {}
    for row in enriched_rows:
        key = row.get("product_key", "")
        if not key or row.get("category") not in GAME_CATEGORIES:
            continue
        game = row.get("game") or clean_game_title(row.get("product", ""))
        groups.setdefault(loose_game_key(game), {}).setdefault(game, set()).add(key)

    duplicates: dict[str, list[str]] = {}
    for loose_key, games in groups.items():
        if not loose_key or len(games) < 2:
            continue
        for game, keys in games.items():
            others = sorted(other for other in games if other != game)
            for key in keys:
                duplicates[key] = others
    return duplicates


def seed_overrides(
    enriched_csv: Path,
    path: Path | None = None,
) -> tuple[int, int]:
    """Add products needing a decision; return added and already-listed counts.

    Reference columns of undecided products are refreshed from the enriched
    CSV, and the file is sorted with undecided products first, grouped by
    reason. Decision columns are never changed.
    """
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
    existing_fields: list[str] = []
    existing_rows: list[dict[str, str]] = []
    if path.exists():
        existing_fields, existing_rows = _read_rows(path)
        fieldnames += [field for field in existing_fields if field not in fieldnames]
        load_overrides(path)  # Refuse to extend a file that does not validate.
    original_rows = [dict(row) for row in existing_rows]
    rows_by_key = {
        (row.get("product_key") or "").strip(): row for row in existing_rows
    }

    titles: dict[str, str] = {}
    owners_by_title_id: dict[str, list[str]] = {}
    for row in enriched_rows:
        key = row.get("product_key", "")
        if not key or key in titles:
            continue
        titles[key] = row.get("product", "")
        if row.get("category") in BASE_GAME_OWNER_CATEGORIES and title_id(key):
            owners_by_title_id.setdefault(title_id(key), []).append(key)

    duplicates = _possible_duplicates(enriched_rows)

    added = 0
    already_listed: set[str] = set()
    seen: set[str] = set()
    for row in enriched_rows:
        key = row.get("product_key", "")
        others = duplicates.get(key, [])
        reason = _curation_reason(row) or (
            "duplicate: may be the same game as " + "; ".join(others)
            if others
            else ""
        )
        if not key or not reason or key in seen:
            continue
        seen.add(key)
        candidates = _candidates(row, titles, owners_by_title_id)
        if others:
            games = "; ".join(f"game: {other}" for other in others)
            candidates = f"{candidates}; {games}" if candidates else games
        reference = {
            "product_key": key,
            "product": row.get("product", ""),
            "current_category": row.get("category", ""),
            "reason": reason,
            "candidates": candidates,
        }
        if key in rows_by_key:
            already_listed.add(key)
            rows_by_key[key].update(reference)
        else:
            added += 1
            rows_by_key[key] = reference

    rows = sorted(rows_by_key.values(), key=_sort_key)
    if rows != original_rows or fieldnames != existing_fields:
        secure_private_directory(path.parent, "relationship overrides")
        atomic_write_csv(
            path,
            rows,
            fieldnames,
            description="relationship overrides",
        )
        secure_private_file(path, "relationship overrides")
    return added, len(already_listed)
