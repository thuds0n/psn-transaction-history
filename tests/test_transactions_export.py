"""Tests for PSN transaction parsing and classification logic."""

import csv
import json
import stat
from datetime import datetime, timedelta, timezone

import pytest
import requests
from typer.testing import CliRunner

from psn_transactions import parse, storage
from psn_transactions.cli import app
from psn_transactions.errors import PSNTransactionsError
from psn_transactions.parse import (
    CSV_FIELDS,
    _classify,
    _classify_detailed,
    _flatten,
    _normalise_store_metadata,
    _sku_base,
)


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
        category, is_ps_plus, source = _classify_detailed(
            "Recurring membership",
            "SUBSCRIPTION-SKU",
            tx_total=1299,
            item_original=1299,
            info={},
            sku_type="SUBSCRIPTION",
        )
        assert category == "Subscription"
        assert is_ps_plus is None
        assert source == "transaction"


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
        assert rows[0]["is_ps_plus"] == ""
        assert rows[0]["enrichment_status"] == "success"
        assert rows[0]["classification_source"] == "store_api"

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
        assert rows[0]["enrichment_status"] == ""
        assert rows[0]["classification_source"] == ""

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

        parse.export(json_path=str(input_path), csv_path=str(output_path))

        with output_path.open(newline="", encoding="utf-8") as output_file:
            rows = list(csv.DictReader(output_file))
        assert len(rows) == 1
        assert rows[0]["product"] == "Some Game"
        assert set(rows[0]) == set(CSV_FIELDS)

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
            parse.export(
                json_path=str(input_path),
                csv_path=str(tmp_path / "transactions.csv"),
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
            parse.export(json_path=str(input_path), csv_path=str(output_path))

        assert output_path.read_text() == "existing export"
        assert list(tmp_path.glob(".transactions.csv.*.tmp")) == []

    def test_basic_export_does_not_depend_on_sku_cache(self, tmp_path, monkeypatch):
        input_path = tmp_path / "transactions.json"
        output_path = tmp_path / "transactions.csv"
        cache_file = tmp_path / "sku_cache.json"
        input_path.write_text(json.dumps([make_tx()]))
        cache_file.write_text("not-json")
        monkeypatch.setattr(parse, "SKU_CACHE_FILE", cache_file)

        parse.export(
            json_path=str(input_path),
            csv_path=str(output_path),
            enrich=False,
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

        monkeypatch.setattr(parse, "SKU_CACHE_FILE", cache_file)
        monkeypatch.setattr(parse.cfg, "get_locale", lambda: "en-us")
        monkeypatch.setattr(parse, "_store_session", FakeSession)

        parse.export(
            json_path=str(input_path),
            csv_path=str(output_path),
            enrich=True,
        )

        with output_path.open(newline="", encoding="utf-8") as output_file:
            row = next(csv.DictReader(output_file))
        assert row["category"] == "DLC / Add-on"
        assert row["content_type"] == "ADD_ON"
        assert row["platform"] == "PS5"
        assert row["publisher"] == "Example Publisher"
        assert row["enrichment_status"] == "success"
        assert row["classification_source"] == "store_api"
        assert "success: 1" in capsys.readouterr().out
        assert cache_file.exists()

    def test_export_cli_forwards_enrichment(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            parse,
            "export",
            lambda **kwargs: calls.append(kwargs),
        )

        result = CliRunner().invoke(
            app,
            [
                "export",
                "--enrich",
            ],
        )

        assert result.exit_code == 0
        assert calls == [
            {
                "json_path": "psn_transactions.json",
                "csv_path": "psn_transactions.csv",
                "enrich": True,
            }
        ]

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
        monkeypatch.setattr(parse, "SKU_CACHE_FILE", cache_file)

        cache = parse._empty_cache()
        monkeypatch.setattr(parse.cfg, "get_locale", lambda: "en-au")
        parse._record_cache_result(
            "SKU001",
            cache,
            "success",
            {"content_type": "FULL_GAME"},
        )
        parse._save_cache(cache)

        saved = json.loads(cache_file.read_text())
        assert saved["schema_version"] == parse.CACHE_SCHEMA_VERSION
        assert saved["source"] == parse.CACHE_SOURCE
        assert saved["entries"]["en-au|SKU001"]["status"] == "success"
        assert saved["entries"]["en-au|SKU001"]["metadata"] == {
            "content_type": "FULL_GAME"
        }
        assert stat.S_IMODE(cache_file.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(cache_file.stat().st_mode) == 0o600

    def test_malformed_cache_reports_user_facing_error(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "sku_cache.json"
        cache_file.write_text("not-json")
        monkeypatch.setattr(parse, "SKU_CACHE_FILE", cache_file)

        with pytest.raises(PSNTransactionsError, match="Could not read SKU cache"):
            parse._load_cache()

    def test_cache_write_failure_preserves_existing_cache(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "sku_cache.json"
        cache_file.write_text(json.dumps({"existing": {"content_type": "GAME"}}))
        monkeypatch.setattr(parse, "SKU_CACHE_FILE", cache_file)
        monkeypatch.setattr(
            storage.os,
            "replace",
            lambda source, destination: (_ for _ in ()).throw(
                OSError("simulated disk failure")
            ),
        )

        with pytest.raises(PSNTransactionsError, match="Could not save SKU cache"):
            parse._save_cache(parse._empty_cache())

        assert json.loads(cache_file.read_text()) == {
            "existing": {"content_type": "GAME"}
        }
        assert list(tmp_path.glob(".sku_cache.json.*.tmp")) == []

    def test_legacy_cached_failures_are_retried(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "sku_cache.json"
        cache_file.write_text(json.dumps({"SKU001": {"error": 503}}))
        monkeypatch.setattr(parse, "SKU_CACHE_FILE", cache_file)

        assert parse._load_cache() == parse._empty_cache()

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
            parse.requests,
            "get",
            lambda *args, **kwargs: calls.append((args, kwargs)) or next(responses),
        )
        cache = parse._empty_cache()

        first = parse._lookup_sku("SKU001", cache)
        second = parse._lookup_sku("SKU001", cache)

        assert first["status"] == "temporary_failure"
        assert second["status"] == "success"
        assert second["metadata"]["content_type"] == "FULL_GAME"
        assert len(cache["entries"]) == 1
        assert len(calls) == 2

    def test_network_failure_is_not_cached(self, monkeypatch):
        monkeypatch.setattr(
            parse.requests,
            "get",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                requests.ConnectionError("offline")
            ),
        )
        cache = parse._empty_cache()

        result = parse._lookup_sku("SKU001", cache)

        assert result["status"] == "temporary_failure"
        assert "offline" in result["detail"]
        assert cache["entries"] == {}

    def test_not_found_is_cached_and_reused(self, monkeypatch):
        class FakeResponse:
            status_code = 404

        calls = []
        monkeypatch.setattr(
            parse.requests,
            "get",
            lambda *args, **kwargs: calls.append((args, kwargs)) or FakeResponse(),
        )
        cache = parse._empty_cache()

        first = parse._lookup_sku("SKU001", cache)
        second = parse._lookup_sku("SKU001", cache)

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

        monkeypatch.setattr(parse.requests, "get", lambda *args, **kwargs: FakeResponse())
        monkeypatch.setattr(parse.cfg, "get_locale", lambda: "en-us")
        cache = parse._empty_cache()

        result = parse._lookup_sku("SKU001", cache)

        assert result["status"] == "no_metadata"
        assert cache["entries"]["en-us|SKU001"]["status"] == "no_metadata"

    def test_no_content_response_is_cached_as_no_metadata(self, monkeypatch):
        class FakeResponse:
            status_code = 204

        calls = []
        monkeypatch.setattr(
            parse.requests,
            "get",
            lambda *args, **kwargs: calls.append((args, kwargs)) or FakeResponse(),
        )
        cache = parse._empty_cache()

        first = parse._lookup_sku("SKU001", cache)
        second = parse._lookup_sku("SKU001", cache)

        assert first["status"] == "no_metadata"
        assert second["status"] == "no_metadata"
        assert second["cached"] is True
        assert len(calls) == 1

    def test_cache_is_scoped_by_locale(self, monkeypatch):
        locale = ["en-au"]
        monkeypatch.setattr(parse.cfg, "get_locale", lambda: locale[0])
        cache = parse._empty_cache()
        parse._record_cache_result(
            "SKU001", cache, "success", {"content_type": "FULL_GAME"}
        )

        assert parse._cached_result("SKU001", cache) is not None
        locale[0] = "en-gb"
        assert parse._cached_result("SKU001", cache) is None

    def test_expired_negative_cache_result_is_retried(self, monkeypatch):
        now = datetime(2026, 8, 2, tzinfo=timezone.utc)
        monkeypatch.setattr(parse, "_utc_now", lambda: now)
        cache = parse._empty_cache()
        parse._record_cache_result("SKU001", cache, "not_found", {})

        later = now + parse.NOT_FOUND_CACHE_TTL + timedelta(seconds=1)

        assert parse._cached_result("SKU001", cache, now=later) is None
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

        monkeypatch.setattr(parse, "_store_session", self.FakeSession)
        monkeypatch.setattr(parse, "_fetch_sku", fake_fetch)
        cache = parse._empty_cache()

        results = parse._enrich_skus(
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

        monkeypatch.setattr(parse, "_store_session", self.FakeSession)
        monkeypatch.setattr(parse, "_fetch_sku", fake_fetch)
        cache = parse._empty_cache()

        results = parse._enrich_skus(
            {"UP001-GAME-E001", "UP001-GAME-E002"},
            cache,
        )

        assert len(calls) == 1
        assert len(results) == 2
        assert len(cache["entries"]) == 1

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

        monkeypatch.setattr(parse, "SKU_CACHE_FILE", cache_file)
        monkeypatch.setattr(parse, "_store_session", self.FakeSession)
        monkeypatch.setattr(parse, "_fetch_sku", interrupt_second_lookup)
        cache = parse._empty_cache()

        with pytest.raises(PSNTransactionsError, match="results were saved"):
            parse._enrich_skus({"SKU001", "SKU002"}, cache)

        saved = json.loads(cache_file.read_text())
        assert len(saved["entries"]) == 1
