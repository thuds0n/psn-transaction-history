from pathlib import Path
from playwright.sync_api import sync_playwright

from psn_receipts import config as cfg

AUTH_DIR = Path.home() / ".psn-receipts"
AUTH_FILE = AUTH_DIR / "auth.json"

DEBUG_COOKIES = {"npsso", "JSESSIONID", "isSignedIn", "_abck"}


def _launch_browser(p):
    """Launch system Chrome for passkey/biometric support; fall back to bundled Chromium."""
    try:
        browser = p.chromium.launch(channel="chrome", headless=False)
        return browser, "Chrome"
    except Exception:
        pass
    try:
        browser = p.chromium.launch(channel="msedge", headless=False)
        return browser, "Edge"
    except Exception:
        pass
    print(
        "Note: system Chrome/Edge not found. Falling back to Playwright's Chromium.\n"
        "      Passkeys and biometric login won't be available in this mode.\n"
        "      Install Chrome for full passkey support.\n"
    )
    browser = p.chromium.launch(headless=False)
    return browser, "Chromium"


def login(force: bool = False, debug: bool = False, locale: str = None) -> None:
    if AUTH_FILE.exists() and not force:
        conf = cfg.load()
        print(f"Already logged in ({AUTH_FILE}). Use --force to re-authenticate.")
        print(f"Current locale: {conf.get('locale', 'en-au')}")
        return

    AUTH_DIR.mkdir(parents=True, exist_ok=True)

    locale = locale or cfg.load().get("locale", "en-au")
    url = cfg.store_url(locale)

    with sync_playwright() as p:
        browser, browser_name = _launch_browser(p)
        context = browser.new_context()
        page = context.new_page()

        print(f"\nUsing {browser_name} — opening {url}")
        page.goto(url)

        print("Sign in to PlayStation Store in the browser window.")
        print("Complete any 2FA if prompted, then return here.")
        input("Press ENTER once you are signed in... ")

        if debug:
            cookies = context.cookies()
            found = [c for c in cookies if c["name"] in DEBUG_COOKIES]
            if found:
                print("\nCookies:")
                for c in found:
                    preview = c["value"][:40] + ("..." if len(c["value"]) > 40 else "")
                    print(f"  {c['name']}: {preview}")
            else:
                print("  (none of the expected cookies found — are you signed in?)")

        context.storage_state(path=str(AUTH_FILE))
        cfg.save({"locale": locale})
        print(f"\n✓ Session saved to {AUTH_FILE}")
        print(f"✓ Locale set to {locale}")
        browser.close()
