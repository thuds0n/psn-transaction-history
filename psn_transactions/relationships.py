"""Pure Store relationship and add-on subtype evidence handling."""

import re

from psn_transactions.classify import (
    ADD_ON_DISPLAY_TYPES,
    is_positive_number,
    is_zero_number,
    normalise_token,
)


_ADD_ON_CONTENT_TYPES = {"ADDON", "ADD_ON", "ADD_ON_CONTENT", "DLC"}
_ADD_ON_TOP_CATEGORIES = {"ADD_ON", "ADDONS", "DOWNLOADABLE_CONTENT"}
_SUBTYPE_NAMES = {
    "ADD_ON": "add_on",
    "ADD_ON_PACK": "add_on_pack",
    "AVATAR": "avatar",
    "CHARACTER": "character",
    "COSTUME": "costume",
    "EPISODE": "episode",
    "LEVEL": "level",
    "MAP": "map",
    "SEASON_PASS": "season_pass",
    "SOUNDTRACK": "soundtrack",
    "THEME": "theme",
    "UPGRADE": "upgrade",
    "VIRTUAL_CURRENCY": "virtual_currency",
}
_RELATIONSHIP_CATEGORIES = {"DLC / Add-on", "In-Game Currency"}
# Store records for recurring subscriptions can carry add-on content types,
# but a subscription renewal is never content that belongs to a base game.
_NON_RELATIONSHIP_CATEGORIES = {"Subscription"}
_STORE_RESULT_STATUSES = {"success", "stale_cache"}


def _sku_base(value: str) -> str:
    return re.sub(r"-[A-Z]\d{3}$", "", value.strip())


def product_key(locale: str, product_id: str) -> str:
    """Return a locale-scoped Store product key."""
    if not locale or not product_id:
        return ""
    return f"{locale.lower()}|{_sku_base(product_id)}"


_CONFLICT_PARENTS = re.compile(r"parent_links contains \d+ products: ([^;]+)")
_TITLE_ID = re.compile(r"-([A-Z]{4}\d{5})_\d{2}-")


def conflict_parent_ids(evidence: str) -> list[str]:
    """Return the Store parent product IDs listed in conflict evidence."""
    match = _CONFLICT_PARENTS.search(evidence)
    if not match:
        return []
    return [value.strip() for value in match.group(1).split(",") if value.strip()]


def title_id(key: str) -> str:
    """Return the platform title ID (such as CUSA01234) in a product key."""
    match = _TITLE_ID.search(key)
    return match.group(1) if match else ""


def _token(info: dict, field: str) -> str:
    return normalise_token(str(info.get(field) or ""))


def _tokens(info: dict, field: str) -> list[str]:
    return [
        normalise_token(value)
        for value in str(info.get(field) or "").split(";")
        if value.strip()
    ]


def explicit_add_on_evidence(info: dict) -> str:
    """Return explicit Store evidence that the product is add-on content."""
    content_type = _token(info, "content_type")
    if content_type in _ADD_ON_CONTENT_TYPES:
        return f"content_type={content_type}"

    top_category = _token(info, "top_category")
    if top_category in _ADD_ON_TOP_CATEGORIES:
        return f"top_category={top_category}"

    for secondary in _tokens(info, "secondary_classification"):
        if secondary in _ADD_ON_CONTENT_TYPES:
            return f"secondary_classification={secondary}"

    for display in _tokens(info, "store_display_classification"):
        if display in ADD_ON_DISPLAY_TYPES:
            return f"store_display_classification={display}"
    return ""


def add_on_subtype_fields(info: dict) -> dict[str, str]:
    """Return an evidence-led add-on subtype without product-name inference."""
    for display in _tokens(info, "store_display_classification"):
        if display in _SUBTYPE_NAMES:
            return {
                "add_on_subtype": _SUBTYPE_NAMES[display],
                "add_on_subtype_source": "store_api",
                "add_on_subtype_evidence": (
                    f"store_display_classification={display}"
                ),
            }

    content_type = _token(info, "content_type")
    if content_type in {"CURRENCY", "VC", "INGAME_CURRENCY"}:
        return {
            "add_on_subtype": "virtual_currency",
            "add_on_subtype_source": "store_api",
            "add_on_subtype_evidence": f"content_type={content_type}",
        }

    evidence = explicit_add_on_evidence(info)
    if evidence:
        return {
            "add_on_subtype": "unspecified_add_on",
            "add_on_subtype_source": "store_api",
            "add_on_subtype_evidence": evidence,
        }
    return {
        "add_on_subtype": "",
        "add_on_subtype_source": "",
        "add_on_subtype_evidence": "",
    }


def _parent_candidates(info: dict, locale: str) -> list[dict[str, str]]:
    candidates = []
    for parent in info.get("parent_products") or []:
        if not isinstance(parent, dict):
            continue
        parent_id = parent.get("product_id")
        if not isinstance(parent_id, str) or not parent_id.strip():
            continue
        candidates.append(
            {
                "key": product_key(locale, parent_id),
                "title": str(parent.get("title_name") or parent.get("name") or ""),
                "product_id": _sku_base(parent_id),
            }
        )
    return candidates


def relationship_fields(
    *,
    locale: str,
    sku: str,
    category: str,
    info: dict,
    enrichment_status: str,
) -> dict[str, str]:
    """Resolve only explicit Store relationships; keep ambiguity visible."""
    fields = {
        "product_key": product_key(locale, sku),
        **add_on_subtype_fields(info),
        "relationship_type": "",
        "base_game_key": "",
        "base_game_title": "",
        "relationship_status": "not_applicable",
        "relationship_source": "",
        "relationship_evidence": "",
    }
    if category in _NON_RELATIONSHIP_CATEGORIES:
        fields.update(
            {
                "add_on_subtype": "",
                "add_on_subtype_source": "",
                "add_on_subtype_evidence": "",
            }
        )
        return fields

    add_on_evidence = explicit_add_on_evidence(info)
    is_relationship_product = bool(add_on_evidence) or category in _RELATIONSHIP_CATEGORIES
    parents = _parent_candidates(info, locale)

    if is_relationship_product:
        fields["relationship_type"] = "add_on_of"
        if add_on_evidence and len(parents) == 1:
            parent = parents[0]
            fields.update(
                {
                    "base_game_key": parent["key"],
                    "base_game_title": parent["title"],
                    "relationship_status": "verified",
                    "relationship_source": "store_api",
                    "relationship_evidence": (
                        f"{add_on_evidence}; parent_links product_id="
                        f"{parent['product_id']}"
                    ),
                }
            )
        elif len(parents) > 1:
            parent_ids = ",".join(parent["product_id"] for parent in parents)
            fields.update(
                {
                    "relationship_status": "conflict",
                    "relationship_source": "store_api",
                    "relationship_evidence": (
                        f"{add_on_evidence or 'classification indicates add-on'}; "
                        f"parent_links contains {len(parents)} products: {parent_ids}"
                    ),
                }
            )
        else:
            evidence = "No explicit Store parent relationship"
            source = ""
            if add_on_evidence and enrichment_status in _STORE_RESULT_STATUSES:
                evidence = f"{add_on_evidence}; Store response contains no parent_links"
                source = "store_api"
            elif enrichment_status:
                evidence = f"{evidence}; enrichment_status={enrichment_status}"
            fields.update(
                {
                    "relationship_status": "unresolved",
                    "relationship_source": source,
                    "relationship_evidence": evidence,
                }
            )
        return fields

    if len(parents) == 1:
        parent = parents[0]
        fields.update(
            {
                "relationship_type": "store_parent",
                "base_game_key": parent["key"],
                "base_game_title": parent["title"],
                "relationship_status": "candidate",
                "relationship_source": "store_api",
                "relationship_evidence": (
                    f"parent_links product_id={parent['product_id']}; "
                    "product is not explicitly classified as add-on content"
                ),
            }
        )
    elif len(parents) > 1:
        parent_ids = ",".join(parent["product_id"] for parent in parents)
        fields.update(
            {
                "relationship_type": "store_parent",
                "relationship_status": "conflict",
                "relationship_source": "store_api",
                "relationship_evidence": (
                    f"parent_links contains {len(parents)} products: {parent_ids}; "
                    "product is not explicitly classified as add-on content"
                ),
            }
        )
    return fields


BASE_GAME_OWNER_CATEGORIES = {"Full Game", "Bundle"}
_EDITION_RELATIONSHIP_TYPES = {"store_parent", "edition_of"}


def _title_key(value: str) -> str:
    """Normalise a title for exact matching of a manually entered base game."""
    cleaned = re.sub(r"[\u2122\u00ae\u00a9]", "", value or "")
    return " ".join(cleaned.casefold().split())


def _owned_base_games(rows: list[dict]) -> tuple[dict[str, tuple], dict[str, tuple]]:
    """Return preferred owning purchase rows by product key and by title."""
    owners: dict[str, tuple] = {}
    title_owners: dict[str, tuple] = {}

    def offer(key: str, candidate: tuple, index: dict[str, tuple] = owners) -> None:
        if key and (key not in index or candidate < index[key]):
            index[key] = candidate

    for row in rows:
        item_id = row.get("order_item_id") or ""
        amount = row.get("paid_minor")
        paid = is_positive_number(amount)
        if not item_id or not (paid or is_zero_number(amount)):
            continue
        ownership = "paid" if paid else "free"
        # Ranking: paid before free, exact product before edition, then oldest.
        offer(
            row.get("product_key") or "",
            (not paid, 0, row.get("date") or "", item_id, ownership, "product"),
        )
        if row.get("category") in BASE_GAME_OWNER_CATEGORIES:
            offer(
                _title_key(row.get("product") or ""),
                (not paid, 0, row.get("date") or "", item_id, ownership, "title"),
                title_owners,
            )
        if (
            row.get("relationship_type") in _EDITION_RELATIONSHIP_TYPES
            and row.get("category") in BASE_GAME_OWNER_CATEGORIES
        ):
            offer(
                row.get("base_game_key") or "",
                (not paid, 1, row.get("date") or "", item_id, ownership, "edition"),
            )
    return owners, title_owners


def link_owned_base_games(rows: list[dict]) -> None:
    """Point add-on rows at the purchase row that owns their Store base game.

    A base game is owned when a purchase row has the same product key, or when
    an owned edition or bundle names that base game as its single parent. A
    manual decision that gives only a title matches an owned game or bundle
    with exactly that title; titles never create links on their own.
    """
    owners, title_owners = _owned_base_games(rows)
    for row in rows:
        row["base_game_ownership"] = ""
        row["base_game_match"] = ""
        row["base_game_order_item_id"] = ""
        if row.get("relationship_type") != "add_on_of":
            continue
        key = row.get("base_game_key") or ""
        title = row.get("base_game_title") or ""
        if key:
            owner = owners.get(key)
        elif row.get("relationship_status") == "manual" and title:
            owner = title_owners.get(_title_key(title))
        else:
            continue
        if owner is None:
            row["base_game_ownership"] = "not_in_history"
            continue
        _free, _edition, _date, item_id, ownership, match = owner
        row["base_game_ownership"] = ownership
        row["base_game_match"] = match
        row["base_game_order_item_id"] = item_id


def missing_base_game_keys(rows: list[dict]) -> set[str]:
    """Return base-game keys that no row in ``rows`` currently owns."""
    return {
        row["base_game_key"]
        for row in rows
        if row.get("base_game_ownership") == "not_in_history"
        and row.get("base_game_key")
    }
