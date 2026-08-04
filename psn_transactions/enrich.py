"""PlayStation Store metadata lookup and locale-scoped cache management."""

import json
import re
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, NotRequired, TypedDict

import requests
from rich.console import Console
from rich.progress import Progress, BarColumn, SpinnerColumn, TextColumn, TaskProgressColumn
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from psn_transactions import config as cfg
from psn_transactions.classify import normalise_token as _normalise_token
from psn_transactions.errors import PSNTransactionsError
from psn_transactions.paths import app_dir
from psn_transactions.storage import (
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


class CacheMode(str, Enum):
    USE = "use"
    REFRESH = "refresh"
    ONLY = "only"


class EnrichmentResult(TypedDict):
    status: str
    metadata: dict
    cached: bool
    detail: NotRequired[str]


def _chihiro_url(sku_base: str) -> str:
    locale = cfg.get_locale()
    country, lang = cfg.locale_parts(locale)
    return _CHIHIRO_TEMPLATE.format(country=country, lang=lang, sku=sku_base)

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
            "Remove it and rerun `psn-transactions enrich`."
        )
    return cache


def _save_cache(cache: dict) -> None:
    secure_private_directory(SKU_CACHE_FILE.parent, "SKU cache")
    atomic_write_json(SKU_CACHE_FILE, cache, description="SKU cache")
    secure_private_file(SKU_CACHE_FILE, "SKU cache")


def _sku_base(sku: str) -> str:
    """Strip regional variant suffix (e.g. -E001): EP1006-PPSA14382_00-XXX-E001 -> EP1006-PPSA14382_00-XXX"""
    return re.sub(r"-[A-Z]\d{3}$", "", sku)


def _cached_result(
    sku: str,
    cache: dict,
    now: datetime | None = None,
) -> EnrichmentResult | None:
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

    detail = record.get("detail")
    if not isinstance(detail, str):
        detail = {
            "not_found": "HTTP 404",
            "no_metadata": "Cached response contained no supported metadata",
        }.get(status, "")
    return {
        "status": status,
        "metadata": metadata,
        "cached": True,
        "detail": detail,
    }


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


def _record_cache_result(
    sku: str,
    cache: dict,
    status: str,
    metadata: dict,
    detail: str = "",
) -> None:
    record = {
        "status": status,
        "fetched_at": _format_cache_time(_utc_now()),
        "metadata": metadata,
    }
    if detail:
        record["detail"] = detail
    cache["entries"][_cache_key(sku)] = record


def _record_lookup_result(sku: str, cache: dict, result: EnrichmentResult) -> None:
    if result.get("status") in {"success", "not_found", "no_metadata"}:
        _record_cache_result(
            sku,
            cache,
            result["status"],
            result.get("metadata") or {},
            result.get("detail") or "",
        )


def _fetch_sku(
    sku: str,
    session: requests.Session | None = None,
) -> EnrichmentResult:
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
            return {
                "status": "not_found",
                "metadata": {},
                "cached": False,
                "detail": "HTTP 404",
            }
        if response.status_code == 204:
            return {
                "status": "no_metadata",
                "metadata": {},
                "cached": False,
                "detail": "HTTP 204",
            }
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
        result = {"status": status, "metadata": metadata, "cached": False}
        if status == "no_metadata":
            result["detail"] = "Store response contained no supported metadata"
        return result
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
) -> EnrichmentResult:
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


def _enrich_skus(
    skus: set[str],
    cache: dict,
    *,
    cache_mode: CacheMode = CacheMode.USE,
) -> dict[str, EnrichmentResult]:
    results: dict[str, EnrichmentResult] = {}
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
            if cached is not None and cache_mode is not CacheMode.REFRESH:
                for sku in variants:
                    results[sku] = cached
                progress.advance(task, len(variants))
                continue
            if cache_mode is CacheMode.ONLY:
                result: EnrichmentResult = {
                    "status": "cache_miss",
                    "metadata": {},
                    "cached": False,
                    "detail": "No reusable cached metadata",
                }
                for sku in variants:
                    results[sku] = result
                progress.advance(task, len(variants))
            else:
                pending.append((representative, variants))

        completed_requests = 0

        def record_completed(
            representative: str,
            variants: list[str],
            result: EnrichmentResult,
        ) -> None:
            nonlocal completed_requests
            _record_lookup_result(representative, cache, result)
            for sku in variants:
                results[sku] = result
            completed_requests += 1
            progress.advance(task, len(variants))
            if completed_requests % 20 == 0:
                _save_cache(cache)

        if pending:
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


# Public names used by the export coordinator. Underscored aliases remain for
# focused cache and Store-client tests.
cache_key = _cache_key
cached_result = _cached_result
metadata_is_useful = _metadata_is_useful
load_cache = _load_cache
save_cache = _save_cache
enrich_skus = _enrich_skus
