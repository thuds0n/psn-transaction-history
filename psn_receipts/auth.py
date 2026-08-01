import json
import sys
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from psn_receipts import config as cfg
from psn_receipts.errors import PSNReceiptsError

AUTH_DIR = Path.home() / ".psn-receipts"
AUTH_FILE = AUTH_DIR / "auth.json"
AUTH_VALIDATION_URL = "https://ca.account.sony.com/api/v1/ssocookie"

DEBUG_COOKIES = {"npsso", "JSESSIONID", "isSignedIn", "_abck"}


def _launch_browser(p):
    """Launch system Chrome for passkey/biometric support; fall back to bundled Chromium."""
    try:
        browser = p.chromium.launch(channel="chrome", headless=False)
        return browser, "Chrome"
    except PlaywrightError:
        pass
    try:
        browser = p.chromium.launch(channel="msedge", headless=False)
        return browser, "Edge"
    except PlaywrightError:
        pass
    print(
        "Note: system Chrome/Edge not found. Falling back to Playwright's Chromium.\n"
        "      Passkeys and biometric login won't be available in this mode.\n"
        "      Install Chrome for full passkey support.\n"
    )
    try:
        browser = p.chromium.launch(headless=False)
    except PlaywrightError as exc:
        raise PSNReceiptsError(
            "Could not launch Chrome, Edge, or Playwright Chromium. "
            "Install the browser with `python3 -m playwright install chromium` "
            f"and try again. Playwright reported: {exc}"
        ) from exc
    return browser, "Chromium"


def _validate_authenticated_session(page) -> None:
    """Prove Sony authentication without depending on transaction history."""
    try:
        response = page.goto(AUTH_VALIDATION_URL)
        body = page.text_content("body")
    except PlaywrightError as exc:
        raise PSNReceiptsError(
            "Could not contact Sony's session-validation endpoint. "
            f"Playwright reported: {exc}"
        ) from exc

    if response is None:
        raise PSNReceiptsError(
            "Sony's session-validation endpoint did not return a response."
        )

    if response.status in {401, 403}:
        raise PSNReceiptsError(
            f"Sony rejected the browser session (HTTP {response.status})."
        )
    if response.status >= 400:
        raise PSNReceiptsError(
            "Sony's session-validation endpoint failed "
            f"(HTTP {response.status})."
        )

    try:
        payload = json.loads(body or "")
    except (json.JSONDecodeError, TypeError) as exc:
        raise PSNReceiptsError(
            "Sony's session-validation endpoint returned an unexpected response."
        ) from exc

    npsso = payload.get("npsso") if isinstance(payload, dict) else None
    if not isinstance(npsso, str) or not npsso.strip():
        raise PSNReceiptsError(
            "Sony did not confirm an authenticated PlayStation Network session."
        )


def login(force: bool = False, debug: bool = False, locale: str = None) -> None:
    if AUTH_FILE.exists() and not force:
        print(f"Already logged in ({AUTH_FILE}). Use --force to re-authenticate.")
        print(f"Current locale: {cfg.get_locale()}")
        return

    AUTH_DIR.mkdir(parents=True, exist_ok=True)

    locale = locale or cfg.get_locale()
    url = cfg.store_url(locale)

    try:
        with sync_playwright() as p:
            browser, browser_name = _launch_browser(p)
            try:
                try:
                    context = browser.new_context()
                except PlaywrightError as exc:
                    raise PSNReceiptsError(
                        "Could not create a browser context for sign-in. "
                        f"Playwright reported: {exc}"
                    ) from exc
                try:
                    page = context.new_page()
                except PlaywrightError as exc:
                    raise PSNReceiptsError(
                        "Could not open a browser page for sign-in. "
                        f"Playwright reported: {exc}"
                    ) from exc

                print(f"\nUsing {browser_name} — opening {url}")
                try:
                    page.goto(url)
                except PlaywrightError as exc:
                    raise PSNReceiptsError(
                        "Could not navigate to PlayStation Store for sign-in. "
                        f"Playwright reported: {exc}"
                    ) from exc

                print("Sign in to PlayStation Store in the browser window.")
                print("Complete any 2FA if prompted, then return here.")
                input("Press ENTER once you are signed in... ")

                if debug:
                    try:
                        cookies = context.cookies()
                    except PlaywrightError as exc:
                        raise PSNReceiptsError(
                            "Could not inspect the browser session. "
                            f"Playwright reported: {exc}"
                        ) from exc
                    found = [c for c in cookies if c["name"] in DEBUG_COOKIES]
                    if found:
                        print("\nCookies:")
                        for cookie in found:
                            preview = cookie["value"][:40] + (
                                "..." if len(cookie["value"]) > 40 else ""
                            )
                            print(f"  {cookie['name']}: {preview}")
                    else:
                        print("  (none of the expected cookies found — are you signed in?)")

                print("\nValidating signed-in session...")
                try:
                    _validate_authenticated_session(page)
                except PSNReceiptsError as exc:
                    raise PSNReceiptsError(
                        f"Sign-in validation failed: {exc}\n"
                        "Your session was not saved. Complete sign-in in the browser and try "
                        "`psn-receipts login --force` again."
                    ) from exc

                try:
                    context.storage_state(path=str(AUTH_FILE))
                except PlaywrightError as exc:
                    raise PSNReceiptsError(
                        f"Could not save the browser session to {AUTH_FILE}. "
                        f"Playwright reported: {exc}"
                    ) from exc
                cfg.save({"locale": locale})
                print(f"\n✓ Session saved to {AUTH_FILE}")
                print(f"✓ Locale set to {locale}")
            finally:
                active_error = sys.exc_info()[0] is not None
                try:
                    browser.close()
                except PlaywrightError as exc:
                    if not active_error:
                        raise PSNReceiptsError(
                            "The browser session completed, but the browser could not be "
                            f"closed cleanly. Playwright reported: {exc}"
                        ) from exc
    except PSNReceiptsError:
        raise
    except PlaywrightError as exc:
        raise PSNReceiptsError(
            "Could not start Playwright for sign-in. "
            f"Playwright reported: {exc}"
        ) from exc
