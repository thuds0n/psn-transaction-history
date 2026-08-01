import builtins
import json
import stat
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError
from typer.testing import CliRunner

from psn_receipts import auth, config as cfg, fetch, parse, storage
from psn_receipts.cli import app
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


class FakeResponse:
    def __init__(self, status=200):
        self.status = status


class FakePage:
    def __init__(self, evaluate_results=None, goto_results=None, body=""):
        self.evaluate_results = list(evaluate_results or [])
        self.goto_results = list(goto_results or [])
        self.body = body
        self.goto_calls = []

    def goto(self, url):
        self.goto_calls.append(url)
        result = self.goto_results.pop(0) if self.goto_results else FakeResponse()
        if isinstance(result, Exception):
            raise result
        return result

    def text_content(self, selector):
        assert selector == "body"
        return self.body

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
    page = FakePage([PlaywrightError("page crashed")])

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
    def fake_validate(page_arg):
        validated.append(page_arg)

    monkeypatch.setattr(auth, "_validate_authenticated_session", fake_validate)

    auth.login()

    assert validated == [page]
    assert len(context.storage_state_calls) == 1
    assert context.storage_state_calls[0] != str(auth_file)
    assert Path(context.storage_state_calls[0]).parent == auth_dir
    assert saved == [{"locale": cfg.DEFAULT_LOCALE}]
    assert stat.S_IMODE(auth_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(auth_file.stat().st_mode) == 0o600
    assert page.goto_calls == [cfg.store_url(cfg.DEFAULT_LOCALE)]
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
        "_validate_authenticated_session",
        lambda page_arg: (_ for _ in ()).throw(
            PSNReceiptsError("Sony rejected the browser session (HTTP 401).")
        ),
    )

    with pytest.raises(PSNReceiptsError, match="session was not saved"):
        auth.login()

    assert context.storage_state_calls == []
    assert saved == []
    assert browser.closed is True


def test_auth_debug_output_never_discloses_cookie_values(
    tmp_path, monkeypatch, capsys
):
    auth_dir = tmp_path / ".psn-receipts"
    auth_file = auth_dir / "auth.json"
    page = FakePage()
    context = FakeContext(page)
    context.cookies_result = [
        {"name": "npsso", "value": "super-secret-npsso-value"},
        {"name": "JSESSIONID", "value": "another-secret-value"},
    ]
    browser = FakeBrowser(context)

    monkeypatch.setattr(auth, "AUTH_DIR", auth_dir)
    monkeypatch.setattr(auth, "AUTH_FILE", auth_file)
    monkeypatch.setattr(auth, "sync_playwright", lambda: FakePlaywrightRunner(browser))
    monkeypatch.setattr(auth, "_launch_browser", lambda p: (browser, "Chromium"))
    monkeypatch.setattr(auth, "_validate_authenticated_session", lambda page_arg: None)
    monkeypatch.setattr(builtins, "input", lambda _: "")
    monkeypatch.setattr(auth.cfg, "get_locale", lambda: cfg.DEFAULT_LOCALE)
    monkeypatch.setattr(auth.cfg, "save", lambda data: None)

    auth.login(debug=True)

    output = capsys.readouterr().out
    assert "npsso: present" in output
    assert "JSESSIONID: present" in output
    assert "values redacted" in output
    assert "super-secret-npsso-value" not in output
    assert "another-secret-value" not in output


def test_auth_existing_session_reports_shared_default_locale(tmp_path, monkeypatch, capsys):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{}")

    monkeypatch.setattr(auth, "AUTH_FILE", auth_file)
    monkeypatch.setattr(auth.cfg, "get_locale", lambda: cfg.DEFAULT_LOCALE)

    auth.login()

    output = capsys.readouterr().out
    assert f"Current locale: {cfg.DEFAULT_LOCALE}" in output
    assert stat.S_IMODE(auth_file.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(auth_file.stat().st_mode) == 0o600


def test_auth_validation_uses_sony_identity_endpoint_not_transactions():
    page = FakePage(
        goto_results=[FakeResponse(200)],
        body=json.dumps({"npsso": "authenticated-session-token"}),
    )

    auth._validate_authenticated_session(page)

    assert page.goto_calls == [auth.AUTH_VALIDATION_URL]
    assert page.evaluate_results == []


@pytest.mark.parametrize(
    ("status", "body", "message"),
    [
        (401, "", "rejected the browser session"),
        (200, json.dumps({"error": "not signed in"}), "did not confirm"),
        (200, "not-json", "unexpected response"),
    ],
)
def test_auth_validation_rejects_unconfirmed_sessions(status, body, message):
    page = FakePage(goto_results=[FakeResponse(status)], body=body)

    with pytest.raises(PSNReceiptsError, match=message):
        auth._validate_authenticated_session(page)


def test_auth_validation_converts_navigation_failure():
    page = FakePage(goto_results=[PlaywrightError("page was closed")])

    with pytest.raises(PSNReceiptsError, match="session-validation endpoint"):
        auth._validate_authenticated_session(page)


def test_login_converts_browser_launch_failure():
    class FailingChromium:
        def launch(self, **kwargs):
            raise PlaywrightError("browser executable is missing")

    class FailingPlaywright:
        chromium = FailingChromium()

    with pytest.raises(PSNReceiptsError, match="Could not launch Chrome, Edge") as exc_info:
        auth._launch_browser(FailingPlaywright())

    assert "python3 -m playwright install chromium" in str(exc_info.value)


def test_login_converts_storage_state_save_failure(tmp_path, monkeypatch):
    auth_dir = tmp_path / ".psn-receipts"
    auth_file = auth_dir / "auth.json"
    page = FakePage()
    context = FakeContext(page)
    browser = FakeBrowser(context)
    saved = []

    def fail_storage_state(path):
        raise PlaywrightError("permission denied")

    context.storage_state = fail_storage_state
    monkeypatch.setattr(auth, "AUTH_DIR", auth_dir)
    monkeypatch.setattr(auth, "AUTH_FILE", auth_file)
    monkeypatch.setattr(auth, "sync_playwright", lambda: FakePlaywrightRunner(browser))
    monkeypatch.setattr(auth, "_launch_browser", lambda p: (browser, "Chromium"))
    monkeypatch.setattr(auth, "_validate_authenticated_session", lambda page_arg: None)
    monkeypatch.setattr(builtins, "input", lambda _: "")
    monkeypatch.setattr(auth.cfg, "get_locale", lambda: cfg.DEFAULT_LOCALE)
    monkeypatch.setattr(auth.cfg, "save", lambda data: saved.append(data))

    with pytest.raises(PSNReceiptsError, match="Could not save the browser session"):
        auth.login()

    assert saved == []
    assert browser.closed is True


def test_fetch_all_converts_saved_state_failure(tmp_path, monkeypatch):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("not valid storage state")
    browser = FakeBrowser(FakeContext(FakePage()))

    def fail_new_context(**kwargs):
        raise PlaywrightError("storage state is malformed")

    browser.new_context = fail_new_context
    monkeypatch.setattr(fetch, "AUTH_FILE", auth_file)
    monkeypatch.setattr(fetch, "sync_playwright", lambda: FakePlaywrightRunner(browser))

    with pytest.raises(PSNReceiptsError, match="Could not load the saved browser session"):
        fetch.fetch_all(output_path=str(tmp_path / "output.json"))

    assert browser.closed is True


def test_fetch_all_converts_store_navigation_failure(tmp_path, monkeypatch):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{}")
    output_path = tmp_path / "output.json"
    page = FakePage(goto_results=[PlaywrightError("net::ERR_NAME_NOT_RESOLVED")])
    browser = FakeBrowser(FakeContext(page))

    monkeypatch.setattr(fetch, "AUTH_FILE", auth_file)
    monkeypatch.setattr(fetch, "sync_playwright", lambda: FakePlaywrightRunner(browser))
    monkeypatch.setattr(fetch.cfg, "get_locale", lambda: cfg.DEFAULT_LOCALE)

    with pytest.raises(PSNReceiptsError, match="Could not navigate to PlayStation Store"):
        fetch.fetch_all(output_path=str(output_path))

    assert browser.closed is True
    assert not output_path.exists()


def test_fetch_restricts_legacy_auth_file_permissions(tmp_path, monkeypatch):
    auth_directory = tmp_path / ".psn-receipts"
    auth_directory.mkdir(mode=0o755)
    auth_file = auth_directory / "auth.json"
    auth_file.write_text("{}")
    auth_file.chmod(0o644)
    output_path = tmp_path / "empty.json"
    browser = FakeBrowser(FakeContext(FakePage([success_result([])])))

    monkeypatch.setattr(fetch, "AUTH_FILE", auth_file)
    monkeypatch.setattr(fetch, "sync_playwright", lambda: FakePlaywrightRunner(browser))
    monkeypatch.setattr(fetch.cfg, "get_locale", lambda: cfg.DEFAULT_LOCALE)

    fetch.fetch_all(output_path=str(output_path))

    assert stat.S_IMODE(auth_directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(auth_file.stat().st_mode) == 0o600


def test_auth_security_rejects_symlinked_session_file(tmp_path):
    real_auth_file = tmp_path / "real-auth.json"
    real_auth_file.write_text("{}")
    linked_auth_file = tmp_path / "auth.json"
    linked_auth_file.symlink_to(real_auth_file)

    with pytest.raises(PSNReceiptsError, match="must be a regular file"):
        storage.secure_auth_file(linked_auth_file)


@pytest.mark.parametrize(
    ("transaction", "message"),
    [
        ({"id": "TX001"}, "no valid `date` string"),
        ({"id": "TX001", "date": "yesterday"}, "malformed date"),
        ({"id": "TX001", "date": "2025-01-15T10:00:00"}, "malformed date"),
        ("not-an-object", "not an object"),
    ],
)
def test_pagination_rejects_malformed_transaction_dates(transaction, message):
    with pytest.raises(PSNReceiptsError, match=message):
        fetch._pagination_end_date(transaction, page_number=2)


def test_pagination_normalises_valid_timestamp_to_utc():
    assert fetch._pagination_end_date(
        {"id": "TX001", "date": "2025-01-15T10:00:00.000+10:00"},
        page_number=1,
    ) == "2025-01-14T23:59:59.999Z"


def test_fetch_all_does_not_write_output_for_malformed_pagination_date(
    tmp_path, monkeypatch
):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{}")
    output_path = tmp_path / "output.json"
    page = FakePage([success_result([{"id": "TX001", "date": "invalid"}])])
    browser = FakeBrowser(FakeContext(page))

    monkeypatch.setattr(fetch, "AUTH_FILE", auth_file)
    monkeypatch.setattr(fetch, "sync_playwright", lambda: FakePlaywrightRunner(browser))
    monkeypatch.setattr(fetch.cfg, "get_locale", lambda: cfg.DEFAULT_LOCALE)

    with pytest.raises(PSNReceiptsError, match="transaction 'TX001' has malformed date"):
        fetch.fetch_all(output_path=str(output_path))

    assert browser.closed is True
    assert not output_path.exists()


def test_fetch_stops_when_pagination_boundary_repeats(tmp_path, monkeypatch):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{}")
    output_path = tmp_path / "output.json"
    repeated_page = [
        {"id": "TX001", "date": "2025-01-15T10:00:00.000Z"}
    ]
    page = FakePage(
        [success_result(repeated_page), success_result(repeated_page)]
    )
    browser = FakeBrowser(FakeContext(page))

    monkeypatch.setattr(fetch, "AUTH_FILE", auth_file)
    monkeypatch.setattr(fetch, "sync_playwright", lambda: FakePlaywrightRunner(browser))
    monkeypatch.setattr(fetch, "_current_end_date", lambda: "2025-01-15T10:00:01.000Z")
    monkeypatch.setattr(fetch.time, "sleep", lambda _: None)
    monkeypatch.setattr(fetch.cfg, "get_locale", lambda: cfg.DEFAULT_LOCALE)

    with pytest.raises(PSNReceiptsError, match="Pagination did not advance"):
        fetch.fetch_all(output_path=str(output_path))

    assert browser.closed is True
    assert not output_path.exists()


def test_atomic_output_failure_preserves_existing_export(tmp_path, monkeypatch):
    output_path = tmp_path / "transactions.json"
    output_path.write_text("existing export")

    def fail_replace(source, destination):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(storage.os, "replace", fail_replace)

    with pytest.raises(PSNReceiptsError, match="Could not save transaction JSON"):
        storage.atomic_write_json(output_path, [{"id": "TX001"}])

    assert output_path.read_text() == "existing export"
    assert list(tmp_path.glob(".transactions.json.*.tmp")) == []


def test_fetch_cli_prints_expected_failure(monkeypatch):
    def fail_fetch(**kwargs):
        raise PSNReceiptsError("Could not load the saved browser session.")

    monkeypatch.setattr(fetch, "fetch_all", fail_fetch)

    result = CliRunner().invoke(app, ["fetch"])

    assert result.exit_code == 1
    assert "Could not load the saved browser session." in result.output


def test_login_cli_prints_expected_failure(monkeypatch):
    def fail_login(**kwargs):
        raise PSNReceiptsError("Could not launch Playwright Chromium.")

    monkeypatch.setattr(auth, "login", fail_login)

    result = CliRunner().invoke(app, ["login"])

    assert result.exit_code == 1
    assert "Could not launch Playwright Chromium." in result.output


def test_config_and_parse_share_default_locale(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"

    monkeypatch.setattr(cfg, "CONFIG_FILE", config_file)

    assert cfg.get_locale() == cfg.DEFAULT_LOCALE
    assert parse._chihiro_url("UP0001-CUSA00001_00-GAME") == (
        "https://store.playstation.com/store/api/chihiro/00_09_000/container/US/en/999/"
        "UP0001-CUSA00001_00-GAME"
    )


def test_config_reports_malformed_json_as_user_facing_error(tmp_path, monkeypatch):
    config_file = tmp_path / "config.json"
    config_file.write_text("not-json")
    monkeypatch.setattr(cfg, "CONFIG_FILE", config_file)

    with pytest.raises(PSNReceiptsError, match="Could not read configuration"):
        cfg.load()
