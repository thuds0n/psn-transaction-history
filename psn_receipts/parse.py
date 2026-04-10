import csv
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from rich.console import Console
from rich.progress import Progress, BarColumn, SpinnerColumn, TextColumn, TaskProgressColumn

from psn_receipts import config as cfg

SKU_CACHE_FILE = Path.home() / ".psn-receipts" / "sku_cache.json"
_CHIHIRO_TEMPLATE = "https://store.playstation.com/store/api/chihiro/00_09_000/container/{country}/{lang}/999/{sku}"


def _chihiro_url(sku_base: str) -> str:
    locale = cfg.load().get("locale", "en-au")
    country, lang = cfg.locale_parts(locale)
    return _CHIHIRO_TEMPLATE.format(country=country, lang=lang, sku=sku_base)

CSV_FIELDS = [
    "date", "transaction_id", "product", "category", "content_type",
    "paid", "original", "discount", "tax", "is_ps_plus", "sku",
    "payment", "card_last4",
]

console = Console()


# ---------------------------------------------------------------------------
# SKU lookup
# ---------------------------------------------------------------------------

def _load_cache() -> dict:
    if SKU_CACHE_FILE.exists():
        return json.loads(SKU_CACHE_FILE.read_text())
    return {}


def _save_cache(cache: dict) -> None:
    SKU_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    SKU_CACHE_FILE.write_text(json.dumps(cache, indent=2))


def _sku_base(sku: str) -> str:
    """Strip regional variant suffix (e.g. -E001): EP1006-PPSA14382_00-XXX-E001 -> EP1006-PPSA14382_00-XXX"""
    return re.sub(r"-[A-Z]\d{3}$", "", sku)


def _lookup_sku(sku: str, cache: dict) -> dict:
    if not sku:
        return {}
    if sku in cache:
        return cache[sku]

    url = _chihiro_url(_sku_base(sku))
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            data = r.json()
            attrs = data.get("attributes", {}) or data
            gct = (attrs.get("game_content_type") or "").upper()
            info = {
                "content_type": gct,
                "top_category": attrs.get("top_category", ""),
                "is_addon": gct in {"ADDON", "DLC", "ADD_ON", "ADD_ON_CONTENT"},
                "is_bundle": "BUNDLE" in gct,
            }
        else:
            info = {"error": r.status_code}
    except Exception as e:
        info = {"error": str(e)}

    cache[sku] = info
    time.sleep(0.25)
    return info


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _classify(name: str, sku: str, tx_total: int, tx_original: int, info: dict) -> tuple[str, bool]:
    """Returns (category, is_ps_plus)."""
    name_upper = name.upper()
    content_type = (info.get("content_type") or "").upper()

    # PS Plus Pack: branded bundle (check name first)
    if "PLAYSTATION PLUS" in name_upper or "PLAYSTATION®PLUS" in name_upper:
        return "PS Plus Pack", True

    # PS Plus Monthly: free game claimed via subscription
    if tx_total == 0 and tx_original > 0:
        return "PS Plus Monthly", True

    # API content-type classification (trusted when present)
    if info.get("is_addon") or content_type in {"ADDON", "DLC", "ADD_ON", "ADD_ON_CONTENT"}:
        return "DLC / Add-on", False

    if content_type == "BUNDLE" or info.get("is_bundle"):
        return "Bundle", False

    if content_type in {"GAME", "FULL_GAME", "PS5_GAME", "PS4_GAME"}:
        return "Full Game", False

    if content_type in {"CURRENCY", "VC", "INGAME_CURRENCY"}:
        return "In-Game Currency", False

    # Keyword heuristics (when API returned no useful content_type)
    if any(k in name.lower() for k in ["season pass", "pack", "skin", "costume", "dlc"]):
        return "DLC / Add-on", False

    # SKU pattern fallback: standard game SKU format with no other signal
    if re.match(r"^[A-Z]{2}\d{4}-[A-Z]{4}\d{5}_00-", sku):
        return "Full Game", False

    return "Other", False


# ---------------------------------------------------------------------------
# Flattening
# ---------------------------------------------------------------------------

def _flatten(txs: list, cache: dict, enrich: bool) -> list:
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
        tx_original = pd.get("originalPrice", 0)
        products = pd.get("productPurchases") or []

        if not products:
            # Wallet top-up, subscription charge, or refund with no product list
            rows.append({
                "date": date_str,
                "transaction_id": tx_id,
                "product": tx_type or "",
                "category": "Other" if enrich else "",
                "content_type": "",
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
            info = cache.get(sku, {}) if enrich else {}

            if enrich:
                category, is_ps_plus = _classify(name, sku, tx_total, tx_original, info)
            else:
                category, is_ps_plus = "", ""

            rows.append({
                "date": date_str,
                "transaction_id": tx_id,
                "product": name,
                "category": category,
                "content_type": info.get("content_type", ""),
                "paid": p.get("totalFormatted") or p.get("displayOfPrice", ""),
                "original": p.get("originalPriceFormatted", ""),
                "discount": p.get("discountFormatted", ""),
                "tax": p.get("taxFormatted", ""),
                "is_ps_plus": is_ps_plus,
                "sku": sku,
                "payment": payment,
                "card_last4": card_last4,
            })

    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def export(
    json_path: str = "psn_history_full.json",
    csv_path: str = "psn_history_enriched.csv",
    enrich: bool = False,
) -> None:
    txs = json.loads(Path(json_path).read_text())
    console.print(f"Loaded {len(txs)} transactions from {json_path}")

    cache = _load_cache()

    if enrich:
        # Collect unique SKUs not yet in cache
        skus = {
            p["skuId"]
            for t in txs
            for p in (t.get("purchaseDetails") or {}).get("productPurchases", [])
            if p.get("skuId") and p["skuId"] not in cache
        }

        if skus:
            console.print(f"Looking up {len(skus)} new SKUs...")
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                console=console,
            ) as progress:
                task = progress.add_task("SKU lookup", total=len(skus))
                for i, sku in enumerate(sorted(skus), 1):
                    _lookup_sku(sku, cache)
                    progress.advance(task)
                    if i % 20 == 0:
                        _save_cache(cache)

            _save_cache(cache)
            console.print(f"✓ Cache saved ({len(cache)} SKUs)")

    rows = _flatten(txs, cache, enrich)

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    console.print(f"✓ Saved [bold]{len(rows)}[/bold] rows to {csv_path}")
