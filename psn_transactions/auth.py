import json
import os
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from psn_transactions import config as cfg
from psn_transactions.errors import PSNTransactionsError
from psn_transactions.paths import app_dir
from psn_transactions.storage import secure_auth_directory, secure_auth_file

AUTH_DIR = app_dir()
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
        raise PSNTransactionsError(
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
        raise PSNTransactionsError(
            "Could not contact Sony's session-validation endpoint. "
            f"Playwright reported: {exc}"
        ) from exc

    if response is None:
        raise PSNTransactionsError(
            "Sony's session-validation endpoint did not return a response."
        )

    if response.status in {401, 403}:
        raise PSNTransactionsError(
            f"Sony rejected the browser session (HTTP {response.status})."
        )
    if response.status >= 400:
        raise PSNTransactionsError(
            "Sony's session-validation endpoint failed "
            f"(HTTP {response.status})."
        )

    try:
        payload = json.loads(body or "")
    except (json.JSONDecodeError, TypeError) as exc:
        raise PSNTransactionsError(
            "Sony's session-validation endpoint returned an unexpected response."
        ) from exc

    npsso = payload.get("npsso") if isinstance(payload, dict) else None
    if not isinstance(npsso, str) or not npsso.strip():
        raise PSNTransactionsError(
            "Sony did not confirm an authenticated PlayStation Network session."
        )


def _save_storage_state(context) -> None:
    temporary_path = None
    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=".auth.", suffix=".json", dir=AUTH_DIR
        )
        os.close(file_descriptor)
        temporary_path = Path(temporary_name)
        context.storage_state(path=str(temporary_path))
        temporary_path.chmod(0o600)
        os.replace(temporary_path, AUTH_FILE)
    except PlaywrightError as exc:
        raise PSNTransactionsError(
            f"Could not save the browser session to {AUTH_FILE}. "
            f"Playwright reported: {exc}"
        ) from exc
    except OSError as exc:
        raise PSNTransactionsError(
            f"Could not save the browser session securely to {AUTH_FILE}: {exc}"
        ) from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def login(force: bool = False, debug: bool = False, locale: str = None) -> None:
    if AUTH_FILE.exists() and not force:
        secure_auth_file(AUTH_FILE)
        print(f"Already logged in ({AUTH_FILE}). Use --force to re-authenticate.")
        print(f"Current locale: {cfg.get_locale()}")
        return

    secure_auth_directory(AUTH_DIR)

    locale = locale or cfg.get_locale()
    url = cfg.store_url(locale)

    try:
        with sync_playwright() as p:
            browser, browser_name = _launch_browser(p)
            try:
                try:
                    context = browser.new_context()
                except PlaywrightError as exc:
                    raise PSNTransactionsError(
                        "Could not create a browser context for sign-in. "
                        f"Playwright reported: {exc}"
                    ) from exc
                try:
                    page = context.new_page()
                except PlaywrightError as exc:
                    raise PSNTransactionsError(
                        "Could not open a browser page for sign-in. "
                        f"Playwright reported: {exc}"
                    ) from exc

                print(f"\nUsing {browser_name} — opening {url}")
                try:
                    page.goto(url)
                except PlaywrightError as exc:
                    raise PSNTransactionsError(
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
                        raise PSNTransactionsError(
                            "Could not inspect the browser session. "
                            f"Playwright reported: {exc}"
                        ) from exc
                    found = [c for c in cookies if c["name"] in DEBUG_COOKIES]
                    if found:
                        print("\nExpected cookies present (values redacted):")
                        for cookie in found:
                            print(f"  {cookie['name']}: present")
                    else:
                        print("  (none of the expected cookies found — are you signed in?)")

                print("\nValidating signed-in session...")
                try:
                    _validate_authenticated_session(page)
                except PSNTransactionsError as exc:
                    raise PSNTransactionsError(
                        f"Sign-in validation failed: {exc}\n"
                        "Your session was not saved. Complete sign-in in the browser and try "
                        "`psn-transactions login --force` again."
                    ) from exc

                _save_storage_state(context)
                secure_auth_file(AUTH_FILE)
                try:
                    cfg.save({"locale": locale})
                except PSNTransactionsError as exc:
                    raise PSNTransactionsError(
                        f"The session was saved, but the locale configuration could not be "
                        f"updated: {exc}"
                    ) from exc
                print(f"\n✓ Session saved to {AUTH_FILE}")
                print(f"✓ Locale set to {locale}")
            finally:
                active_error = sys.exc_info()[0] is not None
                try:
                    browser.close()
                except PlaywrightError as exc:
                    if not active_error:
                        raise PSNTransactionsError(
                            "The browser session completed, but the browser could not be "
                            f"closed cleanly. Playwright reported: {exc}"
                        ) from exc
    except PSNTransactionsError:
        raise
    except PlaywrightError as exc:
        raise PSNTransactionsError(
            "Could not start Playwright for sign-in. "
            f"Playwright reported: {exc}"
        ) from exc
