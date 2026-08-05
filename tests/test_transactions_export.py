"""Tests for PSN transaction parsing and classification logic."""

import csv
import json
import shutil
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
from typer.testing import CliRunner

from psn_transactions import enrich as store_enrich
from psn_transactions import export as csv_export
from psn_transactions import storage
from psn_transactions.cli import app
from psn_transactions.errors import PSNTransactionsError
from psn_transactions.classify import (
    _classify,
    _classify_detailed,
)
from psn_transactions.enrich import (
    _normalise_store_metadata,
    _sku_base,
)
from psn_transactions.export import (
    CORE_CSV_FIELDS,
    ENRICHED_CSV_FIELDS,
    PAYMENT_DETAIL_FIELDS,
    _flatten,
)


FIXTURES = Path(__file__).parent / "fixtures"


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
            "displayOfOriginalPrice": (
                f"${tx_original / 100:.2f}" if tx_original is not None else ""
            ),
            "displayOfDiscount": "$0.00",
            "displayOfTax": "$0.00",
            "productPurchases": products or [],
        },
    }


def make_product(
    name,
    sku,
    paid_cents=0,
    original_cents=0,
    sku_type="STANDARD",
):
    paid = f"${paid_cents / 100:.2f}"
    orig = f"${original_cents / 100:.2f}"
    return {
        "orderItemId": "ORDERITEM001",
        "productName": name,
        "skuId": sku,
        "skuType": sku_type,
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
            "Some Game - PlayStation Plus",
            "UP1234-CUSA12345_00-SOMEGAME0000001-E001",
            tx_total=0, tx_original=1999, info={},
        )
        assert category == "PS Plus Monthly"
        assert is_ps_plus is True

    def test_zero_price_without_ps_plus_evidence_is_not_assumed_plus(self):
        category, is_ps_plus = _classify(
            "Some Game",
            "UP1234-CUSA12345_00-SOMEGAME0000001-E001",
            tx_total=0, tx_original=1999, info={},
        )
        assert category == "Other"
        assert is_ps_plus is None

    def test_full_game_by_full_game_content_type(self):
        category, is_ps_plus = _classify(
            "Disney Pixar Buzz Lightyear",
            "EP0001-PPSA12345_00-BUZZLIGHTYEAR001",
            tx_total=537, tx_original=537,
            info={"content_type": "FULL_GAME"},
        )
        assert category == "Full Game"
        assert is_ps_plus is None

    def test_full_game_by_ps5_game_content_type(self):
        category, is_ps_plus = _classify(
            "Some Game",
            "EP0001-PPSA12345_00-SOMEGAME001",
            tx_total=6999, tx_original=6999,
            info={"content_type": "PS5_GAME"},
        )
        assert category == "Full Game"
        assert is_ps_plus is None

    def test_standard_sku_pattern_is_not_assumed_to_be_full_game(self):
        category, is_ps_plus = _classify(
            "Some Game",
            "EP1234-PPSA12345_00-SOMEGAME",
            tx_total=1000, tx_original=1000, info={},
        )
        assert category == "Other"
        assert is_ps_plus is None

    def test_dlc_by_addon_content_type(self):
        category, is_ps_plus = _classify(
            "Character Pack",
            "UP1234-CUSA00001_00-CHARPACK001",
            tx_total=499, tx_original=499,
            info={"content_type": "ADDON", "is_addon": True},
        )
        assert category == "DLC / Add-on"
        assert is_ps_plus is None

    def test_dlc_by_keyword_pack_fallback(self):
        category, is_ps_plus = _classify(
            "Awesome Skin Pack",
            "UP1234-CUSA00001_00-SKINPACK001",
            tx_total=199, tx_original=199, info={},
        )
        assert category == "DLC / Add-on"
        assert is_ps_plus is None

    def test_bundle_by_content_type(self):
        category, is_ps_plus = _classify(
            "Game of the Year Edition",
            "UP1234-CUSA00001_00-GOTY001",
            tx_total=2999, tx_original=2999,
            info={"content_type": "BUNDLE", "is_bundle": True},
        )
        assert category == "Bundle"
        assert is_ps_plus is None

    def test_in_game_currency(self):
        category, is_ps_plus = _classify(
            "1000 Gold Coins",
            "UP1234-CUSA00001_00-COINS1000",
            tx_total=299, tx_original=299,
            info={"content_type": "CURRENCY"},
        )
        assert category == "In-Game Currency"
        assert is_ps_plus is None

    def test_other_fallback_for_unknown(self):
        # Non-standard SKU format that doesn't match any pattern
        category, is_ps_plus = _classify(
            "Unknown Item",
            "MISC-NONSTANDARDSKU",
            tx_total=100, tx_original=100, info={},
        )
        assert category == "Other"
        assert is_ps_plus is None

    def test_transaction_metadata_identifies_subscription(self):
        category, is_ps_plus, source, evidence = _classify_detailed(
            "Recurring membership",
            "SUBSCRIPTION-SKU",
            item_total=1299,
            item_original=1299,
            info={},
            sku_type="SUBSCRIPTION",
        )
        assert category == "Subscription"
        assert is_ps_plus is None
        assert source == "transaction"
        assert evidence == "sku_type=SUBSCRIPTION"

    def test_heuristic_classification_names_the_matching_evidence(self):
        category, is_ps_plus, source, evidence = _classify_detailed(
            "Example Season Pass",
            "UP001-EXAMPLE",
            item_total=999,
            item_original=999,
            info={},
        )

        assert category == "DLC / Add-on"
        assert is_ps_plus is None
        assert source == "heuristic"
        assert evidence == 'product name contains "season pass"'


class TestStoreMetadataNormalisation:
    def test_current_chihiro_shape_is_normalised(self):
        metadata = _normalise_store_metadata(
            {
                "game_contentType": "Add-On",
                "gameContentTypesList": [{"key": "ADD-ON", "name": "Add-On"}],
                "content_type": "1",
                "top_category": "add_on",
                "playable_platform": ["PS4™", "PS5"],
                "provider_name": "Example Publisher",
                "release_date": "2025-01-01T00:00:00Z",
            }
        )

        assert metadata == {
            "content_type": "ADD_ON",
            "top_category": "add_on",
            "platform": "PS4™; PS5",
            "publisher": "Example Publisher",
            "release_date": "2025-01-01T00:00:00Z",
        }

    def test_numeric_container_content_type_is_not_treated_as_category(self):
        metadata = _normalise_store_metadata({"content_type": "1"})

        assert metadata["content_type"] == ""

# ---------------------------------------------------------------------------
# _flatten
# ---------------------------------------------------------------------------

class TestFlatten:
    def test_single_product_maps_to_one_row_without_payment_details_by_default(self):
        product = make_product("Buzz Lightyear", "EP0001-PPSA12345_00-BUZZ-E001", paid_cents=537, original_cents=537)
        tx = make_tx(tx_total=537, tx_original=537, products=[product],
                     charge_method="CREDIT_CARD", charge_display="****1234")
        rows = _flatten([tx], cache={}, enrich=False)
        assert len(rows) == 1
        assert rows[0]["product"] == "Buzz Lightyear"
        assert rows[0]["paid"] == "$5.37"
        assert "payment" not in rows[0]
        assert "card_last4" not in rows[0]

    def test_payment_details_can_be_included_explicitly(self):
        product = make_product(
            "Buzz Lightyear",
            "EP0001-PPSA12345_00-BUZZ-E001",
            paid_cents=537,
            original_cents=537,
        )
        tx = make_tx(
            products=[product],
            charge_method="CREDIT_CARD",
            charge_display="****1234",
        )

        rows = _flatten(
            [tx],
            cache={},
            enrich=False,
            include_payment_details=True,
        )

        assert rows[0]["payment"] == "CREDIT_CARD"
        assert rows[0]["card_last4"] == "1234"
        assert list(rows[0]) == CORE_CSV_FIELDS + PAYMENT_DETAIL_FIELDS

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
        assert rows[0]["is_ps_plus"] == ""
        assert rows[0]["enrichment_status"] == "success"
        assert rows[0]["classification_source"] == "store_api"

    def test_product_row_includes_stable_item_id_and_numeric_amounts(self):
        product = make_product(
            "Some Game",
            "UP001-CUSA00001_00-GAME-E001",
            paid_cents=999,
            original_cents=1299,
        )
        product["orderItemId"] = "ORDER001"
        product["discount"] = 300
        product["tax"] = 100
        tx = make_tx(tx_total=999, products=[product])

        rows = _flatten([tx], cache={}, enrich=False)

        assert rows[0]["order_item_id"] == "ORDER001"
        assert rows[0]["paid_minor"] == 999
        assert rows[0]["original_minor"] == 1299
        assert rows[0]["discount_minor"] == 300
        assert rows[0]["tax_minor"] == 100

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

    def test_basic_rows_omit_enriched_columns(self):
        product = make_product("Some Game", "UP001-CUSA00001_00-GAME-E001", paid_cents=999, original_cents=999)
        tx = make_tx(tx_total=999, tx_original=999, products=[product])
        rows = _flatten([tx], cache={}, enrich=False)
        assert "category" not in rows[0]
        assert "content_type" not in rows[0]
        assert "is_ps_plus" not in rows[0]
        assert "enrichment_status" not in rows[0]
        assert "classification_source" not in rows[0]

    def test_live_like_nullable_transaction_original_price_does_not_crash(self):
        product = make_product(
            "Some Game - PlayStation Plus",
            "UP001-CUSA00001_00-GAME-E001",
            paid_cents=0,
            original_cents=1999,
        )
        tx = make_tx(tx_total=0, tx_original=None, products=[product])

        rows = _flatten([tx], cache={}, enrich=True)

        assert rows[0]["category"] == "PS Plus Monthly"
        assert rows[0]["is_ps_plus"] is True
        assert rows[0]["classification_source"] == "product_name"

    def test_ps_plus_classification_uses_item_total_in_mixed_transaction(self):
        plus_item = make_product(
            "Some Game - PlayStation Plus",
            "UP001-CUSA00001_00-PLUS-E001",
            paid_cents=0,
            original_cents=1999,
        )
        paid_item = make_product(
            "Paid Item",
            "UP001-CUSA00002_00-PAID-E001",
            paid_cents=999,
            original_cents=999,
        )
        tx = make_tx(tx_total=999, products=[plus_item, paid_item])

        rows = _flatten([tx], cache={}, enrich=True)

        plus_row = next(row for row in rows if row["product"].endswith("Plus"))
        assert plus_row["category"] == "PS Plus Monthly"
        assert plus_row["is_ps_plus"] is True

    def test_date_formatted_as_yyyy_mm_dd_hhmm(self):
        product = make_product("Some Game", "UP001-CUSA00001_00-GAME-E001")
        tx = make_tx(date="2025-03-30T09:31:25.285Z", products=[product])
        rows = _flatten([tx], cache={}, enrich=False)
        assert rows[0]["date"] == "2025-03-30 09:31"

    def test_core_fields_present_in_basic_output(self):
        product = make_product("Some Game", "UP001-CUSA00001_00-GAME-E001")
        tx = make_tx(products=[product])
        rows = _flatten([tx], cache={}, enrich=False)
        assert list(rows[0]) == CORE_CSV_FIELDS

    def test_enriched_fields_present_in_enriched_output(self):
        product = make_product("Some Game", "UP001-CUSA00001_00-GAME-E001")
        tx = make_tx(products=[product])

        rows = _flatten([tx], cache={}, enrich=True)

        assert list(rows[0]) == ENRICHED_CSV_FIELDS

    def test_transaction_id_populated(self):
        product = make_product("Some Game", "UP001-CUSA00001_00-GAME-E001")
        tx = make_tx(tx_id="787042153182277", products=[product])
        rows = _flatten([tx], cache={}, enrich=False)
        assert rows[0]["transaction_id"] == "787042153182277"


# ---------------------------------------------------------------------------
# Export and cache integrity
# ---------------------------------------------------------------------------

class TestExportIntegrity:
    def test_export_writes_complete_csv(self, tmp_path):
        input_path = tmp_path / "transactions.json"
        output_path = tmp_path / "transactions.csv"
        input_path.write_text(
            json.dumps(
                [
                    make_tx(
                        products=[
                            make_product(
                                "Some Game",
                                "UP001-CUSA00001_00-GAME-E001",
                            )
                        ]
                    )
                ]
            )
        )

        csv_export.export_csv(json_path=str(input_path), csv_path=str(output_path))

        with output_path.open(newline="", encoding="utf-8") as output_file:
            rows = list(csv.DictReader(output_file))
        assert len(rows) == 1
        assert rows[0]["product"] == "Some Game"
        assert list(rows[0]) == CORE_CSV_FIELDS

    def test_anonymised_raw_fixture_to_basic_export(self, tmp_path):
        output_path = tmp_path / "transactions.csv"

        csv_export.export_csv(
            json_path=str(FIXTURES / "anonymised_transactions_raw.json"),
            csv_path=str(output_path),
        )

        assert output_path.read_text().splitlines() == (
            FIXTURES / "anonymised_basic.csv"
        ).read_text().splitlines()

    def test_export_includes_payment_columns_only_when_requested(self, tmp_path):
        input_path = tmp_path / "transactions.json"
        output_path = tmp_path / "transactions.csv"
        input_path.write_text(
            json.dumps(
                [
                    make_tx(
                        products=[make_product("Example Product", "EXAMPLE-SKU")],
                        charge_method="EXAMPLE_PAYMENT",
                        charge_display="****0000",
                    )
                ]
            )
        )

        csv_export.export_csv(
            json_path=str(input_path),
            csv_path=str(output_path),
            include_payment_details=True,
        )

        with output_path.open(newline="", encoding="utf-8") as output_file:
            row = next(csv.DictReader(output_file))
        assert list(row) == CORE_CSV_FIELDS + PAYMENT_DETAIL_FIELDS
        assert row["payment"] == "EXAMPLE_PAYMENT"
        assert row["card_last4"] == "0000"

    @pytest.mark.parametrize(
        ("paid_only", "expected_name"),
        [
            (False, "anonymised_cache_only_enriched.csv"),
            (True, "anonymised_paid_only_enriched.csv"),
        ],
    )
    def test_anonymised_raw_fixture_to_cache_only_enriched_export(
        self, tmp_path, monkeypatch, paid_only, expected_name
    ):
        output_path = tmp_path / "transactions.csv"
        cache_file = tmp_path / ".psn-transactions" / "sku_cache.json"
        cache_file.parent.mkdir()
        shutil.copyfile(FIXTURES / "anonymised_sku_cache.json", cache_file)
        monkeypatch.setattr(store_enrich, "SKU_CACHE_FILE", cache_file)
        monkeypatch.setattr(store_enrich.cfg, "get_locale", lambda: "en-us")
        monkeypatch.setattr(
            store_enrich,
            "_store_session",
            lambda: (_ for _ in ()).throw(AssertionError("network session opened")),
        )

        csv_export.enrich_csv(
            json_path=str(FIXTURES / "anonymised_transactions_raw.json"),
            csv_path=str(output_path),
            cache_only=True,
            paid_only=paid_only,
        )

        assert output_path.read_text().splitlines() == (
            FIXTURES / expected_name
        ).read_text().splitlines()

    @pytest.mark.parametrize(
        ("contents", "message"),
        [
            ("not-json", "Could not read transaction JSON"),
            (json.dumps({"id": "TX001"}), "must contain a list"),
            (json.dumps(["not-an-object"]), "non-object transaction"),
        ],
    )
    def test_export_rejects_malformed_transaction_json(
        self, tmp_path, contents, message
    ):
        input_path = tmp_path / "transactions.json"
        input_path.write_text(contents)

        with pytest.raises(PSNTransactionsError, match=message):
            csv_export.export_csv(
                json_path=str(input_path),
                csv_path=str(tmp_path / "transactions.csv"),
            )

    @pytest.mark.parametrize(
        ("mutate", "message"),
        [
            (
                lambda transaction: transaction.update(purchaseDetails=[]),
                r"transaction\[0\]\.purchaseDetails",
            ),
            (
                lambda transaction: transaction["purchaseDetails"].update(
                    productPurchases={}
                ),
                r"transaction\[0\]\.purchaseDetails\.productPurchases",
            ),
            (
                lambda transaction: transaction["purchaseDetails"].update(
                    productPurchases=["not-an-object"]
                ),
                r"productPurchases\[0\].*expected an object",
            ),
            (
                lambda transaction: transaction["purchaseDetails"][
                    "productPurchases"
                ][0].update(orderItemId=123),
                r"productPurchases\[0\]\.orderItemId",
            ),
            (
                lambda transaction: transaction["purchaseDetails"][
                    "productPurchases"
                ][0].update(skuId=[]),
                r"productPurchases\[0\]\.skuId",
            ),
            (
                lambda transaction: transaction["purchaseDetails"][
                    "productPurchases"
                ][0].update(productName={}),
                r"productPurchases\[0\]\.productName",
            ),
            (
                lambda transaction: transaction["purchaseDetails"][
                    "productPurchases"
                ][0].update(total="$9.99"),
                r"productPurchases\[0\]\.total.*expected a number",
            ),
        ],
    )
    def test_export_reports_precise_schema_drift(
        self, tmp_path, mutate, message
    ):
        transaction = make_tx(
            products=[make_product("Example Product", "EXAMPLE-SKU-E001")]
        )
        mutate(transaction)
        input_path = tmp_path / "transactions.json"
        input_path.write_text(json.dumps([transaction]))

        with pytest.raises(PSNTransactionsError, match=message):
            csv_export.export_csv(
                json_path=str(input_path),
                csv_path=str(tmp_path / "transactions.csv"),
            )

    def test_missing_optional_product_data_is_reported_in_enrichment_detail(self):
        product = make_product("Example Product", "EXAMPLE-SKU-E001")
        product.pop("orderItemId")
        product.pop("productName")
        product.pop("total")

        rows = _flatten(
            [make_tx(products=[product])],
            cache={},
            enrich=True,
            enrichment_results={
                "EXAMPLE-SKU-E001": {
                    "status": "success",
                    "metadata": {"content_type": "FULL_GAME"},
                    "cached": True,
                }
            },
        )

        assert rows[0]["enrichment_status"] == "success"
        assert rows[0]["enrichment_detail"] == (
            "Transaction item missing optional fields: "
            "orderItemId, productName, total"
        )

    def test_export_failure_preserves_existing_csv(self, tmp_path, monkeypatch):
        input_path = tmp_path / "transactions.json"
        output_path = tmp_path / "transactions.csv"
        input_path.write_text(json.dumps([make_tx()]))
        output_path.write_text("existing export")

        monkeypatch.setattr(
            storage.os,
            "replace",
            lambda source, destination: (_ for _ in ()).throw(
                OSError("simulated disk failure")
            ),
        )

        with pytest.raises(PSNTransactionsError, match="Could not save CSV export"):
            csv_export.export_csv(
                json_path=str(input_path),
                csv_path=str(output_path),
            )

        assert output_path.read_text() == "existing export"
        assert list(tmp_path.glob(".transactions.csv.*.tmp")) == []

    def test_basic_export_does_not_depend_on_sku_cache(self, tmp_path, monkeypatch):
        input_path = tmp_path / "transactions.json"
        output_path = tmp_path / "transactions.csv"
        cache_file = tmp_path / "sku_cache.json"
        input_path.write_text(json.dumps([make_tx()]))
        cache_file.write_text("not-json")
        monkeypatch.setattr(store_enrich, "SKU_CACHE_FILE", cache_file)

        csv_export.export_csv(
            json_path=str(input_path),
            csv_path=str(output_path),
        )

        assert output_path.exists()

    def test_enriched_export_uses_current_store_shape_and_reports_status(
        self, tmp_path, monkeypatch, capsys
    ):
        input_path = tmp_path / "transactions.json"
        output_path = tmp_path / "transactions.csv"
        cache_file = tmp_path / ".psn-transactions" / "sku_cache.json"
        input_path.write_text(
            json.dumps(
                [
                    make_tx(
                        tx_total=999,
                        tx_original=None,
                        products=[
                            make_product(
                                "Some Add-on",
                                "UP001-CUSA00001_00-ADDON-E001",
                                paid_cents=999,
                                original_cents=999,
                            )
                        ],
                    )
                ]
            )
        )

        class FakeResponse:
            status_code = 200

            def json(self):
                return {
                    "game_contentType": "Add-On",
                    "top_category": "add_on",
                    "playable_platform": ["PS5"],
                    "provider_name": "Example Publisher",
                    "release_date": "2025-01-01T00:00:00Z",
                }

        class FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get(self, *args, **kwargs):
                return FakeResponse()

        monkeypatch.setattr(store_enrich, "SKU_CACHE_FILE", cache_file)
        monkeypatch.setattr(store_enrich.cfg, "get_locale", lambda: "en-us")
        monkeypatch.setattr(store_enrich, "_store_session", FakeSession)

        csv_export.enrich_csv(
            json_path=str(input_path),
            csv_path=str(output_path),
        )

        with output_path.open(newline="", encoding="utf-8") as output_file:
            row = next(csv.DictReader(output_file))
        assert list(row) == ENRICHED_CSV_FIELDS
        assert row["category"] == "DLC / Add-on"
        assert row["content_type"] == "ADD_ON"
        assert row["platform"] == "PS5"
        assert row["publisher"] == "Example Publisher"
        assert row["enrichment_status"] == "success"
        assert row["enrichment_detail"] == ""
        assert row["classification_source"] == "store_api"
        assert row["classification_evidence"] == "content_type=ADD_ON"
        assert "success: 1" in capsys.readouterr().out
        assert cache_file.exists()

    def test_paid_only_filters_rows_before_store_lookups(
        self, tmp_path, monkeypatch, capsys
    ):
        input_path = tmp_path / "transactions.json"
        output_path = tmp_path / "transactions.csv"
        cache_file = tmp_path / ".psn-transactions" / "sku_cache.json"
        paid_product = make_product(
            "Paid Item",
            "UP001-CUSA00001_00-PAID-E001",
            paid_cents=999,
            original_cents=999,
        )
        free_product = make_product(
            "Free Item",
            "UP001-CUSA00002_00-FREE-E001",
            paid_cents=0,
            original_cents=499,
        )
        refunded_product = make_product(
            "Refunded Item",
            "UP001-CUSA00003_00-REFUND-E001",
            paid_cents=-499,
            original_cents=499,
        )
        input_path.write_text(
            json.dumps(
                [
                    make_tx(
                        tx_total=999,
                        products=[paid_product, free_product, refunded_product],
                    ),
                    make_tx(tx_id="NONPRODUCT", products=[]),
                ]
            )
        )
        calls = []

        class FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_fetch(sku, session=None):
            calls.append(sku)
            return {
                "status": "success",
                "metadata": {"content_type": "FULL_GAME"},
                "cached": False,
            }

        monkeypatch.setattr(store_enrich, "SKU_CACHE_FILE", cache_file)
        monkeypatch.setattr(store_enrich.cfg, "get_locale", lambda: "en-us")
        monkeypatch.setattr(store_enrich, "_store_session", FakeSession)
        monkeypatch.setattr(store_enrich, "_fetch_sku", fake_fetch)

        csv_export.enrich_csv(
            json_path=str(input_path),
            csv_path=str(output_path),
            paid_only=True,
            summary=True,
        )

        with output_path.open(newline="", encoding="utf-8") as output_file:
            rows = list(csv.DictReader(output_file))
        assert len(rows) == 1
        assert rows[0]["product"] == "Paid Item"
        assert calls == ["UP001-CUSA00001_00-PAID-E001"]
        output = capsys.readouterr().out
        assert "included: 1" in output
        assert "zero-cost skipped: 1" in output
        assert "negative-total skipped: 1" in output
        assert "non-product transactions skipped: 1" in output
        assert "Detailed summary" in output
        assert "product rows: 3" in output
        assert "requests: 1" in output
        assert "Classification sources — store api: 1" in output

    def test_export_cli_uses_raw_input_without_enrichment(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            csv_export,
            "export_csv",
            lambda **kwargs: calls.append(kwargs),
        )

        result = CliRunner().invoke(app, ["export"])

        assert result.exit_code == 0
        assert calls == [
            {
                "json_path": "psn_transactions_raw.json",
                "csv_path": "psn_transactions.csv",
                "include_payment_details": False,
            }
        ]

    def test_enrich_cli_uses_separate_output(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            csv_export,
            "enrich_csv",
            lambda **kwargs: calls.append(kwargs),
        )

        result = CliRunner().invoke(app, ["enrich"])

        assert result.exit_code == 0
        assert calls == [
            {
                "json_path": "psn_transactions_raw.json",
                "csv_path": "psn_transactions_enriched.csv",
                "paid_only": False,
                "refresh": False,
                "cache_only": False,
                "summary": False,
                "include_payment_details": False,
            }
        ]

    def test_enrich_cli_forwards_paid_and_cache_options(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            csv_export,
            "enrich_csv",
            lambda **kwargs: calls.append(kwargs),
        )

        result = CliRunner().invoke(
            app,
            [
                "enrich",
                "--paid-only",
                "--refresh",
                "--summary",
                "--include-payment-details",
            ],
        )

        assert result.exit_code == 0
        assert calls == [
            {
                "json_path": "psn_transactions_raw.json",
                "csv_path": "psn_transactions_enriched.csv",
                "paid_only": True,
                "refresh": True,
                "cache_only": False,
                "summary": True,
                "include_payment_details": True,
            }
        ]

    def test_export_cli_forwards_payment_details_option(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            csv_export,
            "export_csv",
            lambda **kwargs: calls.append(kwargs),
        )

        result = CliRunner().invoke(
            app,
            ["export", "--include-payment-details"],
        )

        assert result.exit_code == 0
        assert calls[0]["include_payment_details"] is True

    def test_enrich_rejects_refresh_with_cache_only(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            csv_export,
            "enrich_csv",
            lambda **kwargs: calls.append(kwargs),
        )

        result = CliRunner().invoke(
            app,
            ["enrich", "--refresh", "--cache-only"],
        )

        assert result.exit_code == 1
        assert "cannot be used together" in result.output
        assert calls == []

    def test_export_rejects_removed_enrich_option(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            csv_export,
            "export_csv",
            lambda **kwargs: calls.append(kwargs),
        )

        result = CliRunner().invoke(app, ["export", "--enrich"])

        assert result.exit_code != 0
        assert "No such option: --enrich" in result.output
        assert calls == []

    @pytest.mark.parametrize(
        ("command", "output_name"),
        [
            ("export", "psn_transactions.csv"),
            ("enrich", "psn_transactions_enriched.csv"),
        ],
    )
    def test_commands_do_not_fall_back_to_old_raw_filename(
        self, tmp_path, monkeypatch, command, output_name
    ):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "psn_transactions.json").write_text("[]")

        result = CliRunner().invoke(app, [command])

        assert result.exit_code == 1
        assert "psn_transactions_raw.json" in result.output
        assert not (tmp_path / output_name).exists()

    def test_export_cli_reports_missing_input_without_traceback(self, tmp_path):
        missing_path = tmp_path / "missing.json"

        result = CliRunner().invoke(
            app,
            ["export", "--input", str(missing_path)],
        )

        assert result.exit_code == 1
        assert "Transaction JSON not found" in result.output
        assert "Traceback" not in result.output


class TestSkuCacheIntegrity:
    def test_cache_is_private_and_atomic(self, tmp_path, monkeypatch):
        cache_file = tmp_path / ".psn-transactions" / "sku_cache.json"
        monkeypatch.setattr(store_enrich, "SKU_CACHE_FILE", cache_file)

        cache = store_enrich._empty_cache()
        monkeypatch.setattr(store_enrich.cfg, "get_locale", lambda: "en-au")
        store_enrich._record_cache_result(
            "SKU001",
            cache,
            "success",
            {"content_type": "FULL_GAME"},
        )
        store_enrich._save_cache(cache)

        saved = json.loads(cache_file.read_text())
        assert saved["schema_version"] == store_enrich.CACHE_SCHEMA_VERSION
        assert saved["source"] == store_enrich.CACHE_SOURCE
        assert saved["entries"]["en-au|SKU001"]["status"] == "success"
        assert saved["entries"]["en-au|SKU001"]["metadata"] == {
            "content_type": "FULL_GAME"
        }
        assert stat.S_IMODE(cache_file.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(cache_file.stat().st_mode) == 0o600

    def test_malformed_cache_reports_user_facing_error(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "sku_cache.json"
        cache_file.write_text("not-json")
        monkeypatch.setattr(store_enrich, "SKU_CACHE_FILE", cache_file)

        with pytest.raises(PSNTransactionsError, match="Could not read SKU cache"):
            store_enrich._load_cache()

    def test_cache_write_failure_preserves_existing_cache(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "sku_cache.json"
        cache_file.write_text(json.dumps({"existing": {"content_type": "GAME"}}))
        monkeypatch.setattr(store_enrich, "SKU_CACHE_FILE", cache_file)
        monkeypatch.setattr(
            storage.os,
            "replace",
            lambda source, destination: (_ for _ in ()).throw(
                OSError("simulated disk failure")
            ),
        )

        with pytest.raises(PSNTransactionsError, match="Could not save SKU cache"):
            store_enrich._save_cache(store_enrich._empty_cache())

        assert json.loads(cache_file.read_text()) == {
            "existing": {"content_type": "GAME"}
        }
        assert list(tmp_path.glob(".sku_cache.json.*.tmp")) == []

    def test_legacy_cached_failures_are_retried(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "sku_cache.json"
        cache_file.write_text(json.dumps({"SKU001": {"error": 503}}))
        monkeypatch.setattr(store_enrich, "SKU_CACHE_FILE", cache_file)

        assert store_enrich._load_cache() == store_enrich._empty_cache()

    def test_transient_lookup_failure_is_not_cached_and_retries(self, monkeypatch):
        class FakeResponse:
            def __init__(self, status_code, payload=None):
                self.status_code = status_code
                self.payload = payload

            def json(self):
                return self.payload

        responses = iter(
            [
                FakeResponse(503),
                FakeResponse(200, {"attributes": {"game_content_type": "FULL_GAME"}}),
            ]
        )
        calls = []
        monkeypatch.setattr(
            store_enrich.requests,
            "get",
            lambda *args, **kwargs: calls.append((args, kwargs)) or next(responses),
        )
        cache = store_enrich._empty_cache()

        first = store_enrich._lookup_sku("SKU001", cache)
        second = store_enrich._lookup_sku("SKU001", cache)

        assert first["status"] == "temporary_failure"
        assert second["status"] == "success"
        assert second["metadata"]["content_type"] == "FULL_GAME"
        assert len(cache["entries"]) == 1
        assert len(calls) == 2

    def test_network_failure_is_not_cached(self, monkeypatch):
        monkeypatch.setattr(
            store_enrich.requests,
            "get",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                requests.ConnectionError("offline")
            ),
        )
        cache = store_enrich._empty_cache()

        result = store_enrich._lookup_sku("SKU001", cache)

        assert result["status"] == "temporary_failure"
        assert "offline" in result["detail"]
        assert cache["entries"] == {}

    def test_not_found_is_cached_and_reused(self, monkeypatch):
        class FakeResponse:
            status_code = 404

        calls = []
        monkeypatch.setattr(
            store_enrich.requests,
            "get",
            lambda *args, **kwargs: calls.append((args, kwargs)) or FakeResponse(),
        )
        cache = store_enrich._empty_cache()

        first = store_enrich._lookup_sku("SKU001", cache)
        second = store_enrich._lookup_sku("SKU001", cache)

        assert first["status"] == "not_found"
        assert first["cached"] is False
        assert second["status"] == "not_found"
        assert second["cached"] is True
        assert len(calls) == 1

    def test_empty_success_is_short_lived_no_metadata_result(self, monkeypatch):
        class FakeResponse:
            status_code = 200

            def json(self):
                return {"content_type": "1"}

        monkeypatch.setattr(store_enrich.requests, "get", lambda *args, **kwargs: FakeResponse())
        monkeypatch.setattr(store_enrich.cfg, "get_locale", lambda: "en-us")
        cache = store_enrich._empty_cache()

        result = store_enrich._lookup_sku("SKU001", cache)

        assert result["status"] == "no_metadata"
        assert cache["entries"]["en-us|SKU001"]["status"] == "no_metadata"

    def test_malformed_store_structure_is_a_precise_temporary_failure(
        self, monkeypatch
    ):
        class FakeResponse:
            status_code = 200

            def json(self):
                return {"attributes": ["not-an-object"]}

        monkeypatch.setattr(
            store_enrich.requests,
            "get",
            lambda *args, **kwargs: FakeResponse(),
        )

        result = store_enrich._fetch_sku("SKU001")

        assert result["status"] == "temporary_failure"
        assert result["detail"] == (
            "Malformed Store response: attributes must be an object or null, "
            "found list"
        )

    def test_no_content_response_is_cached_as_no_metadata(self, monkeypatch):
        class FakeResponse:
            status_code = 204

        calls = []
        monkeypatch.setattr(
            store_enrich.requests,
            "get",
            lambda *args, **kwargs: calls.append((args, kwargs)) or FakeResponse(),
        )
        cache = store_enrich._empty_cache()

        first = store_enrich._lookup_sku("SKU001", cache)
        second = store_enrich._lookup_sku("SKU001", cache)

        assert first["status"] == "no_metadata"
        assert second["status"] == "no_metadata"
        assert second["cached"] is True
        assert len(calls) == 1

    def test_cache_is_scoped_by_locale(self, monkeypatch):
        locale = ["en-au"]
        monkeypatch.setattr(store_enrich.cfg, "get_locale", lambda: locale[0])
        cache = store_enrich._empty_cache()
        store_enrich._record_cache_result(
            "SKU001", cache, "success", {"content_type": "FULL_GAME"}
        )

        assert store_enrich._cached_result("SKU001", cache) is not None
        locale[0] = "en-gb"
        assert store_enrich._cached_result("SKU001", cache) is None

    def test_expired_negative_cache_result_is_retried(self, monkeypatch):
        now = datetime(2026, 8, 2, tzinfo=timezone.utc)
        monkeypatch.setattr(store_enrich, "_utc_now", lambda: now)
        cache = store_enrich._empty_cache()
        store_enrich._record_cache_result("SKU001", cache, "not_found", {})

        later = now + store_enrich.NOT_FOUND_CACHE_TTL + timedelta(seconds=1)

        assert store_enrich._cached_result("SKU001", cache, now=later) is None
        assert cache["entries"] == {}


class TestSerialEnrichment:
    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def close(self):
            pass

    def test_lookups_reuse_one_session(self, monkeypatch):
        sessions = []

        def fake_fetch(sku, session=None):
            sessions.append(session)
            return {
                "status": "success",
                "metadata": {"content_type": "FULL_GAME"},
                "cached": False,
            }

        monkeypatch.setattr(store_enrich, "_store_session", self.FakeSession)
        monkeypatch.setattr(store_enrich, "_fetch_sku", fake_fetch)
        cache = store_enrich._empty_cache()

        results = store_enrich._enrich_skus(
            {"SKU001", "SKU002", "SKU003", "SKU004"}, cache
        )

        assert set(results) == {"SKU001", "SKU002", "SKU003", "SKU004"}
        assert len(sessions) == 4
        assert len({id(session) for session in sessions}) == 1
        assert len(cache["entries"]) == 4

    def test_normalised_variants_share_one_lookup(self, monkeypatch):
        calls = []

        def fake_fetch(sku, session=None):
            calls.append(sku)
            return {
                "status": "success",
                "metadata": {"content_type": "FULL_GAME"},
                "cached": False,
            }

        monkeypatch.setattr(store_enrich, "_store_session", self.FakeSession)
        monkeypatch.setattr(store_enrich, "_fetch_sku", fake_fetch)
        cache = store_enrich._empty_cache()

        results = store_enrich._enrich_skus(
            {"UP001-GAME-E001", "UP001-GAME-E002"},
            cache,
        )

        assert len(calls) == 1
        assert len(results) == 2
        assert len(cache["entries"]) == 1

    def test_refresh_bypasses_successful_cache(self, monkeypatch):
        calls = []

        def fake_fetch(sku, session=None):
            calls.append(sku)
            return {
                "status": "success",
                "metadata": {"content_type": "FULL_GAME"},
                "cached": False,
            }

        monkeypatch.setattr(store_enrich, "_store_session", self.FakeSession)
        monkeypatch.setattr(store_enrich, "_fetch_sku", fake_fetch)
        cache = store_enrich._empty_cache()
        store_enrich._record_cache_result(
            "SKU001",
            cache,
            "success",
            {"content_type": "ADD_ON"},
        )

        results = store_enrich._enrich_skus(
            {"SKU001"},
            cache,
            cache_mode=store_enrich.CacheMode.REFRESH,
        )

        assert calls == ["SKU001"]
        assert results["SKU001"]["metadata"]["content_type"] == "FULL_GAME"
        assert (
            cache["entries"][store_enrich._cache_key("SKU001")]["metadata"]["content_type"]
            == "FULL_GAME"
        )

    def test_refresh_temporary_failure_uses_stale_successful_metadata(
        self, monkeypatch
    ):
        def fake_fetch(sku, session=None):
            return {
                "status": "temporary_failure",
                "metadata": {},
                "cached": False,
                "detail": "HTTP 503",
            }

        monkeypatch.setattr(store_enrich, "_store_session", self.FakeSession)
        monkeypatch.setattr(store_enrich, "_fetch_sku", fake_fetch)
        cache = store_enrich._empty_cache()
        store_enrich._record_cache_result(
            "SKU001",
            cache,
            "success",
            {"content_type": "ADD_ON"},
        )
        cached_record = dict(cache["entries"][store_enrich._cache_key("SKU001")])

        results = store_enrich._enrich_skus(
            {"SKU001"},
            cache,
            cache_mode=store_enrich.CacheMode.REFRESH,
        )

        assert results["SKU001"] == {
            "status": "stale_cache",
            "metadata": {"content_type": "ADD_ON"},
            "cached": True,
            "network_requested": True,
            "detail": "Refresh failed with HTTP 503; using cached metadata",
        }
        assert cache["entries"][store_enrich._cache_key("SKU001")] == cached_record

    def test_refresh_not_found_replaces_previous_success(self, monkeypatch):
        def fake_fetch(sku, session=None):
            return {
                "status": "not_found",
                "metadata": {},
                "cached": False,
                "detail": "HTTP 404",
            }

        monkeypatch.setattr(store_enrich, "_store_session", self.FakeSession)
        monkeypatch.setattr(store_enrich, "_fetch_sku", fake_fetch)
        cache = store_enrich._empty_cache()
        store_enrich._record_cache_result(
            "SKU001",
            cache,
            "success",
            {"content_type": "FULL_GAME"},
        )

        results = store_enrich._enrich_skus(
            {"SKU001"},
            cache,
            cache_mode=store_enrich.CacheMode.REFRESH,
        )

        assert results["SKU001"]["status"] == "not_found"
        record = cache["entries"][store_enrich._cache_key("SKU001")]
        assert record["status"] == "not_found"
        assert record["metadata"] == {}

    def test_cache_only_uses_hits_and_marks_misses_without_session(self, monkeypatch):
        cache = store_enrich._empty_cache()
        store_enrich._record_cache_result(
            "SKU001",
            cache,
            "success",
            {"content_type": "FULL_GAME"},
        )
        monkeypatch.setattr(
            store_enrich,
            "_store_session",
            lambda: (_ for _ in ()).throw(AssertionError("network session opened")),
        )

        results = store_enrich._enrich_skus(
            {"SKU001", "SKU002"},
            cache,
            cache_mode=store_enrich.CacheMode.ONLY,
        )

        assert results["SKU001"]["status"] == "success"
        assert results["SKU001"]["cached"] is True
        assert results["SKU002"]["status"] == "cache_miss"
        assert results["SKU002"]["cached"] is False
        assert results["SKU002"]["detail"] == "No reusable cached metadata"

    def test_interrupt_saves_completed_cache_progress(
        self, tmp_path, monkeypatch
    ):
        cache_file = tmp_path / ".psn-transactions" / "sku_cache.json"
        calls = []

        def interrupt_second_lookup(sku, session=None):
            calls.append(sku)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return {
                "status": "success",
                "metadata": {"content_type": "FULL_GAME"},
                "cached": False,
            }

        monkeypatch.setattr(store_enrich, "SKU_CACHE_FILE", cache_file)
        monkeypatch.setattr(store_enrich, "_store_session", self.FakeSession)
        monkeypatch.setattr(store_enrich, "_fetch_sku", interrupt_second_lookup)
        cache = store_enrich._empty_cache()

        with pytest.raises(PSNTransactionsError, match="results were saved"):
            store_enrich._enrich_skus({"SKU001", "SKU002"}, cache)

        saved = json.loads(cache_file.read_text())
        assert len(saved["entries"]) == 1
