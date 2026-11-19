"""Tests for private manual classification and base-game decisions."""

import csv
import json
import stat

import pytest
from typer.testing import CliRunner

from psn_transactions import enrich as store_enrich
from psn_transactions import export as csv_export
from psn_transactions import overrides
from psn_transactions.cli import app
from psn_transactions.errors import PSNTransactionsError
from psn_transactions.relationships import link_owned_base_games


BASE_KEY = "en-us|EX0001-CUSA00001_00-BASEGAME"


def write_overrides(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=overrides.OVERRIDE_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def store_fields(**values):
    return {
        "relationship_type": "",
        "base_game_key": "",
        "base_game_title": "",
        "relationship_status": "unresolved",
        "relationship_source": "",
        "relationship_evidence": "",
        **values,
    }


class TestLoadOverrides:
    def test_missing_file_means_no_overrides(self, isolated_overrides_file):
        assert overrides.load_overrides() == {}

    def test_rows_without_decisions_are_ignored(self, isolated_overrides_file):
        write_overrides(
            isolated_overrides_file,
            [
                {"product_key": "en-us|EX0001-A", "product": "Reference only"},
                {"product_key": "en-us|EX0001-B", "base_game_key": "None"},
            ],
        )

        loaded = overrides.load_overrides()

        assert set(loaded) == {"en-us|EX0001-B"}
        assert loaded["en-us|EX0001-B"]["base_game_key"] == "none"

    @pytest.mark.parametrize(
        ("row", "message"),
        [
            ({"product_key": "EX0001-A", "category": "Full Game"}, "invalid product_key"),
            ({"product_key": "en-us|EX0001-A", "category": "DLC"}, "unknown category"),
            (
                {"product_key": "en-us|EX0001-A", "base_game_key": "EX0001-BASE"},
                "invalid base_game_key",
            ),
            (
                {
                    "product_key": "en-us|EX0001-A",
                    "base_game_key": "none",
                    "base_game_title": "Example",
                },
                "also gives a base_game_title",
            ),
            ({"product_key": "", "category": "Full Game"}, "no product_key"),
        ],
    )
    def test_invalid_rows_report_the_row_number(
        self, isolated_overrides_file, row, message
    ):
        write_overrides(isolated_overrides_file, [row])

        with pytest.raises(PSNTransactionsError, match=message) as error:
            overrides.load_overrides()
        assert "row 2" in str(error.value)

    def test_duplicate_product_keys_are_rejected(self, isolated_overrides_file):
        row = {"product_key": "en-us|EX0001-A", "category": "Full Game"}
        write_overrides(isolated_overrides_file, [row, row])

        with pytest.raises(PSNTransactionsError, match="row 3 repeats"):
            overrides.load_overrides()


class TestApplyOverrides:
    def test_manual_base_game_replaces_store_evidence(self):
        fields = store_fields()
        overrides.apply_relationship_override(
            fields,
            {
                "category": "",
                "base_game_key": BASE_KEY,
                "base_game_title": "",
                "notes": "Checked the Store page",
            },
            "DLC / Add-on",
        )

        assert fields["relationship_type"] == "add_on_of"
        assert fields["base_game_key"] == BASE_KEY
        assert fields["relationship_status"] == "manual"
        assert fields["relationship_source"] == "manual"
        assert fields["relationship_evidence"] == "Checked the Store page"

    def test_manual_key_matching_store_parent_keeps_store_title(self):
        fields = store_fields(
            relationship_type="add_on_of",
            base_game_key=BASE_KEY,
            base_game_title="Example Game",
            relationship_status="verified",
        )
        overrides.apply_relationship_override(
            fields,
            {
                "category": "",
                "base_game_key": BASE_KEY,
                "base_game_title": "",
                "notes": "",
            },
            "DLC / Add-on",
        )

        assert fields["base_game_title"] == "Example Game"
        assert fields["relationship_evidence"] == "relationship_overrides.csv"

    def test_none_removes_a_relationship(self):
        fields = store_fields(
            relationship_type="add_on_of",
            base_game_key=BASE_KEY,
            relationship_status="verified",
        )
        overrides.apply_relationship_override(
            fields,
            {"category": "", "base_game_key": "none", "base_game_title": "", "notes": ""},
            "DLC / Add-on",
        )

        assert fields["relationship_status"] == "not_applicable"
        assert fields["relationship_source"] == "manual"
        assert fields["base_game_key"] == ""

    def test_non_add_on_manual_link_is_an_edition(self):
        fields = store_fields(relationship_status="not_applicable")
        overrides.apply_relationship_override(
            fields,
            {"category": "Full Game", "base_game_key": BASE_KEY, "base_game_title": "", "notes": ""},
            "Full Game",
        )

        assert fields["relationship_type"] == "edition_of"

    def test_manual_title_links_only_to_an_exactly_matching_owned_game(self):
        add_on = {
            "order_item_id": "ITEM-DLC",
            "product": "Example Expansion",
            "product_key": "en-us|EX0001-DLC",
            "paid_minor": 999,
            "date": "2025-02-01 00:00",
            "category": "DLC / Add-on",
            "relationship_type": "add_on_of",
            "relationship_status": "manual",
            "base_game_key": "",
            "base_game_title": "example game",
        }
        near_miss = {**add_on, "base_game_title": "Example Gam"}
        owned = {
            "order_item_id": "ITEM-BASE",
            "product": "Example Game™",
            "product_key": "en-us|EX0001-BASE",
            "paid_minor": 0,
            "date": "2025-01-01 00:00",
            "category": "Full Game",
            "relationship_type": "",
            "base_game_key": "",
        }
        rows = [add_on, near_miss, owned]

        link_owned_base_games(rows)

        assert add_on["base_game_ownership"] == "free"
        assert add_on["base_game_match"] == "title"
        assert add_on["base_game_order_item_id"] == "ITEM-BASE"
        assert near_miss["base_game_ownership"] == "not_in_history"


def make_product(name, sku, cents, item_id):
    return {
        "orderItemId": item_id,
        "productName": name,
        "skuId": sku,
        "skuType": "STANDARD",
        "totalFormatted": f"${cents / 100:.2f}",
        "displayOfPrice": f"${cents / 100:.2f}",
        "total": cents,
        "originalPrice": cents,
    }


def make_tx(tx_id, date, products):
    return {
        "id": tx_id,
        "date": date,
        "transactionType": "PRODUCT_PURCHASE",
        "displayOfTransactionValue": "$0.00",
        "chargeDetails": [],
        "purchaseDetails": {"total": 0, "originalPrice": 0, "productPurchases": products},
    }


@pytest.fixture
def enrich_environment(tmp_path, monkeypatch):
    input_path = tmp_path / "transactions.json"
    output_path = tmp_path / "transactions.csv"
    input_path.write_text(
        json.dumps(
            [
                make_tx(
                    "TX-2",
                    "2025-02-01T00:00:00.000Z",
                    [
                        make_product("Mystery Pack", "EX0001-CUSA00002_00-MYSTERY-E001", 499, "ITEM-MYSTERY"),
                        make_product("Delisted Thing", "EX0001-CUSA00003_00-DELISTED-E001", 299, "ITEM-DELISTED"),
                    ],
                ),
                make_tx(
                    "TX-1",
                    "2025-01-01T00:00:00.000Z",
                    [make_product("Example Game", "EX0001-CUSA00001_00-BASEGAME-E001", 0, "ITEM-BASE")],
                ),
            ]
        )
    )
    metadata = {
        "EX0001-CUSA00002_00-MYSTERY-E001": {"content_type": "ADD_ON"},
        "EX0001-CUSA00003_00-DELISTED-E001": {},
        "EX0001-CUSA00001_00-BASEGAME-E001": {"content_type": "FULL_GAME"},
    }

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_fetch(sku, session=None):
        if not metadata[sku]:
            return {"status": "not_found", "metadata": {}, "cached": False, "detail": "HTTP 404"}
        return {"status": "success", "metadata": metadata[sku], "cached": False}

    monkeypatch.setattr(store_enrich, "SKU_CACHE_FILE", tmp_path / "cache" / "sku_cache.json")
    monkeypatch.setattr(store_enrich.cfg, "get_locale", lambda: "en-us")
    monkeypatch.setattr(store_enrich, "_store_session", FakeSession)
    monkeypatch.setattr(store_enrich, "_fetch_sku", fake_fetch)

    def run(include_base_games=True):
        csv_export.enrich_csv(
            json_path=str(input_path),
            csv_path=str(output_path),
            paid_only=True,
            include_base_games=include_base_games,
        )
        with output_path.open(newline="", encoding="utf-8") as file:
            return {row["order_item_id"]: row for row in csv.DictReader(file)}

    return run, output_path


class TestOverridesInEnrichment:
    def test_manual_decisions_drive_classification_and_linking(
        self, enrich_environment, isolated_overrides_file, capsys
    ):
        run, _output_path = enrich_environment
        write_overrides(
            isolated_overrides_file,
            [
                {
                    "product_key": "en-us|EX0001-CUSA00002_00-MYSTERY",
                    "base_game_key": BASE_KEY,
                    "notes": "Store page lists it as an add-on for Example Game",
                },
                {
                    "product_key": "en-us|EX0001-CUSA00003_00-DELISTED",
                    "category": "DLC / Add-on",
                    "base_game_title": "Disc Only Game",
                },
                {"product_key": "en-us|EX0001-UNUSED", "category": "Full Game"},
            ],
        )

        rows = run()

        mystery = rows["ITEM-MYSTERY"]
        assert mystery["relationship_status"] == "manual"
        assert mystery["base_game_ownership"] == "free"
        assert mystery["base_game_order_item_id"] == "ITEM-BASE"
        assert rows["ITEM-BASE"]["inclusion_reason"] == "base_game_reference"

        delisted = rows["ITEM-DELISTED"]
        assert delisted["category"] == "DLC / Add-on"
        assert delisted["classification_source"] == "manual"
        assert delisted["relationship_status"] == "manual"
        assert delisted["base_game_title"] == "Disc Only Game"
        assert delisted["base_game_ownership"] == "not_in_history"
        assert "applied: 2; not matched to any output row: 1" in capsys.readouterr().out

    def test_paid_only_excludes_free_base_games_unless_requested(
        self, enrich_environment, isolated_overrides_file
    ):
        run, _output_path = enrich_environment
        write_overrides(
            isolated_overrides_file,
            [
                {
                    "product_key": "en-us|EX0001-CUSA00002_00-MYSTERY",
                    "base_game_key": BASE_KEY,
                }
            ],
        )

        rows = run(include_base_games=False)

        assert "ITEM-BASE" not in rows
        assert rows["ITEM-MYSTERY"]["base_game_ownership"] == "free"
        assert rows["ITEM-MYSTERY"]["base_game_order_item_id"] == "ITEM-BASE"
        assert rows["ITEM-MYSTERY"]["inclusion_reason"] == "paid"

    def test_curate_seeds_undecided_rows_and_preserves_decisions(
        self, enrich_environment, isolated_overrides_file
    ):
        run, output_path = enrich_environment
        run()

        result = CliRunner().invoke(app, ["curate", "--input", str(output_path)])

        assert result.exit_code == 0, result.output
        assert "Added 2 products" in result.output
        mode = stat.S_IMODE(isolated_overrides_file.stat().st_mode)
        assert mode == 0o600
        with isolated_overrides_file.open(newline="", encoding="utf-8") as file:
            seeded = {row["product_key"]: row for row in csv.DictReader(file)}
        assert set(seeded) == {
            "en-us|EX0001-CUSA00002_00-MYSTERY",
            "en-us|EX0001-CUSA00003_00-DELISTED",
        }
        assert seeded["en-us|EX0001-CUSA00002_00-MYSTERY"]["reason"].startswith(
            "unresolved"
        )
        assert seeded["en-us|EX0001-CUSA00003_00-DELISTED"]["reason"] == (
            "unclassified: enrichment_status=not_found"
        )

        rows = list(seeded.values())
        rows[0]["base_game_key"] = BASE_KEY
        write_overrides(isolated_overrides_file, rows)
        again = CliRunner().invoke(app, ["curate", "--input", str(output_path)])

        assert "Added 0 products" in again.output
        assert "2 were already listed and refreshed" in again.output
        assert overrides.load_overrides()[rows[0]["product_key"]]["base_game_key"] == BASE_KEY

    def test_invalid_overrides_stop_enrichment(
        self, enrich_environment, isolated_overrides_file
    ):
        run, output_path = enrich_environment
        write_overrides(
            isolated_overrides_file,
            [{"product_key": "en-us|EX0001-A", "category": "Game"}],
        )

        with pytest.raises(PSNTransactionsError, match="unknown category"):
            run()
        assert not output_path.exists()


class TestSeedOverrides:
    ENRICHED_FIELDS = [
        "product_key",
        "product",
        "category",
        "relationship_status",
        "relationship_evidence",
        "enrichment_status",
    ]

    def write_enriched(self, path, rows):
        with path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=self.ENRICHED_FIELDS)
            writer.writeheader()
            writer.writerows(rows)

    def read_seeded(self, path):
        with path.open(newline="", encoding="utf-8") as file:
            return list(csv.DictReader(file))

    @pytest.fixture
    def enriched_csv(self, tmp_path):
        path = tmp_path / "enriched.csv"
        self.write_enriched(
            path,
            [
                {
                    "product_key": BASE_KEY,
                    "product": "Base Game",
                    "category": "Full Game",
                    "relationship_status": "not_applicable",
                },
                {
                    "product_key": "en-us|EX0001-CUSA00001_00-ZEXPANSION",
                    "product": "Zed Expansion",
                    "category": "DLC / Add-on",
                    "relationship_status": "unresolved",
                    "relationship_evidence": (
                        "content_type=ADD_ON; Store response contains no parent_links"
                    ),
                },
                {
                    "product_key": "en-us|EX0001-CUSA00009_00-DELISTED",
                    "product": "Alpha Delisted",
                    "category": "Other",
                    "enrichment_status": "not_found",
                },
                {
                    "product_key": "en-us|EX0001-CUSA00005_00-BUNDLE",
                    "product": "Mega Bundle",
                    "category": "Bundle",
                    "relationship_status": "conflict",
                    "relationship_evidence": (
                        "parent_links contains 2 products: "
                        "EX0001-CUSA00001_00-BASEGAME,EX0001-PPSA00001_00-PS5GAME; "
                        "product is not explicitly classified as add-on content"
                    ),
                },
            ],
        )
        return path

    def test_lists_candidates_and_sorts_by_reason(
        self, enriched_csv, isolated_overrides_file
    ):
        assert overrides.seed_overrides(enriched_csv) == (3, 0)

        rows = self.read_seeded(isolated_overrides_file)
        assert [row["product"] for row in rows] == [
            "Mega Bundle",
            "Zed Expansion",
            "Alpha Delisted",
        ]
        assert rows[0]["candidates"] == (
            f"{BASE_KEY} (Store parent, owned: Base Game); "
            "en-us|EX0001-PPSA00001_00-PS5GAME (Store parent, not in history)"
        )
        assert rows[1]["candidates"] == (
            f"{BASE_KEY} (same title ID, owned: Base Game)"
        )
        assert rows[2]["candidates"] == ""

    def test_refreshes_reference_columns_without_touching_decisions(
        self, enriched_csv, isolated_overrides_file
    ):
        # A file written before the candidates column existed, in old order.
        isolated_overrides_file.parent.mkdir(parents=True, exist_ok=True)
        with isolated_overrides_file.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(
                file,
                fieldnames=[
                    "product_key",
                    "product",
                    "current_category",
                    "reason",
                    "category",
                    "base_game_key",
                    "base_game_title",
                    "notes",
                    "my_column",
                ],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "product_key": "en-us|EX0001-CUSA00009_00-DELISTED",
                    "product": "Alpha Delisted",
                    "reason": "unclassified",
                    "category": "DLC / Add-on",
                    "base_game_key": BASE_KEY,
                    "notes": "Checked PSNProfiles",
                    "my_column": "keep me",
                }
            )
            writer.writerow(
                {
                    "product_key": "en-us|EX0001-CUSA00001_00-ZEXPANSION",
                    "product": "Old name",
                    "reason": "old reason",
                }
            )

        assert overrides.seed_overrides(enriched_csv) == (1, 2)

        rows = {row["product_key"]: row for row in self.read_seeded(
            isolated_overrides_file
        )}
        assert [row["product"] for row in rows.values()] == [
            "Mega Bundle",
            "Zed Expansion",
            "Alpha Delisted",
        ]
        expansion = rows["en-us|EX0001-CUSA00001_00-ZEXPANSION"]
        assert expansion["reason"].startswith("unresolved: content_type=ADD_ON")
        assert "same title ID" in expansion["candidates"]
        decided = rows["en-us|EX0001-CUSA00009_00-DELISTED"]
        assert decided["category"] == "DLC / Add-on"
        assert decided["base_game_key"] == BASE_KEY
        assert decided["notes"] == "Checked PSNProfiles"
        assert decided["my_column"] == "keep me"

    def test_unchanged_file_is_not_rewritten(
        self, enriched_csv, isolated_overrides_file
    ):
        overrides.seed_overrides(enriched_csv)
        before = isolated_overrides_file.stat().st_mtime_ns

        assert overrides.seed_overrides(enriched_csv) == (0, 3)
        assert isolated_overrides_file.stat().st_mtime_ns == before
