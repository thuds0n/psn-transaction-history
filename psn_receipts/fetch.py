import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from psn_receipts import config as cfg

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
    return await res.json();
}
"""

console = Console()


def _subtract_1ms(iso: str) -> str:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    dt -= timedelta(milliseconds=1)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def fetch_all(output_path: str = "psn_history_full.json", limit: int = None) -> list:
    """Fetch full transaction history using saved session. limit= caps page count (for testing)."""
    if not AUTH_FILE.exists():
        raise FileNotFoundError(
            f"No auth session at {AUTH_FILE}. Run: psn-receipts login"
        )

    all_tx = []
    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(AUTH_FILE))
        page = context.new_page()

        store_url = cfg.store_url(cfg.load().get("locale", "en-au"))
        console.print(f"Navigating to PlayStation Store ({store_url})...")
        page.goto(store_url)

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task("Fetching transactions...", total=None)
            page_num = 0

            while True:
                data = page.evaluate(_JS_FETCH, {"endDate": end_date})
                txs = (
                    (data.get("data") or {})
                    .get("transactionHistoryRetrieve", {})
                    .get("transactions", [])
                )

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

                end_date = _subtract_1ms(txs[-1]["date"])
                time.sleep(0.3)

        browser.close()

    Path(output_path).write_text(json.dumps(all_tx, indent=2))
    console.print(f"✓ Saved [bold]{len(all_tx)}[/bold] transactions to {output_path}")
    return all_tx
