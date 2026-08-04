# PSN Transaction History

Export your complete PlayStation Network transaction history.

Sony hides transaction history behind infinite scroll and bot protection. This tool signs in through a real browser, securely saves the resulting session, calls the internal PSN GraphQL API directly over HTTP, and enriches results with content-type metadata from the PS Store API. A browser-based fetch transport remains available as a fallback.

Works with all major PSN regions (default US).

## Install

Create a project-local virtual environment so `python` and installed commands consistently use the required Python version:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

System Chrome or Edge is used for login. If neither is installed, add
Playwright's Chromium fallback with `python -m playwright install chromium`;
passkeys and biometric login are unavailable in that fallback browser.

Run `source .venv/bin/activate` again when returning to the project in a new terminal. The `.venv/` directory is ignored by Git.

## Usage

### 1. Log in (once)

```bash
psn-transactions login
```

A browser window opens (system Chrome with passkey support if available; Chromium as fallback). Sign in to PlayStation Store and complete any 2FA. The command detects successful sign-in automatically, validates the session with Sony, and saves it to `~/.psn-transactions/auth.json` with owner-only permissions. Keep the browser window open until the command finishes.

```bash
psn-transactions login --force              # re-authenticate
psn-transactions login --debug              # report session-cookie presence; values stay redacted
psn-transactions login --locale en-au       # set region (default: en-us)
psn-transactions login --manual-confirmation # press ENTER after signing in instead
```

Automatic detection waits for up to five minutes. Use `--manual-confirmation`
if Sony's login flow is not detected automatically.

Supported locales: `en-us` `en-gb` `en-au` `en-ca` `de-de` `fr-fr` `es-es` `it-it` `nl-nl` `pt-pt` `ja-jp` `ko-kr` `pt-br` `es-mx`

The locale is saved to `~/.psn-transactions/config.json` and reused automatically by `fetch` and `enrich`.

### 2. Fetch transaction history

```bash
psn-transactions fetch
```

Downloads all transactions to the raw snapshot `psn_transactions_raw.json`.
The completed snapshot replaces any existing file atomically, so a failed fetch
leaves the previous snapshot intact. For testing, limit to one page (100
transactions):

```bash
psn-transactions fetch --limit 1
psn-transactions fetch --output my_transactions.json
```

Fetching uses direct HTTP by default, reusing the same securely saved session
without launching a browser. The Playwright browser transport remains available
as a fallback:

```bash
psn-transactions fetch --transport http     # explicit default
psn-transactions fetch --transport browser  # fallback
```

Use the browser transport if Sony rejects the direct request. Interactive login
still uses system Chrome so passkeys, biometric authentication, and 2FA remain
available.

To fetch an inclusive date range, provide a start date, an end date, or both:

```bash
psn-transactions fetch --start 2025-01-01
psn-transactions fetch --end 2025-12-31
psn-transactions fetch --start 2025-01-01 --end 2025-12-31
```

Date bounds use your computer's local timezone by default. The detected IANA
timezone is shown when fetching, and daylight-saving changes are handled before
the boundaries are converted to UTC for PSN. Override the timezone when needed:

```bash
psn-transactions fetch --start 2025-01-01 --timezone Australia/Sydney
psn-transactions fetch --start 2025-01-01 --timezone Europe/London
psn-transactions fetch --start 2025-01-01 --timezone UTC
```

For repeatable scripts, specify `--timezone` explicitly so results do not depend
on the timezone configured on the machine running the command.

### 3. Export to CSV

```bash
psn-transactions export
psn-transactions export --output my_transactions.csv
```

This reads `psn_transactions_raw.json` and writes `psn_transactions.csv`
without making Store requests. The completed CSV replaces any existing file
atomically.

### 4. Enrich and export to CSV

```bash
psn-transactions enrich
psn-transactions enrich --paid-only
psn-transactions enrich --paid-only --summary
psn-transactions enrich --output my_transactions_enriched.csv
```

This reads `psn_transactions_raw.json`, looks up current PS Store metadata,
classifies purchases and writes `psn_transactions_enriched.csv`. It does not
modify the raw snapshot.

Use `--paid-only` to include only product items whose item-level total is
positive. Zero-cost items, negative totals such as refunds, items without a
usable numeric total and transactions without product rows are skipped before
Store lookups. The command prints a count for each group so excluded data is not
silent.

Store cache behaviour can be selected explicitly:

```bash
psn-transactions enrich --refresh
psn-transactions enrich --cache-only
```

`--refresh` repeats Store lookups instead of reusing cached results.
`--cache-only` makes no Store requests and marks uncached products as
`cache_miss`. The two options cannot be used together.

Normal runs show progress, important failures and the saved output path without
printing purchase names or identifiers. Add `--summary` for detailed aggregate
counts covering transactions, product rows, output rows, unique SKUs,
normalised lookup keys, cache hits, network requests, lookup statuses and
classification sources. No separate summary file is created.

## CSV columns

| Column | Description |
|---|---|
| `date` | Transaction date (YYYY-MM-DD HH:MM) |
| `transaction_id` | PSN transaction ID |
| `order_item_id` | Stable PSN line-item identifier when the row represents a product |
| `product` | Product name |
| `paid` | Amount paid |
| `original` | Original price before discounts |
| `discount` | Discount applied |
| `tax` | Tax component |
| `sku` | PlayStation SKU identifier |
| `payment` | Payment method |
| `card_last4` | Last 4 digits of payment card |

Product rows also include integer minor-unit values in `paid_minor`,
`original_minor`, `discount_minor`, and `tax_minor`. These avoid parsing
locale-formatted currency strings in downstream tools.

### Enriched columns

Running `psn-transactions enrich` looks up each unique SKU against the PS Store
API and reports how many records came from the cache, succeeded, were not found,
lacked useful metadata, or failed temporarily. The following columns are
present only in the enriched CSV:

Store lookups run serially through one reusable connection session. Requests
honour Store rate-limit responses and retry temporary connection and server
failures with backoff. Cache updates are checkpointed every 20 completed
lookups. Pressing Control-C saves completed results before the command exits.

| Column | Description |
|---|---|
| `category` | Classified purchase type (see below) |
| `content_type` | Normalised content type from the PS Store API |
| `top_category` | Raw top-level Store category |
| `platform` | Playable platform or platforms reported by the Store |
| `publisher` | Provider or publisher reported by the Store |
| `release_date` | Release date reported by the Store |
| `enrichment_status` | Whether Store metadata succeeded, was unavailable, or failed |
| `enrichment_detail` | Concise lookup outcome or failure detail, without response bodies |
| `classification_source` | Evidence used: `store_api`, `transaction`, `product_name`, `heuristic`, or `unknown` |
| `classification_evidence` | Specific rule or metadata value used for classification |
| `is_ps_plus` | `True` only when the product name supplies PS Plus evidence; otherwise empty |

**Category values:**

| Category | Condition |
|---|---|
| PS Plus Pack | PS Plus name evidence together with a pack or bundle name |
| PS Plus Monthly | PS Plus name evidence together with a zero item total and positive original item price |
| PS Plus Item | Other explicitly named PS Plus item |
| Full Game | Store content type or top category identifies a game |
| DLC / Add-on | Store metadata identifies add-on content, or a limited name fallback matches |
| Bundle | Store metadata identifies a bundle |
| In-Game Currency | Store metadata identifies virtual currency |
| Subscription | Store or transaction metadata identifies a subscription |
| Pre-order | Transaction metadata identifies a pre-order |
| Promotion | Transaction metadata identifies a promotion |
| Voucher | Transaction metadata identifies a voucher |
| Other | No supported evidence identified the content type |

Rows without a product list, such as wallet top-ups and refunds, use transaction
metadata and have an enrichment status of `not_applicable`.

**Enrichment status values:**

| Status | Meaning |
|---|---|
| `success` | Useful Store metadata was returned |
| `not_found` | The Store returned HTTP 404 |
| `no_metadata` | The Store response was valid but lacked useful metadata |
| `temporary_failure` | A network, rate-limit, server, or response error can be retried later |
| `cache_miss` | Cache-only mode had no reusable metadata for the SKU |
| `missing_sku` | The transaction item has no SKU to look up |
| `not_applicable` | The transaction has no product item to enrich |

SKU lookups are cached atomically in the owner-only file
`~/.psn-transactions/sku_cache.json`. Cache entries are scoped by Store locale
and source schema. A 404 is retried after seven days and a valid response with
no useful metadata is retried after one day. Transient network, rate-limit, and
Store server failures are not cached, so the next `enrich` run can retry them.
Successful results are reused until `--refresh` is requested. Changing locale
does not reuse metadata from another regional Store.

## Development

The processing code is separated by responsibility:

- `classify.py` contains pure classification rules and evidence generation.
- `enrich.py` contains the serial Store client and locale-scoped cache.
- `export.py` contains CSV schemas, transaction flattening and the explicit
  `export_csv()` and `enrich_csv()` entry points.
- `parse.py` is a small compatibility façade for older internal imports.

With the virtual environment activated, install the development extras and run the suite:

```bash
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest tests/ -v
```

## Requirements

- Python 3.11+
- System Chrome or Edge for passkey-capable login, or Playwright Chromium as a fallback
- A PlayStation Network account (any region)
