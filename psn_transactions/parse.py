import json
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from rich.console import Console
from rich.progress import Progress, BarColumn, SpinnerColumn, TextColumn, TaskProgressColumn
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from psn_transactions import config as cfg
from psn_transactions.errors import PSNTransactionsError
from psn_transactions.paths import app_dir
from psn_transactions.storage import (
    atomic_write_csv,
    atomic_write_json,
    secure_private_directory,
    secure_private_file,
)

SKU_CACHE_FILE = app_dir() / "sku_cache.json"
_CHIHIRO_TEMPLATE = "https://store.playstation.com/store/api/chihiro/00_09_000/container/{country}/{lang}/999/{sku}"
CACHE_SCHEMA_VERSION = 2
CACHE_SOURCE = "playstation-store-chihiro"
NOT_FOUND_CACHE_TTL = timedelta(days=7)
NO_METADATA_CACHE_TTL = timedelta(days=1)
STORE_TIMEOUT_SECONDS = 10


def _chihiro_url(sku_base: str) -> str:
    locale = cfg.get_locale()
    country, lang = cfg.locale_parts(locale)
    return _CHIHIRO_TEMPLATE.format(country=country, lang=lang, sku=sku_base)

CSV_FIELDS = [
    "date", "transaction_id", "product", "category", "content_type",
    "top_category", "platform", "publisher", "release_date",
    "enrichment_status", "classification_source",
    "paid", "original", "discount", "tax", "is_ps_plus", "sku",
    "payment", "card_last4",
]

console = Console()


# ---------------------------------------------------------------------------
# SKU lookup
# ---------------------------------------------------------------------------

def _empty_cache() -> dict:
    return {
        "schema_version": CACHE_SCHEMA_VERSION,
        "source": CACHE_SOURCE,
        "entries": {},
    }


def _cache_key(sku: str, locale: str | None = None) -> str:
    resolved_locale = locale or cfg.get_locale()
    return f"{resolved_locale}|{_sku_base(sku)}"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_cache_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_cache_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _metadata_is_useful(metadata: dict) -> bool:
    return any(
        metadata.get(field)
        for field in (
            "content_type",
            "top_category",
            "platform",
            "publisher",
            "release_date",
        )
    )


def _migrate_legacy_cache(cache: dict) -> dict:
    migrated = _empty_cache()
    fetched_at = _format_cache_time(_utc_now())
    entries = migrated["entries"]
    for sku, metadata in cache.items():
        if not isinstance(sku, str) or not isinstance(metadata, dict):
            raise PSNTransactionsError(
                f"SKU cache at {SKU_CACHE_FILE} must contain an object of SKU records."
            )
        if "error" in metadata or not _metadata_is_useful(metadata):
            continue
        entries[_cache_key(sku)] = {
            "status": "success",
            "fetched_at": fetched_at,
            "metadata": metadata,
        }
    return migrated


def _load_cache() -> dict:
    if not SKU_CACHE_FILE.exists():
        return _empty_cache()

    secure_private_file(SKU_CACHE_FILE, "SKU cache")
    try:
        cache = json.loads(SKU_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PSNTransactionsError(
            f"Could not read SKU cache from {SKU_CACHE_FILE}: {exc}"
        ) from exc

    if not isinstance(cache, dict):
        raise PSNTransactionsError(
            f"SKU cache at {SKU_CACHE_FILE} must contain an object of SKU records."
        )

    if "schema_version" not in cache:
        return _migrate_legacy_cache(cache)

    if (
        cache.get("schema_version") != CACHE_SCHEMA_VERSION
        or cache.get("source") != CACHE_SOURCE
        or not isinstance(cache.get("entries"), dict)
        or any(
            not isinstance(key, str) or not isinstance(record, dict)
            for key, record in cache["entries"].items()
        )
    ):
        raise PSNTransactionsError(
            f"SKU cache at {SKU_CACHE_FILE} uses an unsupported schema. "
            "Remove it and rerun the enriched export."
        )
    return cache


def _save_cache(cache: dict) -> None:
    secure_private_directory(SKU_CACHE_FILE.parent, "SKU cache")
    atomic_write_json(SKU_CACHE_FILE, cache, description="SKU cache")
    secure_private_file(SKU_CACHE_FILE, "SKU cache")


def _sku_base(sku: str) -> str:
    """Strip regional variant suffix (e.g. -E001): EP1006-PPSA14382_00-XXX-E001 -> EP1006-PPSA14382_00-XXX"""
    return re.sub(r"-[A-Z]\d{3}$", "", sku)


def _cached_result(sku: str, cache: dict, now: datetime | None = None) -> dict | None:
    record = cache["entries"].get(_cache_key(sku))
    if not isinstance(record, dict):
        return None

    status = record.get("status")
    metadata = record.get("metadata")
    if status not in {"success", "not_found", "no_metadata"} or not isinstance(
        metadata, dict
    ):
        cache["entries"].pop(_cache_key(sku), None)
        return None
    if status == "success" and not _metadata_is_useful(metadata):
        cache["entries"].pop(_cache_key(sku), None)
        return None

    fetched_at = _parse_cache_time(record.get("fetched_at"))
    age = (now or _utc_now()) - fetched_at if fetched_at else None
    ttl = {
        "not_found": NOT_FOUND_CACHE_TTL,
        "no_metadata": NO_METADATA_CACHE_TTL,
    }.get(status)
    if ttl is not None and (age is None or age > ttl):
        cache["entries"].pop(_cache_key(sku), None)
        return None

    return {"status": status, "metadata": metadata, "cached": True}


def _normalise_token(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", value.upper()).strip("_")


def _normalise_content_type(attrs: dict) -> str:
    for key in ("game_content_type", "game_contentType"):
        value = attrs.get(key)
        if isinstance(value, str) and value.strip():
            return _normalise_token(value)

    content_types = attrs.get("gameContentTypesList")
    if isinstance(content_types, list):
        for item in content_types:
            if isinstance(item, dict):
                value = item.get("key") or item.get("name")
            else:
                value = item
            if isinstance(value, str) and value.strip():
                return _normalise_token(value)

    value = attrs.get("content_type")
    if isinstance(value, str) and value.strip() and not value.strip().isdigit():
        return _normalise_token(value)
    return ""


def _normalise_text_list(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "; ".join(
            item.strip() for item in value if isinstance(item, str) and item.strip()
        )
    return ""


def _normalise_store_metadata(data: dict) -> dict:
    attrs = data.get("attributes", {}) or data
    if not isinstance(attrs, dict):
        return {}
    return {
        "content_type": _normalise_content_type(attrs),
        "top_category": _normalise_text_list(attrs.get("top_category")),
        "platform": _normalise_text_list(attrs.get("playable_platform")),
        "publisher": _normalise_text_list(attrs.get("provider_name")),
        "release_date": _normalise_text_list(attrs.get("release_date")),
    }


def _store_session() -> requests.Session:
    retry = Retry(
        total=3,
        connect=2,
        read=2,
        status=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods={"GET"},
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    return session


def _record_cache_result(sku: str, cache: dict, status: str, metadata: dict) -> None:
    cache["entries"][_cache_key(sku)] = {
        "status": status,
        "fetched_at": _format_cache_time(_utc_now()),
        "metadata": metadata,
    }


def _record_lookup_result(sku: str, cache: dict, result: dict) -> None:
    if result.get("status") in {"success", "not_found", "no_metadata"}:
        _record_cache_result(
            sku,
            cache,
            result["status"],
            result.get("metadata") or {},
        )


def _fetch_sku(
    sku: str,
    session: requests.Session | None = None,
) -> dict:
    if not sku:
        return {"status": "missing_sku", "metadata": {}, "cached": False}

    url = _chihiro_url(_sku_base(sku))
    client = session or requests
    try:
        response = client.get(
            url,
            timeout=STORE_TIMEOUT_SECONDS,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if response.status_code == 404:
            return {"status": "not_found", "metadata": {}, "cached": False}
        if response.status_code == 204:
            return {"status": "no_metadata", "metadata": {}, "cached": False}
        if response.status_code != 200:
            return {
                "status": "temporary_failure",
                "metadata": {},
                "cached": False,
                "detail": f"HTTP {response.status_code}",
            }

        try:
            data = response.json()
        except ValueError as exc:
            return {
                "status": "temporary_failure",
                "metadata": {},
                "cached": False,
                "detail": f"invalid JSON: {exc}",
            }
        if not isinstance(data, dict):
            return {
                "status": "temporary_failure",
                "metadata": {},
                "cached": False,
                "detail": "unexpected response shape",
            }

        metadata = _normalise_store_metadata(data)
        status = "success" if _metadata_is_useful(metadata) else "no_metadata"
        return {"status": status, "metadata": metadata, "cached": False}
    except requests.RequestException as exc:
        return {
            "status": "temporary_failure",
            "metadata": {},
            "cached": False,
            "detail": str(exc),
        }


def _lookup_sku(
    sku: str,
    cache: dict,
    session: requests.Session | None = None,
) -> dict:
    if not sku:
        return {"status": "missing_sku", "metadata": {}, "cached": False}
    cached = _cached_result(sku, cache)
    if cached is not None:
        return cached

    result = _fetch_sku(sku, session=session)
    _record_lookup_result(sku, cache, result)
    return result


def _group_skus(skus: set[str]) -> list[tuple[str, list[str]]]:
    grouped: dict[str, list[str]] = {}
    for sku in sorted(skus):
        grouped.setdefault(_cache_key(sku), []).append(sku)
    return [(variants[0], variants) for variants in grouped.values()]


def _enrich_skus(skus: set[str], cache: dict) -> dict[str, dict]:
    results: dict[str, dict] = {}
    pending: list[tuple[str, list[str]]] = []
    grouped_skus = _group_skus(skus)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("SKU enrichment", total=len(skus))
        for representative, variants in grouped_skus:
            cached = _cached_result(representative, cache)
            if cached is None:
                pending.append((representative, variants))
                continue
            for sku in variants:
                results[sku] = cached
            progress.advance(task, len(variants))

        completed_requests = 0

        def record_completed(
            representative: str,
            variants: list[str],
            result: dict,
        ) -> None:
            nonlocal completed_requests
            _record_lookup_result(representative, cache, result)
            for sku in variants:
                results[sku] = result
            completed_requests += 1
            progress.advance(task, len(variants))
            if completed_requests % 20 == 0:
                _save_cache(cache)

        try:
            with _store_session() as session:
                for representative, variants in pending:
                    record_completed(
                        representative,
                        variants,
                        _fetch_sku(representative, session=session),
                    )
        except KeyboardInterrupt as exc:
            _save_cache(cache)
            raise PSNTransactionsError(
                "Enrichment interrupted; completed lookup results were saved to the cache."
            ) from exc

    return results


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _is_positive_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _is_zero_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0


def _classify_detailed(
    name: str,
    sku: str,
    tx_total: Any,
    item_original: Any,
    info: dict,
    sku_type: str = "",
    tx_type: str = "",
) -> tuple[str, bool | None, str]:
    """Return category, PS Plus evidence and the category's evidence source."""
    name_upper = name.upper()
    content_type = _normalise_token(str(info.get("content_type") or ""))
    top_category = _normalise_token(str(info.get("top_category") or ""))
    sku_type_token = _normalise_token(sku_type)
    tx_type_token = _normalise_token(tx_type)
    is_plus_name = (
        "PLAYSTATION PLUS" in name_upper or "PLAYSTATION®PLUS" in name_upper
    )

    if sku_type_token == "SUBSCRIPTION" or tx_type_token == "CYCLE_SUBSCRIPTION":
        return "Subscription", True if is_plus_name else None, "transaction"

    if is_plus_name and any(word in name_upper for word in ("PACK", "BUNDLE")):
        return "PS Plus Pack", True, "product_name"

    if (
        is_plus_name
        and _is_zero_number(tx_total)
        and _is_positive_number(item_original)
    ):
        return "PS Plus Monthly", True, "product_name"

    if content_type in {"ADDON", "ADD_ON", "ADD_ON_CONTENT", "DLC"}:
        return "DLC / Add-on", True if is_plus_name else None, "store_api"
    if content_type == "BUNDLE":
        return "Bundle", True if is_plus_name else None, "store_api"
    if content_type in {"GAME", "FULL_GAME", "PS5_GAME", "PS4_GAME"}:
        return "Full Game", True if is_plus_name else None, "store_api"
    if content_type in {"CURRENCY", "VC", "INGAME_CURRENCY"}:
        return "In-Game Currency", True if is_plus_name else None, "store_api"

    if top_category in {"ADD_ON", "ADDONS", "DOWNLOADABLE_CONTENT"}:
        return "DLC / Add-on", True if is_plus_name else None, "store_api"
    if top_category in {"DOWNLOADABLE_GAME", "GAME", "GAMES"}:
        return "Full Game", True if is_plus_name else None, "store_api"
    if top_category in {"SUBSCRIPTION", "SUBSCRIPTIONS"}:
        return "Subscription", True if is_plus_name else None, "store_api"
    if top_category == "BUNDLE":
        return "Bundle", True if is_plus_name else None, "store_api"

    if sku_type_token in {"PRE_ORDER", "PRE_ORDER_VOUCHER"}:
        return "Pre-order", True if is_plus_name else None, "transaction"
    if sku_type_token == "PROMOTION":
        return "Promotion", True if is_plus_name else None, "transaction"
    if sku_type_token == "VOUCHER" or tx_type_token == "VOUCHER_PURCHASE":
        return "Voucher", True if is_plus_name else None, "transaction"

    if is_plus_name:
        return "PS Plus Item", True, "product_name"

    if any(
        keyword in name.lower()
        for keyword in ("season pass", "skin", "costume", "add-on", "addon", "dlc")
    ):
        return "DLC / Add-on", None, "heuristic"

    return "Other", None, "unknown"


def _classify(
    name: str,
    sku: str,
    tx_total: Any,
    tx_original: Any,
    info: dict,
) -> tuple[str, bool | None]:
    """Compatibility wrapper returning category and PS Plus evidence."""
    category, is_ps_plus, _source = _classify_detailed(
        name,
        sku,
        tx_total,
        tx_original,
        info,
    )
    return category, is_ps_plus


# ---------------------------------------------------------------------------
# Flattening
# ---------------------------------------------------------------------------

def _transaction_category(tx_type: str) -> str:
    token = _normalise_token(tx_type)
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


def _result_from_cache(sku: str, cache: dict) -> dict | None:
    if "entries" in cache:
        return _cached_result(sku, cache)

    # Keep direct callers using the original in-memory cache shape working.
    metadata = cache.get(sku)
    if isinstance(metadata, dict) and _metadata_is_useful(metadata):
        return {"status": "success", "metadata": metadata, "cached": True}
    return None


def _flatten(
    txs: list,
    cache: dict,
    enrich: bool,
    enrichment_results: dict[str, dict] | None = None,
) -> list:
    rows = []
    for t in txs:
        date_iso = t.get("date", "")
        date_str = (
            datetime.fromisoformat(date_iso.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
            if date_iso else ""
        )
        tx_id = t.get("id", "")
        tx_type = t.get("transactionType") or t.get("invoiceType", "")

        charge = (t.get("chargeDetails") or [{}])[0]
        payment = charge.get("paymentMethod", "")
        card_last4 = charge.get("paymentDescriptionDisplay", "").replace("*", "").strip()

        pd = t.get("purchaseDetails") or {}
        tx_total = pd.get("total", 0)
        products = pd.get("productPurchases") or []

        if not products:
            # Wallet top-up, subscription charge, or refund with no product list
            rows.append({
                "date": date_str,
                "transaction_id": tx_id,
                "product": tx_type or "",
                "category": _transaction_category(tx_type) if enrich else "",
                "content_type": "",
                "top_category": "",
                "platform": "",
                "publisher": "",
                "release_date": "",
                "enrichment_status": "not_applicable" if enrich else "",
                "classification_source": "transaction" if enrich else "",
                "paid": t.get("displayOfTransactionValue", ""),
                "original": pd.get("displayOfOriginalPrice", ""),
                "discount": pd.get("displayOfDiscount", ""),
                "tax": pd.get("displayOfTax", ""),
                "is_ps_plus": "",
                "sku": "",
                "payment": payment,
                "card_last4": card_last4,
            })
            continue

        for p in products:
            sku = p.get("skuId", "")
            name = p.get("productName", "")
            result = None
            if enrich:
                result = (enrichment_results or {}).get(sku) or _result_from_cache(
                    sku, cache
                )
            if result is None:
                result = {
                    "status": "not_requested" if sku else "missing_sku",
                    "metadata": {},
                    "cached": False,
                }
            info = result.get("metadata") or {}

            if enrich:
                category, is_ps_plus, classification_source = _classify_detailed(
                    name,
                    sku,
                    tx_total,
                    p.get("originalPrice"),
                    info,
                    sku_type=p.get("skuType") or "",
                    tx_type=tx_type,
                )
            else:
                category, is_ps_plus, classification_source = "", "", ""

            rows.append({
                "date": date_str,
                "transaction_id": tx_id,
                "product": name,
                "category": category,
                "content_type": info.get("content_type", ""),
                "top_category": info.get("top_category", ""),
                "platform": info.get("platform", ""),
                "publisher": info.get("publisher", ""),
                "release_date": info.get("release_date", ""),
                "enrichment_status": result.get("status", "") if enrich else "",
                "classification_source": classification_source,
                "paid": p.get("totalFormatted") or p.get("displayOfPrice", ""),
                "original": p.get("originalPriceFormatted", ""),
                "discount": p.get("discountFormatted", ""),
                "tax": p.get("taxFormatted", ""),
                "is_ps_plus": "" if is_ps_plus is None else is_ps_plus,
                "sku": sku,
                "payment": payment,
                "card_last4": card_last4,
            })

    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _load_transactions(json_path: str) -> list[dict]:
    input_path = Path(json_path)
    try:
        payload = json.loads(input_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PSNTransactionsError(
            f"Transaction JSON not found at {input_path}. Run `psn-transactions fetch` first."
        ) from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PSNTransactionsError(
            f"Could not read transaction JSON from {input_path}: {exc}"
        ) from exc

    if not isinstance(payload, list):
        raise PSNTransactionsError(
            f"Transaction JSON at {input_path} must contain a list of transactions."
        )
    if any(not isinstance(transaction, dict) for transaction in payload):
        raise PSNTransactionsError(
            f"Transaction JSON at {input_path} contains a non-object transaction."
        )
    return payload


def export(
    json_path: str = "psn_transactions.json",
    csv_path: str = "psn_transactions.csv",
    enrich: bool = False,
) -> None:
    txs = _load_transactions(json_path)
    console.print(f"Loaded {len(txs)} transactions from {json_path}")

    cache = _load_cache() if enrich else {}
    enrichment_results: dict[str, dict] = {}

    if enrich:
        # Collect unique SKUs, including cached entries so the summary reflects
        # the complete export rather than only network requests.
        try:
            skus = {
                product["skuId"]
                for transaction in txs
                for product in (transaction.get("purchaseDetails") or {}).get(
                    "productPurchases", []
                )
                if product.get("skuId")
            }
        except (AttributeError, KeyError, TypeError) as exc:
            raise PSNTransactionsError(
                f"Transaction JSON at {json_path} has an unexpected purchase structure."
            ) from exc

        if skus:
            console.print(f"Enriching {len(skus)} unique SKUs...")
            enrichment_results = _enrich_skus(skus, cache)

            _save_cache(cache)
            status_counts = Counter(
                result["status"] for result in enrichment_results.values()
            )
            cache_hits = sum(
                bool(result.get("cached"))
                for result in enrichment_results.values()
            )
            summary = ", ".join(
                f"{status.replace('_', ' ')}: {count}"
                for status, count in sorted(status_counts.items())
            )
            console.print(
                f"Enrichment results — cache hits: {cache_hits}; {summary}"
            )
            console.print(
                f"✓ Cache saved ({len(cache['entries'])} locale-scoped records)"
            )

    try:
        rows = _flatten(
            txs,
            cache,
            enrich,
            enrichment_results=enrichment_results,
        )
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
        raise PSNTransactionsError(
            f"Transaction JSON at {json_path} has an unexpected transaction structure."
        ) from exc

    atomic_write_csv(Path(csv_path), rows, CSV_FIELDS)

    console.print(f"✓ Saved [bold]{len(rows)}[/bold] rows to {csv_path}")
