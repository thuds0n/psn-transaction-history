"""Pure transaction-item classification rules."""

import re
from typing import Any


ADD_ON_DISPLAY_TYPES = frozenset(
    {
        "ADD_ON",
        "ADD_ON_PACK",
        "AVATAR",
        "CHARACTER",
        "COSTUME",
        "EPISODE",
        "LEVEL",
        "MAP",
        "SEASON_PASS",
        "SOUNDTRACK",
        "THEME",
        "UPGRADE",
        "VIRTUAL_CURRENCY",
    }
)
_ADD_ON_SECONDARY_TYPES = {"ADDON", "ADD_ON", "ADD_ON_CONTENT", "DLC"}
_FULL_GAME_DISPLAY_TYPES = {"FULL_GAME", "PREMIUM_EDITION"}


def normalise_token(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", value.upper()).strip("_")


def _store_tokens(info: dict, field: str) -> list[str]:
    return [
        token
        for value in str(info.get(field) or "").split(";")
        if (token := normalise_token(value))
    ]


def _store_classification(info: dict) -> tuple[str, str] | None:
    """Classify from the Store's display, secondary and product-type fields."""
    display = _store_tokens(info, "store_display_classification")
    secondary = _store_tokens(info, "secondary_classification")
    product_type = _store_tokens(info, "product_type")

    for token in display:
        if token == "VIRTUAL_CURRENCY":
            return "In-Game Currency", f"store_display_classification={token}"
    for token in secondary:
        if token in _ADD_ON_SECONDARY_TYPES:
            return "DLC / Add-on", f"secondary_classification={token}"
    for token in display:
        if token in ADD_ON_DISPLAY_TYPES:
            return "DLC / Add-on", f"store_display_classification={token}"
    for token in display:
        if token == "GAME_BUNDLE":
            return "Bundle", f"store_display_classification={token}"
    for token in display:
        if token in _FULL_GAME_DISPLAY_TYPES:
            return "Full Game", f"store_display_classification={token}"
    for token in product_type:
        if token == "GAME_BUNDLE":
            return "Bundle", f"product_type={token}"
        if token == "GAME_FULL_GAME":
            return "Full Game", f"product_type={token}"
    return None


def is_positive_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def is_zero_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0


def is_negative_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value < 0


def classify_detailed(
    name: str,
    sku: str,
    item_total: Any,
    item_original: Any,
    info: dict,
    sku_type: str = "",
    tx_type: str = "",
) -> tuple[str, bool | None, str, str]:
    """Return category, PS Plus flag, evidence source and specific evidence."""
    name_upper = name.upper()
    content_type = normalise_token(str(info.get("content_type") or ""))
    top_category = normalise_token(str(info.get("top_category") or ""))
    sku_type_token = normalise_token(sku_type)
    tx_type_token = normalise_token(tx_type)
    is_plus_name = (
        "PLAYSTATION PLUS" in name_upper or "PLAYSTATION®PLUS" in name_upper
    )

    if sku_type_token == "SUBSCRIPTION" or tx_type_token == "CYCLE_SUBSCRIPTION":
        evidence = (
            f"sku_type={sku_type_token}"
            if sku_type_token == "SUBSCRIPTION"
            else f"transaction_type={tx_type_token}"
        )
        return "Subscription", True if is_plus_name else None, "transaction", evidence

    if is_plus_name and any(word in name_upper for word in ("PACK", "BUNDLE")):
        return (
            "PS Plus Pack",
            True,
            "product_name",
            "product name contains PS Plus and pack/bundle",
        )

    if (
        is_plus_name
        and is_zero_number(item_total)
        and is_positive_number(item_original)
    ):
        return (
            "PS Plus Monthly",
            True,
            "product_name",
            "PS Plus name; item total=0; original item price>0",
        )

    if content_type in {"ADDON", "ADD_ON", "ADD_ON_CONTENT", "DLC"}:
        return (
            "DLC / Add-on",
            True if is_plus_name else None,
            "store_api",
            f"content_type={content_type}",
        )
    if content_type == "BUNDLE":
        return (
            "Bundle",
            True if is_plus_name else None,
            "store_api",
            "content_type=BUNDLE",
        )
    if content_type in {"GAME", "FULL_GAME", "PS5_GAME", "PS4_GAME"}:
        return (
            "Full Game",
            True if is_plus_name else None,
            "store_api",
            f"content_type={content_type}",
        )
    if content_type in {"CURRENCY", "VC", "INGAME_CURRENCY"}:
        return (
            "In-Game Currency",
            True if is_plus_name else None,
            "store_api",
            f"content_type={content_type}",
        )

    if top_category in {"ADD_ON", "ADDONS", "DOWNLOADABLE_CONTENT"}:
        return (
            "DLC / Add-on",
            True if is_plus_name else None,
            "store_api",
            f"top_category={top_category}",
        )
    if top_category in {"DOWNLOADABLE_GAME", "GAME", "GAMES"}:
        return (
            "Full Game",
            True if is_plus_name else None,
            "store_api",
            f"top_category={top_category}",
        )
    if top_category in {"SUBSCRIPTION", "SUBSCRIPTIONS"}:
        return (
            "Subscription",
            True if is_plus_name else None,
            "store_api",
            f"top_category={top_category}",
        )
    if top_category == "BUNDLE":
        return (
            "Bundle",
            True if is_plus_name else None,
            "store_api",
            "top_category=BUNDLE",
        )

    store_classification = _store_classification(info)
    if store_classification is not None:
        category, evidence = store_classification
        return category, True if is_plus_name else None, "store_api", evidence

    if sku_type_token in {"PRE_ORDER", "PRE_ORDER_VOUCHER"}:
        return (
            "Pre-order",
            True if is_plus_name else None,
            "transaction",
            f"sku_type={sku_type_token}",
        )
    if sku_type_token == "PROMOTION":
        return (
            "Promotion",
            True if is_plus_name else None,
            "transaction",
            "sku_type=PROMOTION",
        )
    if sku_type_token == "VOUCHER" or tx_type_token == "VOUCHER_PURCHASE":
        evidence = (
            "sku_type=VOUCHER"
            if sku_type_token == "VOUCHER"
            else "transaction_type=VOUCHER_PURCHASE"
        )
        return "Voucher", True if is_plus_name else None, "transaction", evidence

    if is_plus_name:
        return (
            "PS Plus Item",
            True,
            "product_name",
            "product name contains PS Plus",
        )

    for keyword in ("season pass", "skin", "costume", "add-on", "addon", "dlc"):
        if keyword in name.lower():
            return (
                "DLC / Add-on",
                None,
                "heuristic",
                f'product name contains "{keyword}"',
            )

    return "Other", None, "unknown", "no supported classification evidence"


def classify(
    name: str,
    sku: str,
    item_total: Any,
    item_original: Any,
    info: dict,
) -> tuple[str, bool | None]:
    """Compatibility wrapper returning category and PS Plus evidence."""
    category, is_ps_plus, _source, _evidence = classify_detailed(
        name,
        sku,
        item_total,
        item_original,
        info,
    )
    return category, is_ps_plus


def transaction_category(tx_type: str) -> str:
    token = normalise_token(tx_type)
    if token == "CYCLE_SUBSCRIPTION":
        return "Subscription"
    if token == "DEPOSIT_CHARGE":
        return "Wallet top-up"
    if "REFUND" in token:
        return "Refund"
    if token == "POINT_PAYMENT":
        return "Points payment"
    if token == "VOUCHER_PURCHASE":
        return "Voucher"
    return "Other"


# Compatibility aliases for existing internal callers.
_normalise_token = normalise_token
_is_positive_number = is_positive_number
_is_zero_number = is_zero_number
_is_negative_number = is_negative_number
_classify_detailed = classify_detailed
_transaction_category = transaction_category


def _classify(
    name: str,
    sku: str,
    tx_total: Any,
    tx_original: Any,
    info: dict,
) -> tuple[str, bool | None]:
    return classify(name, sku, tx_total, tx_original, info)
