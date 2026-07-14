import builtins
import json

import pytest

from psn_receipts import auth, config as cfg, fetch, parse
from psn_receipts.errors import PSNReceiptsError


def success_result(transactions):
    return {
        "ok": True,
        "status": 200,
        "statusText": "OK",
        "body": {
            "data": {
                "transactionHistoryRetrieve": {
                    "transactions": transactions,
                }
            }
        },
    }


class FakePage:
    def __init__(self, evaluate_results=None):
        self.evaluate_results = list(evaluate_results or [])
        self.goto_calls = []

    def goto(self, url):
        self.goto_calls.append(url)

    def evaluate(self, script, payload):
        result = self.evaluate_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeContext:
    def __init__(self, page):
        self.page = page
        self.storage_state_calls = []
        self.cookies_result = []

    def new_page(self):
        return self.page

    def cookies(self):
        return list(self.cookies_result)

    def storage_state(self, path):
        self.storage_state_calls.append(path)


class FakeBrowser:
    def __init__(self, context):
        self.context = context
        self.closed = False
        self.new_context_calls = []

    def new_context(self, **kwargs):
        self.new_context_calls.append(kwargs)
        return self.context

    def close(self):
        self.closed = True


class FakePlaywrightRunner:
    def __init__(self, browser):
        self.browser = browser

    def __enter__(self):
        return FakePlaywright(self.browser)

    def __exit__(self, exc_type, exc, tb):
        return False


class FakePlaywright:
    def __init__(self, browser):
        self.browser = browser
        self.chromium = self

    def launch(self, **kwargs):
        return self.browser


def test_fetch_all_writes_transactions_after_valid_responses(tmp_path, monkeypatch):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{}")
    output_path = tmp_path / "history.json"

    transactions = [{"id": "TX001", "date": "2025-01-15T10:00:00.000Z"}]
    page = FakePage([success_result(transactions), success_result([])])
    context = FakeContext(page)
    browser = FakeBrowser(context)

    monkeypatch.setattr(fetch, "AUTH_FILE", auth_file)
    monkeypatch.setattr(fetch, "sync_playwright", lambda: FakePlaywrightRunner(browser))
    monkeypatch.setattr(fetch.time, "sleep", lambda _: None)
    monkeypatch.setattr(fetch.cfg, "get_locale", lambda: "en-gb")

    result = fetch.fetch_all(output_path=str(output_path))

    assert result == transactions
    assert json.loads(output_path.read_text()) == transactions
    assert page.goto_calls == [cfg.store_url("en-gb")]


def test_fetch_all_allows_empty_first_page(tmp_path, monkeypatch):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{}")
    output_path = tmp_path / "empty.json"

    page = FakePage([success_result([])])
    context = FakeContext(page)
    browser = FakeBrowser(context)

    monkeypatch.setattr(fetch, "AUTH_FILE", auth_file)
    monkeypatch.setattr(fetch, "sync_playwright", lambda: FakePlaywrightRunner(browser))
    monkeypatch.setattr(fetch.cfg, "get_locale", lambda: cfg.DEFAULT_LOCALE)

    result = fetch.fetch_all(output_path=str(output_path))

    assert result == []
    assert json.loads(output_path.read_text()) == []


def test_fetch_all_does_not_write_partial_output_on_failure(tmp_path, monkeypatch):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{}")
    output_path = tmp_path / "partial.json"

    transactions = [{"id": "TX001", "date": "2025-01-15T10:00:00.000Z"}]
    page = FakePage(
        [
            success_result(transactions),
            {
                "ok": True,
                "status": 200,
                "statusText": "OK",
                "body": {"errors": [{"message": "UNAUTHENTICATED"}]},
            },
        ]
    )
    context = FakeContext(page)
    browser = FakeBrowser(context)

    monkeypatch.setattr(fetch, "AUTH_FILE", auth_file)
    monkeypatch.setattr(fetch, "sync_playwright", lambda: FakePlaywrightRunner(browser))
    monkeypatch.setattr(fetch.time, "sleep", lambda _: None)
    monkeypatch.setattr(fetch.cfg, "get_locale", lambda: cfg.DEFAULT_LOCALE)

    with pytest.raises(PSNReceiptsError, match="GraphQL errors"):
        fetch.fetch_all(output_path=str(output_path))

    assert not output_path.exists()


def test_fetch_helper_raises_on_graphql_errors():
    page = FakePage(
        [
            {
                "ok": True,
                "status": 200,
                "statusText": "OK",
                "body": {"errors": [{"message": "UNAUTHENTICATED"}]},
            }
        ]
    )

    with pytest.raises(PSNReceiptsError, match="UNAUTHENTICATED"):
        fetch._fetch_transaction_history_page(page, "2025-01-01T00:00:00.000Z")


def test_fetch_helper_raises_on_malformed_response():
    page = FakePage(
        [
            {
                "ok": True,
                "status": 200,
                "statusText": "OK",
                "body": {"data": {"transactionHistoryRetrieve": {}}},
            }
        ]
    )

    with pytest.raises(PSNReceiptsError, match="unexpected response shape"):
        fetch._fetch_transaction_history_page(page, "2025-01-01T00:00:00.000Z")


def test_fetch_helper_raises_on_page_evaluate_failure():
    page = FakePage([RuntimeError("page crashed")])

    with pytest.raises(PSNReceiptsError, match="page crashed"):
        fetch._fetch_transaction_history_page(page, "2025-01-01T00:00:00.000Z")


def test_auth_login_saves_state_only_after_validation(tmp_path, monkeypatch):
    auth_dir = tmp_path / ".psn-receipts"
    auth_file = auth_dir / "auth.json"
    page = FakePage()
    context = FakeContext(page)
    browser = FakeBrowser(context)
    saved = []
    validated = []

    monkeypatch.setattr(auth, "AUTH_DIR", auth_dir)
    monkeypatch.setattr(auth, "AUTH_FILE", auth_file)
    monkeypatch.setattr(auth, "sync_playwright", lambda: FakePlaywrightRunner(browser))
    monkeypatch.setattr(auth, "_launch_browser", lambda p: (browser, "Chromium"))
    monkeypatch.setattr(builtins, "input", lambda _: "")
    monkeypatch.setattr(auth.cfg, "get_locale", lambda: cfg.DEFAULT_LOCALE)
    monkeypatch.setattr(auth.cfg, "save", lambda data: saved.append(data))
    monkeypatch.setattr(auth, "_current_end_date", lambda: "2025-01-01T00:00:00.000Z")

    def fake_validate(page_arg, end_date):
        validated.append((page_arg, end_date))
        return []

    monkeypatch.setattr(auth, "_fetch_transaction_history_page", fake_validate)

    auth.login()

    assert validated == [(page, "2025-01-01T00:00:00.000Z")]
    assert context.storage_state_calls == [str(auth_file)]
    assert saved == [{"locale": cfg.DEFAULT_LOCALE}]
    assert page.goto_calls == [
        cfg.store_url(cfg.DEFAULT_LOCALE),
        cfg.store_url(cfg.DEFAULT_LOCALE),
    ]
    assert browser.closed is True


def test_auth_login_does_not_save_unauthenticated_session(tmp_path, monkeypatch):
    auth_dir = tmp_path / ".psn-receipts"
    auth_file = auth_dir / "auth.json"
    page = FakePage()
    context = FakeContext(page)
    browser = FakeBrowser(context)
    saved = []

    monkeypatch.setattr(auth, "AUTH_DIR", auth_dir)
    monkeypatch.setattr(auth, "AUTH_FILE", auth_file)
    monkeypatch.setattr(auth, "sync_playwright", lambda: FakePlaywrightRunner(browser))
    monkeypatch.setattr(auth, "_launch_browser", lambda p: (browser, "Chromium"))
    monkeypatch.setattr(builtins, "input", lambda _: "")
    monkeypatch.setattr(auth.cfg, "get_locale", lambda: cfg.DEFAULT_LOCALE)
    monkeypatch.setattr(auth.cfg, "save", lambda data: saved.append(data))
    monkeypatch.setattr(
        auth,
        "_fetch_transaction_history_page",
        lambda page_arg, end_date: (_ for _ in ()).throw(
            PSNReceiptsError("PlayStation Store rejected the saved session (HTTP 401 Unauthorized).")
        ),
    )

    with pytest.raises(PSNReceiptsError, match="session was not saved"):
        auth.login()

    assert context.storage_state_calls == []
    assert saved == []
    assert browser.closed is True


def test_auth_existing_session_reports_shared_default_locale(tmp_path, monkeypatch, capsys):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{}")

    monkeypatch.setattr(auth, "AUTH_FILE", auth_file)
    monkeypatch.setattr(auth.cfg, "get_locale", lambda: cfg.DEFAULT_LOCALE)

    auth.login()

    output = capsys.readouterr().out
    assert f"Current locale: {cfg.DEFAULT_LOCALE}" in output


def test_config_and_parse_share_default_locale(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"

    monkeypatch.setattr(cfg, "CONFIG_FILE", config_file)

    assert cfg.get_locale() == cfg.DEFAULT_LOCALE
    assert parse._chihiro_url("UP0001-CUSA00001_00-GAME") == (
        "https://store.playstation.com/store/api/chihiro/00_09_000/container/US/en/999/"
        "UP0001-CUSA00001_00-GAME"
    )
