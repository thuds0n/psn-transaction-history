# PSN Transaction History

Export your complete PlayStation Network transaction history.

Sony hides transaction history behind infinite scroll and bot protection. This tool uses a saved browser session to call the internal PSN GraphQL API directly, bypassing CORS via `page.evaluate()`, and enriches results with content-type metadata from the PS Store API.

Works with all major PSN regions (default US).

## Install

Create a project-local virtual environment so `python` and installed commands consistently use the required Python version:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m playwright install chromium
```

Run `source .venv/bin/activate` again when returning to the project in a new terminal. The `.venv/` directory is ignored by Git.

## Usage

### 1. Log in (once)

```bash
psn-transactions login
```

A browser window opens (system Chrome with passkey support if available; Chromium as fallback). Sign in to PlayStation Store, complete any 2FA, then press **ENTER** in the terminal. Your session is saved to `~/.psn-transactions/auth.json` with owner-only permissions.

```bash
psn-transactions login --force              # re-authenticate
psn-transactions login --debug              # report session-cookie presence; values stay redacted
psn-transactions login --locale en-au       # set region (default: en-us)
```

Supported locales: `en-us` `en-gb` `en-au` `en-ca` `de-de` `fr-fr` `es-es` `it-it` `nl-nl` `pt-pt` `ja-jp` `ko-kr` `pt-br` `es-mx`

The locale is saved to `~/.psn-transactions/config.json` and reused automatically by `fetch` and `export`.

### 2. Fetch transaction history

```bash
psn-transactions fetch
```

Downloads all transactions to `psn_transactions.json`. The completed export replaces any existing file atomically, so a failed fetch leaves the previous export intact. For testing, limit to one page (100 transactions):

```bash
psn-transactions fetch --limit 1
psn-transactions fetch --output my_transactions.json
```

### 3. Export to CSV

```bash
psn-transactions export                   # basic export, no classification
psn-transactions export --enrich          # also classify each item via PS Store API
psn-transactions export --enrich --csv enriched_transactions.csv
```

The default CSV output is `psn_transactions.csv`.

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

Running `psn-transactions export --enrich` looks up each SKU against the PS Store API to classify your purchases. The following columns are always present in the CSV but are empty without `--enrich`:

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

SKU lookups are cached in `~/.psn-transactions/sku_cache.json`.

## Development

With the virtual environment activated, install the development extras and run the suite:

```bash
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest tests/ -v
```

## Requirements

- Python 3.11+
- Playwright Chromium (`python -m playwright install chromium` inside the virtual environment)
- A PlayStation Network account (any region)

## Backlog

- Auto-detect when user has completed sign-in via the browser, instead of requiring manual confirmation. Include a setting to turn on manual confirmation as a fallback.
- Support fetching transactions for a user-specified date range (start date and/or end date), not just full account history.
