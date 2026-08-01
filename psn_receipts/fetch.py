import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from psn_receipts import config as cfg
from psn_receipts.errors import PSNReceiptsError
from psn_receipts.storage import atomic_write_json, secure_auth_file

AUTH_FILE = Path.home() / ".psn-receipts" / "auth.json"
GRAPHQL_HASH = "076aae24f704a963a06287c26e69f79afce2ea74ed7535109a15600577c6c479"

# Runs inside the browser page to avoid CORS/CSRF issues
_JS_FETCH = """
async ({endDate}) => {
    const HASH = "076aae24f704a963a06287c26e69f79afce2ea74ed7535109a15600577c6c479";
    const vars = JSON.stringify({
        startDate: "1994-12-03T00:00:00.000Z",
        endDate,
        limit: 100
    });
    const ext = JSON.stringify({
        persistedQuery: {version: 1, sha256Hash: HASH}
    });
    const url = "https://web.np.playstation.com/api/graphql/v1/op"
        + "?operationName=transactionHistoryRetrieve"
        + "&variables=" + encodeURIComponent(vars)
        + "&extensions=" + encodeURIComponent(ext);
    try {
        const res = await fetch(url, {
            credentials: "include",
            headers: {
                "content-type": "application/json",
                "x-apollo-operation-name": "transactionHistoryRetrieve",
                "apollo-require-preflight": "true",
                "apollographql-client-name": "@sie-ppr-web-checkout/app",
                "apollographql-client-version": "2.169.1",
                "x-psn-app-ver": "@sie-ppr-web-checkout/app/v2.169.1",
                "x-psn-storefront-type": "checkout:store"
            }
        });
        const rawBody = await res.text();
        let body = null;
        let parseError = null;
        if (rawBody) {
            try {
                body = JSON.parse(rawBody);
            } catch (error) {
                parseError = error?.message ?? String(error);
            }
        }
        return {
            ok: res.ok,
            status: res.status,
            statusText: res.statusText,
            body,
            rawBody: rawBody.slice(0, 500),
            parseError
        };
    } catch (error) {
        return {
            requestError: error?.message ?? String(error)
        };
    }
}
"""

console = Console()


def _subtract_1ms(iso: str) -> str:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    dt = dt.astimezone(timezone.utc)
    dt -= timedelta(milliseconds=1)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _current_end_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _format_payload_snippet(payload: Any) -> str:
    snippet = json.dumps(payload, ensure_ascii=True, sort_keys=True)
    if len(snippet) > 300:
        snippet = snippet[:297] + "..."
    return snippet


def _format_graphql_errors(errors: Any) -> str:
    if not isinstance(errors, list) or not errors:
        return "unknown GraphQL error"

    messages = []
    for error in errors[:3]:
        if isinstance(error, dict):
            message = error.get("message") or _format_payload_snippet(error)
        else:
            message = str(error)
        if message:
            messages.append(message)

    return "; ".join(messages) if messages else "unknown GraphQL error"


def _fetch_transaction_history_page(page, end_date: str) -> list[dict]:
    try:
        result = page.evaluate(_JS_FETCH, {"endDate": end_date})
    except PlaywrightError as exc:
        raise PSNReceiptsError(
            f"Failed to query PlayStation transaction history: {exc}"
        ) from exc

    if not isinstance(result, dict):
        raise PSNReceiptsError(
            "PlayStation transaction history returned an unexpected browser response."
        )

    if result.get("requestError"):
        raise PSNReceiptsError(
            "PlayStation transaction request failed before the API returned a response: "
            f"{result['requestError']}. "
            "Please sign in again with `psn-receipts login --force` if your session has expired."
        )

    status = result.get("status")
    status_text = result.get("statusText") or ""
    if status is None:
        raise PSNReceiptsError(
            "PlayStation transaction history response did not include an HTTP status."
        )

    if not result.get("ok"):
        status_label = f"HTTP {status}"
        if status_text:
            status_label = f"{status_label} {status_text}"
        if status in {401, 403}:
            raise PSNReceiptsError(
                f"PlayStation Store rejected the saved session ({status_label}). "
                "Please sign in again with `psn-receipts login --force`."
            )
        raise PSNReceiptsError(
            f"PlayStation transaction history request failed with {status_label}."
        )

    if result.get("parseError"):
        raw_body = result.get("rawBody") or ""
        snippet = f" Response started with: {raw_body!r}" if raw_body else ""
        raise PSNReceiptsError(
            "PlayStation transaction history returned a non-JSON response."
            f"{snippet}"
        )

    payload = result.get("body")
    if not isinstance(payload, dict):
        raise PSNReceiptsError(
            "PlayStation transaction history returned an empty or invalid JSON payload."
        )

    graphql_errors = payload.get("errors")
    if graphql_errors:
        raise PSNReceiptsError(
            "PlayStation transaction history returned GraphQL errors: "
            f"{_format_graphql_errors(graphql_errors)}"
        )

    try:
        transactions = payload["data"]["transactionHistoryRetrieve"]["transactions"]
    except (KeyError, TypeError) as exc:
        raise PSNReceiptsError(
            "PlayStation transaction history returned an unexpected response shape: "
            f"{_format_payload_snippet(payload)}"
        ) from exc

    if not isinstance(transactions, list):
        raise PSNReceiptsError(
            "PlayStation transaction history response contained a non-list "
            f"`transactions` value: {_format_payload_snippet(transactions)}"
        )

    return transactions


def _pagination_end_date(transaction: Any, page_number: int) -> str:
    if not isinstance(transaction, dict):
        raise PSNReceiptsError(
            f"Cannot paginate after page {page_number}: its final transaction is not an object."
        )

    transaction_date = transaction.get("date")
    if not isinstance(transaction_date, str) or not transaction_date.strip():
        raise PSNReceiptsError(
            f"Cannot paginate after page {page_number}: its final transaction has no valid "
            "`date` string."
        )

    try:
        return _subtract_1ms(transaction_date)
    except (TypeError, ValueError) as exc:
        transaction_id = transaction.get("id")
        record_label = f" {transaction_id!r}" if transaction_id is not None else ""
        raise PSNReceiptsError(
            f"Cannot paginate after page {page_number}: transaction{record_label} has malformed "
            f"date {transaction_date!r}; expected an ISO 8601 timestamp with a timezone."
        ) from exc


def fetch_all(output_path: str = "psn_transactions.json", limit: int = None) -> list:
    """Fetch full transaction history using saved session. limit= caps page count (for testing)."""
    if not AUTH_FILE.exists():
        raise FileNotFoundError(
            f"No auth session at {AUTH_FILE}. Run: psn-receipts login"
        )
    secure_auth_file(AUTH_FILE)

    all_tx = []
    end_date = _current_end_date()

    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
            except PlaywrightError as exc:
                raise PSNReceiptsError(
                    "Could not launch Playwright Chromium for fetching. Install it with "
                    "`python3 -m playwright install chromium` and try again. "
                    f"Playwright reported: {exc}"
                ) from exc

            try:
                try:
                    context = browser.new_context(storage_state=str(AUTH_FILE))
                except PlaywrightError as exc:
                    raise PSNReceiptsError(
                        f"Could not load the saved browser session from {AUTH_FILE}. It may be "
                        "invalid or damaged; run `psn-receipts login --force` to replace it. "
                        f"Playwright reported: {exc}"
                    ) from exc
                try:
                    page = context.new_page()
                except PlaywrightError as exc:
                    raise PSNReceiptsError(
                        "Could not open a browser page for fetching. "
                        f"Playwright reported: {exc}"
                    ) from exc

                store_url = cfg.store_url(cfg.get_locale())
                console.print(f"Navigating to PlayStation Store ({store_url})...")
                try:
                    page.goto(store_url)
                except PlaywrightError as exc:
                    raise PSNReceiptsError(
                        "Could not navigate to PlayStation Store before fetching. Check your "
                        f"network connection and try again. Playwright reported: {exc}"
                    ) from exc

                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    console=console,
                    transient=True,
                ) as progress:
                    task = progress.add_task("Fetching transactions...", total=None)
                    page_num = 0

                    while True:
                        txs = _fetch_transaction_history_page(page, end_date)

                        if not txs:
                            break

                        all_tx.extend(txs)
                        page_num += 1
                        progress.update(
                            task,
                            description=(
                                f"Fetched {len(all_tx)} transactions (page {page_num})..."
                            ),
                        )

                        if limit is not None and page_num >= limit:
                            break

                        next_end_date = _pagination_end_date(txs[-1], page_num)
                        if next_end_date >= end_date:
                            raise PSNReceiptsError(
                                f"Pagination did not advance after page {page_num}: Sony "
                                f"returned final transaction date {txs[-1]['date']!r} again. "
                                "No output was written."
                            )
                        end_date = next_end_date
                        time.sleep(0.3)
            finally:
                active_error = sys.exc_info()[0] is not None
                try:
                    browser.close()
                except PlaywrightError as exc:
                    if not active_error:
                        raise PSNReceiptsError(
                            "Fetching completed, but the browser could not be closed cleanly. "
                            f"Playwright reported: {exc}"
                        ) from exc
    except PSNReceiptsError:
        raise
    except PlaywrightError as exc:
        raise PSNReceiptsError(
            "Could not start Playwright for fetching. "
            f"Playwright reported: {exc}"
        ) from exc

    output_file = Path(output_path)
    atomic_write_json(output_file, all_tx)
    console.print(f"✓ Saved [bold]{len(all_tx)}[/bold] transactions to {output_path}")
    return all_tx
