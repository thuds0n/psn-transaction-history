import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from tzlocal import get_localzone_name

from psn_transactions import config as cfg
from psn_transactions.errors import PSNTransactionsError
from psn_transactions.paths import app_dir
from psn_transactions.storage import atomic_write_json, secure_auth_file

AUTH_FILE = app_dir() / "auth.json"
GRAPHQL_HASH = "076aae24f704a963a06287c26e69f79afce2ea74ed7535109a15600577c6c479"
GRAPHQL_URL = "https://web.np.playstation.com/api/graphql/v1/op"
GRAPHQL_HEADERS = {
    "content-type": "application/json",
    "x-apollo-operation-name": "transactionHistoryRetrieve",
    "apollo-require-preflight": "true",
    "apollographql-client-name": "@sie-ppr-web-checkout/app",
    "apollographql-client-version": "2.169.1",
    "x-psn-app-ver": "@sie-ppr-web-checkout/app/v2.169.1",
    "x-psn-storefront-type": "checkout:store",
}
SUPPORTED_TRANSPORTS = {"browser", "http"}
HTTP_TIMEOUT_SECONDS = 30
DEFAULT_START_DATE = "1994-12-03T00:00:00.000Z"

# Runs inside the browser page to avoid CORS/CSRF issues
_JS_FETCH = """
async ({startDate, endDate, url, hash, headers}) => {
    const vars = JSON.stringify({
        startDate,
        endDate,
        limit: 100
    });
    const ext = JSON.stringify({
        persistedQuery: {version: 1, sha256Hash: hash}
    });
    const requestUrl = url
        + "?operationName=transactionHistoryRetrieve"
        + "&variables=" + encodeURIComponent(vars)
        + "&extensions=" + encodeURIComponent(ext);
    try {
        const res = await fetch(requestUrl, {
            credentials: "include",
            headers
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


def _parse_calendar_date(value: str, option_name: str) -> datetime:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError) as exc:
        raise PSNTransactionsError(
            f"Invalid {option_name} date {value!r}; expected YYYY-MM-DD."
        ) from exc

    if parsed.strftime("%Y-%m-%d") != value:
        raise PSNTransactionsError(
            f"Invalid {option_name} date {value!r}; expected YYYY-MM-DD."
        )
    return parsed


def _resolve_timezone(timezone_name: str | None) -> tuple[str, ZoneInfo]:
    try:
        resolved_name = timezone_name or get_localzone_name()
        return resolved_name, ZoneInfo(resolved_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        if timezone_name:
            raise PSNTransactionsError(
                f"Unknown timezone {timezone_name!r}; expected an IANA name such as "
                "Australia/Sydney or UTC."
            ) from exc
        raise PSNTransactionsError(
            "Could not detect the local timezone. Specify one explicitly with "
            "`--timezone`, for example `--timezone UTC`."
        ) from exc


def _format_utc_timestamp(value: datetime) -> str:
    utc_value = value.astimezone(timezone.utc)
    return utc_value.strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{utc_value.microsecond // 1000:03d}Z"
    )


def _resolve_date_range(
    start_date: str | None,
    end_date: str | None,
    timezone_name: str | None = None,
) -> tuple[str, str, str]:
    parsed_start = (
        _parse_calendar_date(start_date, "--start")
        if start_date is not None
        else None
    )
    parsed_end = (
        _parse_calendar_date(end_date, "--end") if end_date is not None else None
    )

    if parsed_start and parsed_end and parsed_start > parsed_end:
        raise PSNTransactionsError(
            f"Invalid date range: --start {start_date} is after --end {end_date}."
        )

    if parsed_start is None and parsed_end is None and timezone_name is None:
        return DEFAULT_START_DATE, _current_end_date(), "UTC"

    resolved_timezone_name, local_timezone = _resolve_timezone(timezone_name)
    start_timestamp = (
        _format_utc_timestamp(parsed_start.replace(tzinfo=local_timezone))
        if parsed_start
        else DEFAULT_START_DATE
    )
    if parsed_end:
        next_local_midnight = (parsed_end + timedelta(days=1)).replace(
            tzinfo=local_timezone
        )
        end_timestamp = _format_utc_timestamp(
            next_local_midnight.astimezone(timezone.utc) - timedelta(milliseconds=1)
        )
    else:
        end_timestamp = _current_end_date()
    return start_timestamp, end_timestamp, resolved_timezone_name


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


def _graphql_errors_indicate_expired_session(errors: Any) -> bool:
    if not isinstance(errors, list):
        return False
    authentication_markers = (
        "access denied",
        "unauthenticated",
        "not authorized",
        "not authorised",
    )
    return any(
        isinstance(error, dict)
        and isinstance(error.get("message"), str)
        and any(
            marker in error["message"].lower()
            for marker in authentication_markers
        )
        for error in errors
    )


def _extract_transactions(result: Any, transport: str = "browser") -> list[dict]:
    if not isinstance(result, dict):
        raise PSNTransactionsError(
            "PlayStation transaction history returned an unexpected transport response."
        )

    if result.get("requestError"):
        raise PSNTransactionsError(
            "PlayStation transaction request failed before the API returned a response: "
            f"{result['requestError']}. "
            "Please sign in again with `psn-transactions login --force` if your session has expired."
        )

    status = result.get("status")
    status_text = result.get("statusText") or ""
    if status is None:
        raise PSNTransactionsError(
            "PlayStation transaction history response did not include an HTTP status."
        )

    if not result.get("ok"):
        status_label = f"HTTP {status}"
        if status_text:
            status_label = f"{status_label} {status_text}"
        if status in {401, 403}:
            if transport == "http":
                raise PSNTransactionsError(
                    f"PlayStation Store rejected the direct HTTP request ({status_label}). "
                    "Retry with `psn-transactions fetch --transport browser`. If the browser "
                    "transport is also rejected, sign in again with "
                    "`psn-transactions login --force`."
                )
            raise PSNTransactionsError(
                f"PlayStation Store rejected the saved session ({status_label}). "
                "Please sign in again with `psn-transactions login --force`."
            )
        raise PSNTransactionsError(
            f"PlayStation transaction history request failed with {status_label}."
        )

    if result.get("parseError"):
        raw_body = result.get("rawBody") or ""
        snippet = f" Response started with: {raw_body!r}" if raw_body else ""
        message = (
            "PlayStation transaction history returned a non-JSON response."
            f"{snippet}"
        )
        if transport == "http":
            message += " Retry with `psn-transactions fetch --transport browser`."
        raise PSNTransactionsError(message)

    payload = result.get("body")
    if not isinstance(payload, dict):
        raise PSNTransactionsError(
            "PlayStation transaction history returned an empty or invalid JSON payload."
        )

    graphql_errors = payload.get("errors")
    if graphql_errors:
        formatted_errors = _format_graphql_errors(graphql_errors)
        if _graphql_errors_indicate_expired_session(graphql_errors):
            if transport == "http":
                raise PSNTransactionsError(
                    "PlayStation did not authorise the direct HTTP request: "
                    f"{formatted_errors}. Retry with "
                    "`psn-transactions fetch --transport browser`. If the browser "
                    "transport is also rejected, sign in again with "
                    "`psn-transactions login --force`."
                )
            raise PSNTransactionsError(
                "PlayStation did not authorise the saved browser session: "
                f"{formatted_errors}. Sign in again with "
                "`psn-transactions login --force`."
            )
        message = (
            "PlayStation transaction history returned GraphQL errors: "
            f"{formatted_errors}"
        )
        if transport == "http":
            message += " Retry with `psn-transactions fetch --transport browser`."
        raise PSNTransactionsError(message)

    try:
        transactions = payload["data"]["transactionHistoryRetrieve"]["transactions"]
    except (KeyError, TypeError) as exc:
        raise PSNTransactionsError(
            "PlayStation transaction history returned an unexpected response shape: "
            f"{_format_payload_snippet(payload)}"
        ) from exc

    if not isinstance(transactions, list):
        raise PSNTransactionsError(
            "PlayStation transaction history response contained a non-list "
            f"`transactions` value: {_format_payload_snippet(transactions)}"
        )

    return transactions


def _fetch_transaction_history_page(
    page, end_date: str, start_date: str = DEFAULT_START_DATE
) -> list[dict]:
    """Fetch one page through an authenticated Playwright browser page."""
    try:
        result = page.evaluate(
            _JS_FETCH,
            {
                "startDate": start_date,
                "endDate": end_date,
                "url": GRAPHQL_URL,
                "hash": GRAPHQL_HASH,
                "headers": GRAPHQL_HEADERS,
            },
        )
    except PlaywrightError as exc:
        raise PSNTransactionsError(
            f"Failed to query PlayStation transaction history: {exc}"
        ) from exc

    return _extract_transactions(result, transport="browser")


def _graphql_params(start_date: str, end_date: str) -> dict[str, str]:
    return {
        "operationName": "transactionHistoryRetrieve",
        "variables": json.dumps(
            {"startDate": start_date, "endDate": end_date, "limit": 100},
            separators=(",", ":"),
        ),
        "extensions": json.dumps(
            {"persistedQuery": {"version": 1, "sha256Hash": GRAPHQL_HASH}},
            separators=(",", ":"),
        ),
    }


def _fetch_transaction_history_page_http(
    session: requests.Session,
    end_date: str,
    start_date: str = DEFAULT_START_DATE,
) -> list[dict]:
    """Fetch one page directly over HTTP using the saved browser cookies."""
    try:
        response = session.get(
            GRAPHQL_URL,
            params=_graphql_params(start_date, end_date),
            headers=GRAPHQL_HEADERS,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise PSNTransactionsError(
            "PlayStation transaction request failed before the API returned a response: "
            f"{exc}."
        ) from exc

    raw_body = response.text
    try:
        body = response.json() if raw_body else None
        parse_error = None
    except ValueError as exc:
        body = None
        parse_error = str(exc)

    return _extract_transactions(
        {
            "ok": response.ok,
            "status": response.status_code,
            "statusText": response.reason,
            "body": body,
            "rawBody": raw_body[:500],
            "parseError": parse_error,
        },
        transport="http",
    )


def _populate_http_session(session: requests.Session) -> None:
    try:
        storage_state = json.loads(AUTH_FILE.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PSNTransactionsError(
            f"Could not load the saved browser session from {AUTH_FILE}. It may be "
            "invalid or damaged; run `psn-transactions login --force` to replace it."
        ) from exc

    cookies = storage_state.get("cookies") if isinstance(storage_state, dict) else None
    if not isinstance(cookies, list):
        raise PSNTransactionsError(
            f"Could not load the saved browser session from {AUTH_FILE}. It has an "
            "unexpected cookie format; run `psn-transactions login --force` to replace it."
        )

    try:
        for cookie in cookies:
            if not isinstance(cookie, dict):
                raise TypeError("cookie entry is not an object")
            options = {
                "path": cookie.get("path") or "/",
                "secure": bool(cookie.get("secure", False)),
            }
            domain = cookie.get("domain")
            if isinstance(domain, str) and domain:
                options["domain"] = domain
            expires = cookie.get("expires")
            if isinstance(expires, (int, float)) and expires > 0:
                options["expires"] = int(expires)
            session.cookies.set(cookie["name"], cookie["value"], **options)
    except (KeyError, TypeError, ValueError) as exc:
        raise PSNTransactionsError(
            f"Could not load cookies from the saved browser session at {AUTH_FILE}. "
            "Run `psn-transactions login --force` to replace it."
        ) from exc


def _pagination_end_date(transaction: Any, page_number: int) -> str:
    if not isinstance(transaction, dict):
        raise PSNTransactionsError(
            f"Cannot paginate after page {page_number}: its final transaction is not an object."
        )

    transaction_date = transaction.get("date")
    if not isinstance(transaction_date, str) or not transaction_date.strip():
        raise PSNTransactionsError(
            f"Cannot paginate after page {page_number}: its final transaction has no valid "
            "`date` string."
        )

    try:
        return _subtract_1ms(transaction_date)
    except (TypeError, ValueError) as exc:
        transaction_id = transaction.get("id")
        record_label = f" {transaction_id!r}" if transaction_id is not None else ""
        raise PSNTransactionsError(
            f"Cannot paginate after page {page_number}: transaction{record_label} has malformed "
            f"date {transaction_date!r}; expected an ISO 8601 timestamp with a timezone."
        ) from exc


def _fetch_pages(
    fetch_page,
    start_timestamp: str,
    end_timestamp: str,
    limit: int | None,
) -> list:
    all_tx = []
    page_end_date = end_timestamp

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Fetching transactions...", total=None)
        page_num = 0

        while True:
            txs = fetch_page(page_end_date)

            if not txs:
                break

            all_tx.extend(txs)
            page_num += 1
            progress.update(
                task,
                description=f"Fetched {len(all_tx)} transactions (page {page_num})...",
            )

            if limit is not None and page_num >= limit:
                break

            next_end_date = _pagination_end_date(txs[-1], page_num)
            if next_end_date >= page_end_date:
                raise PSNTransactionsError(
                    f"Pagination did not advance after page {page_num}: Sony "
                    f"returned final transaction date {txs[-1]['date']!r} again. "
                    "No output was written."
                )
            if next_end_date < start_timestamp:
                break
            page_end_date = next_end_date
            time.sleep(0.3)

    return all_tx


def _launch_fetch_browser(p):
    for channel in ("chrome", "msedge"):
        try:
            return p.chromium.launch(channel=channel, headless=True)
        except PlaywrightError:
            pass

    try:
        return p.chromium.launch(headless=True)
    except PlaywrightError as exc:
        raise PSNTransactionsError(
            "Could not launch Chrome, Edge, or Playwright Chromium for fetching. "
            "Install Chrome or add the fallback with "
            "`python3 -m playwright install chromium`, then try again. "
            f"Playwright reported: {exc}"
        ) from exc


def _fetch_with_browser(
    start_timestamp: str, end_timestamp: str, limit: int | None
) -> list:
    try:
        with sync_playwright() as p:
            browser = _launch_fetch_browser(p)

            try:
                try:
                    context = browser.new_context(storage_state=str(AUTH_FILE))
                except PlaywrightError as exc:
                    raise PSNTransactionsError(
                        f"Could not load the saved browser session from {AUTH_FILE}. It may be "
                        "invalid or damaged; run `psn-transactions login --force` to replace it. "
                        f"Playwright reported: {exc}"
                    ) from exc
                try:
                    page = context.new_page()
                except PlaywrightError as exc:
                    raise PSNTransactionsError(
                        "Could not open a browser page for fetching. "
                        f"Playwright reported: {exc}"
                    ) from exc

                store_url = cfg.store_url(cfg.get_locale())
                console.print(f"Navigating to PlayStation Store ({store_url})...")
                try:
                    page.goto(store_url)
                except PlaywrightError as exc:
                    raise PSNTransactionsError(
                        "Could not navigate to PlayStation Store before fetching. Check your "
                        f"network connection and try again. Playwright reported: {exc}"
                    ) from exc

                return _fetch_pages(
                    lambda page_end_date: _fetch_transaction_history_page(
                        page,
                        page_end_date,
                        start_timestamp,
                    ),
                    start_timestamp,
                    end_timestamp,
                    limit,
                )
            finally:
                active_error = sys.exc_info()[0] is not None
                try:
                    browser.close()
                except PlaywrightError as exc:
                    if not active_error:
                        raise PSNTransactionsError(
                            "Fetching completed, but the browser could not be closed cleanly. "
                            f"Playwright reported: {exc}"
                        ) from exc
    except PSNTransactionsError:
        raise
    except PlaywrightError as exc:
        raise PSNTransactionsError(
            "Could not start Playwright for fetching. "
            f"Playwright reported: {exc}"
        ) from exc


def _fetch_with_http(
    start_timestamp: str, end_timestamp: str, limit: int | None
) -> list:
    with requests.Session() as session:
        _populate_http_session(session)
        return _fetch_pages(
            lambda page_end_date: _fetch_transaction_history_page_http(
                session,
                page_end_date,
                start_timestamp,
            ),
            start_timestamp,
            end_timestamp,
            limit,
        )


def fetch_all(
    output_path: str = "psn_transactions.json",
    limit: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    timezone_name: str | None = None,
    transport: str = "http",
) -> list:
    """Fetch transaction history using the selected transport and optional date bounds."""
    if transport not in SUPPORTED_TRANSPORTS:
        supported = ", ".join(sorted(SUPPORTED_TRANSPORTS))
        raise PSNTransactionsError(
            f"Unknown fetch transport {transport!r}; expected one of: {supported}."
        )

    start_timestamp, end_timestamp, resolved_timezone_name = _resolve_date_range(
        start_date,
        end_date,
        timezone_name,
    )

    if not AUTH_FILE.exists():
        raise FileNotFoundError(
            f"No auth session at {AUTH_FILE}. Run: psn-transactions login"
        )
    secure_auth_file(AUTH_FILE)

    if start_date is not None or end_date is not None:
        console.print(
            f"Interpreting date range in [bold]{resolved_timezone_name}[/bold]."
        )
    console.print(f"Using [bold]{transport}[/bold] fetch transport.")

    if transport == "http":
        all_tx = _fetch_with_http(start_timestamp, end_timestamp, limit)
    else:
        all_tx = _fetch_with_browser(start_timestamp, end_timestamp, limit)

    output_file = Path(output_path)
    atomic_write_json(output_file, all_tx)
    console.print(f"✓ Saved [bold]{len(all_tx)}[/bold] transactions to {output_path}")
    return all_tx
