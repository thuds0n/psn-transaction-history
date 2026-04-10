"""Tests for psn_receipts parsing and classification logic."""

import pytest

from psn_receipts.parse import _classify, _flatten, _sku_base, CSV_FIELDS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_tx(
    tx_id="TX001",
    date="2025-01-15T10:00:00.000Z",
    tx_total=0,
    tx_original=0,
    products=None,
    charge_method="",
    charge_display="",
):
    """Build a minimal transaction dict matching the PSN GraphQL shape."""
    return {
        "id": tx_id,
        "date": date,
        "transactionType": "PRODUCT_PURCHASE",
        "displayOfTransactionValue": "$0.00",
        "chargeDetails": (
            [{"paymentMethod": charge_method, "paymentDescriptionDisplay": charge_display}]
            if charge_method else []
        ),
        "purchaseDetails": {
            "total": tx_total,
            "originalPrice": tx_original,
            "displayOfOriginalPrice": f"${tx_original / 100:.2f}",
            "displayOfDiscount": "$0.00",
            "displayOfTax": "$0.00",
            "productPurchases": products or [],
        },
    }


def make_product(name, sku, paid_cents=0, original_cents=0):
    paid = f"${paid_cents / 100:.2f}"
    orig = f"${original_cents / 100:.2f}"
    return {
        "productName": name,
        "skuId": sku,
        "totalFormatted": paid,
        "originalPriceFormatted": orig,
        "discountFormatted": "$0.00",
        "taxFormatted": "$0.00",
        "displayOfPrice": paid,
        "total": paid_cents,
        "originalPrice": original_cents,
    }


# ---------------------------------------------------------------------------
# _sku_base
# ---------------------------------------------------------------------------

class TestSkuBase:
    def test_strips_trailing_region_suffix(self):
        assert _sku_base("EP1006-PPSA14382_00-XXX-E001") == "EP1006-PPSA14382_00-XXX"

    def test_strips_e001_variant(self):
        assert _sku_base("UP0006-CUSA37423_00-REWARDPACK300000-E001") == "UP0006-CUSA37423_00-REWARDPACK300000"

    def test_no_suffix_leaves_sku_unchanged(self):
        assert _sku_base("UP0006-CUSA37423_00-REWARDPACK300000") == "UP0006-CUSA37423_00-REWARDPACK300000"


# ---------------------------------------------------------------------------
# _classify
# ---------------------------------------------------------------------------

class TestClassify:
    def test_ps_plus_pack_by_name_registered_trademark(self):
        category, is_ps_plus = _classify(
            "Marathon Digital Bundle for PlayStation®Plus",
            "UP1234-PPSA00001_00-BUNDLE001-E001",
            tx_total=0, tx_original=0, info={},
        )
        assert category == "PS Plus Pack"
        assert is_ps_plus is True

    def test_ps_plus_pack_by_name_plain(self):
        category, is_ps_plus = _classify(
            "PUBG 2026 PlayStation Plus Exclusive Bundle",
            "UP1234-CUSA00001_00-BUNDLE001-E001",
            tx_total=0, tx_original=0, info={},
        )
        assert category == "PS Plus Pack"
        assert is_ps_plus is True

    def test_ps_plus_monthly_free_with_nonzero_original(self):
        category, is_ps_plus = _classify(
            "Some Game",
            "UP1234-CUSA12345_00-SOMEGAME0000001-E001",
            tx_total=0, tx_original=1999, info={},
        )
        assert category == "PS Plus Monthly"
        assert is_ps_plus is True

    def test_full_game_by_full_game_content_type(self):
        category, is_ps_plus = _classify(
            "Disney Pixar Buzz Lightyear",
            "EP0001-PPSA12345_00-BUZZLIGHTYEAR001",
            tx_total=537, tx_original=537,
            info={"content_type": "FULL_GAME"},
        )
        assert category == "Full Game"
        assert is_ps_plus is False

    def test_full_game_by_ps5_game_content_type(self):
        category, is_ps_plus = _classify(
            "Some Game",
            "EP0001-PPSA12345_00-SOMEGAME001",
            tx_total=6999, tx_original=6999,
            info={"content_type": "PS5_GAME"},
        )
        assert category == "Full Game"
        assert is_ps_plus is False

    def test_full_game_by_sku_pattern_no_api_info(self):
        # Standard two-part AU SKU pattern should be recognized without API lookup
        category, is_ps_plus = _classify(
            "Some Game",
            "EP1234-PPSA12345_00-SOMEGAME",
            tx_total=1000, tx_original=1000, info={},
        )
        assert category == "Full Game"
        assert is_ps_plus is False

    def test_dlc_by_addon_content_type(self):
        category, is_ps_plus = _classify(
            "Character Pack",
            "UP1234-CUSA00001_00-CHARPACK001",
            tx_total=499, tx_original=499,
            info={"content_type": "ADDON", "is_addon": True},
        )
        assert category == "DLC / Add-on"
        assert is_ps_plus is False

    def test_dlc_by_keyword_pack_fallback(self):
        category, is_ps_plus = _classify(
            "Awesome Skin Pack",
            "UP1234-CUSA00001_00-SKINPACK001",
            tx_total=199, tx_original=199, info={},
        )
        assert category == "DLC / Add-on"
        assert is_ps_plus is False

    def test_bundle_by_content_type(self):
        category, is_ps_plus = _classify(
            "Game of the Year Edition",
            "UP1234-CUSA00001_00-GOTY001",
            tx_total=2999, tx_original=2999,
            info={"content_type": "BUNDLE", "is_bundle": True},
        )
        assert category == "Bundle"
        assert is_ps_plus is False

    def test_in_game_currency(self):
        category, is_ps_plus = _classify(
            "1000 Gold Coins",
            "UP1234-CUSA00001_00-COINS1000",
            tx_total=299, tx_original=299,
            info={"content_type": "CURRENCY"},
        )
        assert category == "In-Game Currency"
        assert is_ps_plus is False

    def test_other_fallback_for_unknown(self):
        # Non-standard SKU format that doesn't match any pattern
        category, is_ps_plus = _classify(
            "Unknown Item",
            "MISC-NONSTANDARDSKU",
            tx_total=100, tx_original=100, info={},
        )
        assert category == "Other"
        assert is_ps_plus is False


# ---------------------------------------------------------------------------
# _flatten
# ---------------------------------------------------------------------------

class TestFlatten:
    def test_single_product_maps_to_one_row(self):
        product = make_product("Buzz Lightyear", "EP0001-PPSA12345_00-BUZZ-E001", paid_cents=537, original_cents=537)
        tx = make_tx(tx_total=537, tx_original=537, products=[product],
                     charge_method="CREDIT_CARD", charge_display="****1234")
        rows = _flatten([tx], cache={}, enrich=False)
        assert len(rows) == 1
        assert rows[0]["product"] == "Buzz Lightyear"
        assert rows[0]["paid"] == "$5.37"
        assert rows[0]["payment"] == "CREDIT_CARD"
        assert rows[0]["card_last4"] == "1234"

    def test_multiple_products_per_transaction_expand_to_rows(self):
        products = [
            make_product("Game A", "UP0001-CUSA00001_00-GAMEA-E001"),
            make_product("Game B", "UP0001-CUSA00002_00-GAMEB-E001"),
        ]
        tx = make_tx(products=products)
        rows = _flatten([tx], cache={}, enrich=False)
        assert len(rows) == 2
        assert {r["product"] for r in rows} == {"Game A", "Game B"}

    def test_no_products_creates_single_placeholder_row(self):
        tx = make_tx(products=[])
        rows = _flatten([tx], cache={}, enrich=False)
        assert len(rows) == 1
        assert rows[0]["sku"] == ""

    def test_rows_sorted_newest_first(self):
        tx_old = make_tx(tx_id="T1", date="2025-01-01T10:00:00.000Z",
                         products=[make_product("Old Game", "UP001-CUSA00001_00-OLD-E001")])
        tx_new = make_tx(tx_id="T2", date="2025-06-01T10:00:00.000Z",
                         products=[make_product("New Game", "UP001-CUSA00002_00-NEW-E001")])
        rows = _flatten([tx_old, tx_new], cache={}, enrich=False)
        assert rows[0]["product"] == "New Game"
        assert rows[1]["product"] == "Old Game"

    def test_enrich_true_classifies_full_game(self):
        product = make_product("Disney Pixar Buzz Lightyear", "EP0001-PPSA12345_00-BUZZ-E001",
                               paid_cents=537, original_cents=537)
        tx = make_tx(tx_total=537, tx_original=537, products=[product])
        cache = {"EP0001-PPSA12345_00-BUZZ-E001": {"content_type": "FULL_GAME", "is_addon": False, "is_bundle": False}}
        rows = _flatten([tx], cache=cache, enrich=True)
        assert rows[0]["category"] == "Full Game"
        assert rows[0]["is_ps_plus"] is False

    def test_enrich_true_classifies_ps_plus_pack(self):
        product = make_product(
            "Marathon Digital Bundle for PlayStation®Plus",
            "UP1234-PPSA00001_00-MARATHON-E001",
            paid_cents=0,
        )
        tx = make_tx(tx_total=0, tx_original=0, products=[product])
        rows = _flatten([tx], cache={}, enrich=True)
        assert rows[0]["category"] == "PS Plus Pack"
        assert rows[0]["is_ps_plus"] is True

    def test_enrich_false_leaves_enriched_columns_empty(self):
        product = make_product("Some Game", "UP001-CUSA00001_00-GAME-E001", paid_cents=999, original_cents=999)
        tx = make_tx(tx_total=999, tx_original=999, products=[product])
        rows = _flatten([tx], cache={}, enrich=False)
        assert rows[0]["category"] == ""
        assert rows[0]["content_type"] == ""
        assert rows[0]["is_ps_plus"] == ""

    def test_date_formatted_as_yyyy_mm_dd_hhmm(self):
        product = make_product("Some Game", "UP001-CUSA00001_00-GAME-E001")
        tx = make_tx(date="2025-03-30T09:31:25.285Z", products=[product])
        rows = _flatten([tx], cache={}, enrich=False)
        assert rows[0]["date"] == "2025-03-30 09:31"

    def test_all_csv_fields_present_in_output(self):
        product = make_product("Some Game", "UP001-CUSA00001_00-GAME-E001")
        tx = make_tx(products=[product])
        rows = _flatten([tx], cache={}, enrich=False)
        assert set(rows[0].keys()) == set(CSV_FIELDS)

    def test_transaction_id_populated(self):
        product = make_product("Some Game", "UP001-CUSA00001_00-GAME-E001")
        tx = make_tx(tx_id="787042153182277", products=[product])
        rows = _flatten([tx], cache={}, enrich=False)
        assert rows[0]["transaction_id"] == "787042153182277"
