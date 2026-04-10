# psn-receipts

Export your complete PlayStation Network transaction history.

Sony hides transaction history behind infinite scroll and bot protection. This tool uses a saved browser session to call the internal PSN GraphQL API directly, bypassing CORS via `page.evaluate()`, and enriches results with content-type metadata from the PS Store API.

Works with all major PSN regions (default US).

## Install

```bash
pip install -e .
playwright install chromium
```

## Usage

### 1. Log in (once)

```bash
psn-receipts login
```

A browser window opens (system Chrome with passkey support if available; Chromium as fallback). Sign in to PlayStation Store, complete any 2FA, then press **ENTER** in the terminal. Your session is saved to `~/.psn-receipts/auth.json`.

```bash
psn-receipts login --force              # re-authenticate
psn-receipts login --debug              # also print session cookies
psn-receipts login --locale en-au       # set region (default: en-us)
```

Supported locales: `en-us` `en-gb` `en-au` `en-ca` `de-de` `fr-fr` `es-es` `it-it` `nl-nl` `pt-pt` `ja-jp` `ko-kr` `pt-br` `es-mx`

The locale is saved to `~/.psn-receipts/config.json` and reused automatically by `fetch` and `export`.

### 2. Fetch transaction history

```bash
psn-receipts fetch
```

Downloads all transactions to `psn_history_full.json`. For testing, limit to one page (100 transactions):

```bash
psn-receipts fetch --limit 1
psn-receipts fetch --output my_history.json
```

### 3. Export to CSV

```bash
psn-receipts export                   # basic export, no classification
psn-receipts export --enrich          # also classify each item via PS Store API
psn-receipts export --enrich --csv enriched.csv
```

## CSV columns

| Column | Description |
|---|---|
| `date` | Transaction date (YYYY-MM-DD HH:MM) |
| `transaction_id` | PSN transaction ID |
| `product` | Product name |
| `paid` | Amount paid |
| `original` | Original price before discounts |
| `discount` | Discount applied |
| `tax` | Tax component |
| `sku` | PlayStation SKU identifier |
| `payment` | Payment method |
| `card_last4` | Last 4 digits of payment card |

### With `--enrich`

Running `psn-receipts export --enrich` looks up each SKU against the PS Store API to classify your purchases. The following columns are always present in the CSV but are empty without `--enrich`:

| Column | Description |
|---|---|
| `category` | Classified purchase type (see below) |
| `content_type` | Raw content type from PS Store API |
| `is_ps_plus` | `True`/`False` if the item was via PS Plus, empty without `--enrich` |

**Category values:**

| Category | Condition |
|---|---|
| PS Plus Pack | "PlayStation Plus" in product name |
| PS Plus Monthly | Transaction total = $0, original price > $0 |
| Full Game | `FULL_GAME`, `PS5_GAME`, `PS4_GAME`, or standard SKU pattern |
| DLC / Add-on | `ADDON`, `DLC`, or keywords (pack, skin, season pass) |
| Bundle | `BUNDLE` content type |
| In-Game Currency | `CURRENCY` content type |
| Other | Unclassified |

SKU lookups are cached in `~/.psn-receipts/sku_cache.json`.

## Development

```bash
pip install -e .
python -m pytest tests/ -v
```

## Requirements

- Python 3.11+
- Playwright Chromium (`playwright install chromium`)
- A PlayStation Network account (any region)
