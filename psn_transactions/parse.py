"""Compatibility façade for the former combined parsing module.

New code should import classification, Store enrichment or CSV export helpers
from their dedicated modules.
"""

from psn_transactions.classify import (
    _classify,
    _classify_detailed,
    _normalise_token,
    _transaction_category,
)
from psn_transactions.enrich import (
    _cache_key,
    _cached_result,
    _empty_cache,
    _enrich_skus,
    _fetch_sku,
    _group_skus,
    _load_cache,
    _lookup_sku,
    _metadata_is_useful,
    _normalise_store_metadata,
    _record_cache_result,
    _save_cache,
    _sku_base,
)
from psn_transactions.export import (
    CORE_CSV_FIELDS,
    ENRICHED_CSV_FIELDS,
    _flatten,
    _load_transactions,
    _paid_only_transactions,
    enrich_csv,
    export_csv,
)


def export(
    json_path: str = "psn_transactions_raw.json",
    csv_path: str = "psn_transactions.csv",
    enrich: bool = False,
    paid_only: bool = False,
    refresh: bool = False,
    cache_only: bool = False,
    summary: bool = False,
) -> None:
    """Compatibility wrapper around the explicit CSV export entry points."""
    if enrich:
        enrich_csv(
            json_path=json_path,
            csv_path=csv_path,
            paid_only=paid_only,
            refresh=refresh,
            cache_only=cache_only,
            summary=summary,
        )
        return
    if paid_only or refresh or cache_only or summary:
        from psn_transactions.errors import PSNTransactionsError

        raise PSNTransactionsError(
            "Paid-only and cache options are available only through "
            "`psn-transactions enrich`."
        )
    export_csv(json_path=json_path, csv_path=csv_path)
