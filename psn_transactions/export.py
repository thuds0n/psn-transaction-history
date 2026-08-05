"""CSV schemas, transaction flattening and export orchestration."""

import json
from collections import Counter
from datetime import datetime
from pathlib import Path

from rich.console import Console

from psn_transactions import classify
from psn_transactions import enrich as store
from psn_transactions.errors import PSNTransactionsError
from psn_transactions.storage import atomic_write_csv


CORE_CSV_FIELDS = [
    "date", "transaction_id", "order_item_id", "product",
    "paid", "paid_minor", "original", "original_minor", "discount",
    "discount_minor", "tax", "tax_minor", "sku",
]

ENRICHED_CSV_FIELDS = [
    "date", "transaction_id", "order_item_id", "product", "category",
    "content_type", "top_category", "platform", "publisher", "release_date",
    "enrichment_status", "enrichment_detail", "classification_source",
    "classification_evidence",
    "paid", "paid_minor", "original", "original_minor", "discount",
    "discount_minor", "tax", "tax_minor", "is_ps_plus", "sku",
]

PAYMENT_DETAIL_FIELDS = ["payment", "card_last4"]

_TRANSACTION_TEXT_FIELDS = (
    "date",
    "id",
    "transactionType",
    "invoiceType",
    "displayOfTransactionValue",
)
_PURCHASE_TEXT_FIELDS = (
    "displayOfOriginalPrice",
    "displayOfDiscount",
    "displayOfTax",
)
_PRODUCT_TEXT_FIELDS = (
    "orderItemId",
    "skuId",
    "productName",
    "skuType",
    "totalFormatted",
    "displayOfPrice",
    "originalPriceFormatted",
    "discountFormatted",
    "taxFormatted",
)
_PRODUCT_NUMBER_FIELDS = ("total", "originalPrice", "discount", "tax")

console = Console()


def _value_type(value: object) -> str:
    if value is None:
        return "null"
    return type(value).__name__


def _raise_schema_error(
    input_path: Path,
    field_path: str,
    expected: str,
    value: object,
) -> None:
    raise PSNTransactionsError(
        f"Transaction JSON at {input_path} has invalid {field_path}: "
        f"expected {expected}, found {_value_type(value)}."
    )


def _validate_optional_text_fields(
    value: dict,
    fields: tuple[str, ...],
    *,
    input_path: Path,
    field_path: str,
) -> None:
    for field in fields:
        field_value = value.get(field)
        if field_value is not None and not isinstance(field_value, str):
            _raise_schema_error(
                input_path,
                f"{field_path}.{field}",
                "a string or null",
                field_value,
            )


def _validate_transactions_schema(
    transactions: list[dict],
    input_path: Path,
) -> None:
    """Validate structures used by CSV export without requiring optional data."""
    for transaction_index, transaction in enumerate(transactions):
        transaction_path = f"transaction[{transaction_index}]"
        _validate_optional_text_fields(
            transaction,
            _TRANSACTION_TEXT_FIELDS,
            input_path=input_path,
            field_path=transaction_path,
        )

        date_value = transaction.get("date")
        if date_value:
            try:
                datetime.fromisoformat(date_value.replace("Z", "+00:00"))
            except ValueError:
                raise PSNTransactionsError(
                    f"Transaction JSON at {input_path} has invalid "
                    f"{transaction_path}.date: expected an ISO 8601 timestamp."
                ) from None

        charge_details = transaction.get("chargeDetails")
        if charge_details is not None:
            if not isinstance(charge_details, list):
                _raise_schema_error(
                    input_path,
                    f"{transaction_path}.chargeDetails",
                    "a list or null",
                    charge_details,
                )
            for charge_index, charge in enumerate(charge_details):
                charge_path = f"{transaction_path}.chargeDetails[{charge_index}]"
                if not isinstance(charge, dict):
                    _raise_schema_error(
                        input_path,
                        charge_path,
                        "an object",
                        charge,
                    )
                _validate_optional_text_fields(
                    charge,
                    ("paymentMethod", "paymentDescriptionDisplay"),
                    input_path=input_path,
                    field_path=charge_path,
                )

        purchase_details = transaction.get("purchaseDetails")
        if purchase_details is None:
            continue
        if not isinstance(purchase_details, dict):
            _raise_schema_error(
                input_path,
                f"{transaction_path}.purchaseDetails",
                "an object or null",
                purchase_details,
            )
        _validate_optional_text_fields(
            purchase_details,
            _PURCHASE_TEXT_FIELDS,
            input_path=input_path,
            field_path=f"{transaction_path}.purchaseDetails",
        )

        products = purchase_details.get("productPurchases")
        if products is None:
            continue
        if not isinstance(products, list):
            _raise_schema_error(
                input_path,
                f"{transaction_path}.purchaseDetails.productPurchases",
                "a list or null",
                products,
            )
        for product_index, product in enumerate(products):
            product_path = (
                f"{transaction_path}.purchaseDetails."
                f"productPurchases[{product_index}]"
            )
            if not isinstance(product, dict):
                _raise_schema_error(input_path, product_path, "an object", product)
            _validate_optional_text_fields(
                product,
                _PRODUCT_TEXT_FIELDS,
                input_path=input_path,
                field_path=product_path,
            )
            for field in _PRODUCT_NUMBER_FIELDS:
                field_value = product.get(field)
                if field_value is not None and (
                    isinstance(field_value, bool)
                    or not isinstance(field_value, (int, float))
                ):
                    _raise_schema_error(
                        input_path,
                        f"{product_path}.{field}",
                        "a number or null",
                        field_value,
                    )


def _product_missing_detail(product: dict) -> str:
    missing_fields = [
        field
        for field in ("orderItemId", "productName", "total")
        if product.get(field) is None or product.get(field) == ""
    ]
    if not missing_fields:
        return ""
    label = "field" if len(missing_fields) == 1 else "fields"
    return f"Transaction item missing optional {label}: {', '.join(missing_fields)}"


def _combine_details(*details: str) -> str:
    return "; ".join(detail for detail in details if detail)


def _result_from_cache(sku: str, cache: dict) -> dict | None:
    if "entries" in cache:
        return store.cached_result(sku, cache)

    # Keep direct callers using the original in-memory cache shape working.
    metadata = cache.get(sku)
    if isinstance(metadata, dict) and store.metadata_is_useful(metadata):
        return {"status": "success", "metadata": metadata, "cached": True}
    return None


def flatten_transactions(
    txs: list,
    cache: dict,
    enriched: bool,
    enrichment_results: dict[str, dict] | None = None,
    include_payment_details: bool = False,
) -> list[dict]:
    rows = []
    for transaction in txs:
        date_iso = transaction.get("date", "")
        date_str = (
            datetime.fromisoformat(date_iso.replace("Z", "+00:00")).strftime(
                "%Y-%m-%d %H:%M"
            )
            if date_iso
            else ""
        )
        tx_id = transaction.get("id", "")
        tx_type = transaction.get("transactionType") or transaction.get(
            "invoiceType", ""
        )

        charge_details = transaction.get("chargeDetails") or []
        charge = charge_details[0] if charge_details else {}
        payment = charge.get("paymentMethod", "")
        card_last4 = (
            charge.get("paymentDescriptionDisplay", "").replace("*", "").strip()
        )

        purchase_details = transaction.get("purchaseDetails") or {}
        products = purchase_details.get("productPurchases") or []

        if not products:
            rows.append(
                {
                    "date": date_str,
                    "transaction_id": tx_id,
                    "order_item_id": "",
                    "product": tx_type or "",
                    "category": (
                        classify.transaction_category(tx_type) if enriched else ""
                    ),
                    "content_type": "",
                    "top_category": "",
                    "platform": "",
                    "publisher": "",
                    "release_date": "",
                    "enrichment_status": "not_applicable" if enriched else "",
                    "enrichment_detail": "",
                    "classification_source": "transaction" if enriched else "",
                    "classification_evidence": (
                        f"transaction_type={classify.normalise_token(tx_type)}"
                        if enriched and tx_type
                        else ""
                    ),
                    "paid": transaction.get("displayOfTransactionValue", ""),
                    "paid_minor": "",
                    "original": purchase_details.get("displayOfOriginalPrice", ""),
                    "original_minor": "",
                    "discount": purchase_details.get("displayOfDiscount", ""),
                    "discount_minor": "",
                    "tax": purchase_details.get("displayOfTax", ""),
                    "tax_minor": "",
                    "is_ps_plus": "",
                    "sku": "",
                    "payment": payment,
                    "card_last4": card_last4,
                }
            )
            continue

        for product in products:
            sku = product.get("skuId", "")
            name = product.get("productName", "")
            result = None
            if enriched:
                result = (enrichment_results or {}).get(sku) or _result_from_cache(
                    sku, cache
                )
            if result is None:
                result = {
                    "status": "not_requested" if sku else "missing_sku",
                    "metadata": {},
                    "cached": False,
                    "detail": "Transaction item has no SKU" if not sku else "",
                }
            info = result.get("metadata") or {}
            enrichment_detail = _combine_details(
                result.get("detail", ""),
                _product_missing_detail(product) if enriched else "",
            )

            if enriched:
                (
                    category,
                    is_ps_plus,
                    classification_source,
                    classification_evidence,
                ) = classify.classify_detailed(
                    name,
                    sku,
                    product.get("total"),
                    product.get("originalPrice"),
                    info,
                    sku_type=product.get("skuType") or "",
                    tx_type=tx_type,
                )
            else:
                category, is_ps_plus, classification_source = "", "", ""
                classification_evidence = ""

            rows.append(
                {
                    "date": date_str,
                    "transaction_id": tx_id,
                    "order_item_id": product.get("orderItemId", ""),
                    "product": name,
                    "category": category,
                    "content_type": info.get("content_type", ""),
                    "top_category": info.get("top_category", ""),
                    "platform": info.get("platform", ""),
                    "publisher": info.get("publisher", ""),
                    "release_date": info.get("release_date", ""),
                    "enrichment_status": result.get("status", "") if enriched else "",
                    "enrichment_detail": enrichment_detail if enriched else "",
                    "classification_source": classification_source,
                    "classification_evidence": classification_evidence,
                    "paid": product.get("totalFormatted")
                    or product.get("displayOfPrice", ""),
                    "paid_minor": product.get("total", ""),
                    "original": product.get("originalPriceFormatted", ""),
                    "original_minor": product.get("originalPrice", ""),
                    "discount": product.get("discountFormatted", ""),
                    "discount_minor": product.get("discount", ""),
                    "tax": product.get("taxFormatted", ""),
                    "tax_minor": product.get("tax", ""),
                    "is_ps_plus": "" if is_ps_plus is None else is_ps_plus,
                    "sku": sku,
                    "payment": payment,
                    "card_last4": card_last4,
                }
            )

    rows.sort(key=lambda row: row["date"], reverse=True)
    fields = list(ENRICHED_CSV_FIELDS if enriched else CORE_CSV_FIELDS)
    if include_payment_details:
        fields.extend(PAYMENT_DETAIL_FIELDS)
    return [{field: row[field] for field in fields} for row in rows]


def paid_only_transactions(txs: list[dict]) -> tuple[list[dict], Counter]:
    """Return copies containing paid product rows and counts for skipped data."""
    filtered = []
    counts = Counter()
    for transaction in txs:
        purchase_details = transaction.get("purchaseDetails") or {}
        products = purchase_details.get("productPurchases") or []
        if not products:
            counts["non_product"] += 1
            continue

        paid_products = []
        for product in products:
            total = product.get("total")
            if classify.is_positive_number(total):
                paid_products.append(product)
                counts["included"] += 1
            elif classify.is_zero_number(total):
                counts["zero_cost"] += 1
            elif classify.is_negative_number(total):
                counts["negative_total"] += 1
            else:
                counts["unknown_price"] += 1

        if paid_products:
            filtered.append(
                {
                    **transaction,
                    "purchaseDetails": {
                        **purchase_details,
                        "productPurchases": paid_products,
                    },
                }
            )
    return filtered, counts


def _product_row_count(txs: list[dict]) -> int:
    return sum(
        len((transaction.get("purchaseDetails") or {}).get("productPurchases") or [])
        for transaction in txs
    )


def _print_enrichment_summary(
    *,
    transaction_count: int,
    product_row_count: int,
    rows: list[dict],
    skus: set[str],
    results: dict[str, dict],
) -> None:
    lookup_statuses = {}
    cache_hit_keys = set()
    network_keys = set()
    for sku, result in results.items():
        key = store.cache_key(sku)
        lookup_statuses[key] = result.get("status", "unknown")
        if result.get("cached"):
            cache_hit_keys.add(key)
        if result.get("network_requested") or (
            not result.get("cached") and result.get("status") != "cache_miss"
        ):
            network_keys.add(key)

    status_counts = Counter(lookup_statuses.values())
    source_counts = Counter(
        row.get("classification_source") or "none" for row in rows
    )
    status_summary = ", ".join(
        f"{status.replace('_', ' ')}: {count}"
        for status, count in sorted(status_counts.items())
    ) or "none"
    source_summary = ", ".join(
        f"{source.replace('_', ' ')}: {count}"
        for source, count in sorted(source_counts.items())
    ) or "none"

    console.print(
        "Detailed summary — "
        f"transactions: {transaction_count}; "
        f"product rows: {product_row_count}; "
        f"output rows: {len(rows)}"
    )
    console.print(
        "Store lookups — "
        f"unique SKUs: {len(skus)}; "
        f"normalised keys: {len({store.cache_key(sku) for sku in skus})}; "
        f"cache hits: {len(cache_hit_keys)}; "
        f"network requests: {len(network_keys)}"
    )
    console.print(f"Lookup status — {status_summary}")
    console.print(f"Classification sources — {source_summary}")


def load_transactions(json_path: str) -> list[dict]:
    input_path = Path(json_path)
    try:
        payload = json.loads(input_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PSNTransactionsError(
            f"Transaction JSON not found at {input_path}. "
            "Run `psn-transactions fetch` first."
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
    _validate_transactions_schema(payload, input_path)
    return payload


def _write_csv(
    *,
    json_path: str,
    csv_path: str,
    enriched: bool,
    paid_only: bool = False,
    refresh: bool = False,
    cache_only: bool = False,
    summary: bool = False,
    include_payment_details: bool = False,
) -> None:
    if refresh and cache_only:
        raise PSNTransactionsError("--refresh and --cache-only cannot be used together.")
    cache_mode = (
        store.CacheMode.REFRESH
        if refresh
        else store.CacheMode.ONLY
        if cache_only
        else store.CacheMode.USE
    )

    transactions = load_transactions(json_path)
    console.print(f"Loaded {len(transactions)} transactions from {json_path}")
    transaction_count = len(transactions)
    try:
        product_row_count = _product_row_count(transactions)
    except (AttributeError, TypeError) as exc:
        raise PSNTransactionsError(
            f"Transaction JSON at {json_path} has an unexpected purchase structure."
        ) from exc

    if paid_only:
        try:
            transactions, paid_counts = paid_only_transactions(transactions)
        except (AttributeError, TypeError) as exc:
            raise PSNTransactionsError(
                f"Transaction JSON at {json_path} has an unexpected purchase structure."
            ) from exc
        console.print(
            "Paid-only filter — "
            f"included: {paid_counts['included']}; "
            f"zero-cost skipped: {paid_counts['zero_cost']}; "
            f"negative-total skipped: {paid_counts['negative_total']}; "
            f"unknown-price skipped: {paid_counts['unknown_price']}; "
            f"non-product transactions skipped: {paid_counts['non_product']}"
        )

    cache = store.load_cache() if enriched else {}
    enrichment_results: dict[str, dict] = {}
    skus: set[str] = set()

    if enriched:
        try:
            skus = {
                product["skuId"]
                for transaction in transactions
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
            enrichment_results = store.enrich_skus(
                skus,
                cache,
                cache_mode=cache_mode,
            )

            if not cache_only:
                store.save_cache(cache)
            status_counts = Counter(
                result["status"] for result in enrichment_results.values()
            )
            cache_hits = sum(
                bool(result.get("cached"))
                for result in enrichment_results.values()
            )
            status_summary = ", ".join(
                f"{status.replace('_', ' ')}: {count}"
                for status, count in sorted(status_counts.items())
            )
            console.print(
                f"Enrichment results — cache hits: {cache_hits}; {status_summary}"
            )
            if cache_only:
                console.print("Cache-only mode — no Store requests were made")
            else:
                console.print(
                    f"✓ Cache saved ({len(cache['entries'])} locale-scoped records)"
                )

    try:
        rows = flatten_transactions(
            transactions,
            cache,
            enriched,
            enrichment_results=enrichment_results,
            include_payment_details=include_payment_details,
        )
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
        raise PSNTransactionsError(
            f"Transaction JSON at {json_path} has an unexpected transaction structure."
        ) from exc

    fields = list(ENRICHED_CSV_FIELDS if enriched else CORE_CSV_FIELDS)
    if include_payment_details:
        fields.extend(PAYMENT_DETAIL_FIELDS)
    atomic_write_csv(Path(csv_path), rows, fields)
    console.print(f"✓ Saved [bold]{len(rows)}[/bold] rows to {csv_path}")

    if summary:
        _print_enrichment_summary(
            transaction_count=transaction_count,
            product_row_count=product_row_count,
            rows=rows,
            skus=skus,
            results=enrichment_results,
        )


def export_csv(
    json_path: str = "psn_transactions_raw.json",
    csv_path: str = "psn_transactions.csv",
    *,
    include_payment_details: bool = False,
) -> None:
    """Export raw transaction JSON to the core CSV schema."""
    _write_csv(
        json_path=json_path,
        csv_path=csv_path,
        enriched=False,
        include_payment_details=include_payment_details,
    )


def enrich_csv(
    json_path: str = "psn_transactions_raw.json",
    csv_path: str = "psn_transactions_enriched.csv",
    *,
    paid_only: bool = False,
    refresh: bool = False,
    cache_only: bool = False,
    summary: bool = False,
    include_payment_details: bool = False,
) -> None:
    """Export transactions with Store metadata and classification."""
    _write_csv(
        json_path=json_path,
        csv_path=csv_path,
        enriched=True,
        paid_only=paid_only,
        refresh=refresh,
        cache_only=cache_only,
        summary=summary,
        include_payment_details=include_payment_details,
    )


# Compatibility aliases for existing internal callers.
def _flatten(
    txs: list,
    cache: dict,
    enrich: bool,
    enrichment_results: dict[str, dict] | None = None,
    include_payment_details: bool = False,
) -> list[dict]:
    return flatten_transactions(
        txs,
        cache,
        enriched=enrich,
        enrichment_results=enrichment_results,
        include_payment_details=include_payment_details,
    )


_paid_only_transactions = paid_only_transactions
_load_transactions = load_transactions
