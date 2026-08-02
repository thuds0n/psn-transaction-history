import builtins
import inspect
import json
import stat
from pathlib import Path

import pytest
import requests
from playwright.sync_api import Error as PlaywrightError
from typer.testing import CliRunner

from psn_transactions import auth, config as cfg, fetch, parse, storage
from psn_transactions.cli import app
from psn_transactions.errors import PSNTransactionsError


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
        self.evaluate_calls = []

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
        self.evaluate_calls.append((script, payload))
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


class FakeHTTPResponse:
    def __init__(self, payload=None, status=200, reason="OK", text=None):
        self.payload = payload
        self.status_code = status
        self.reason = reason
        self.ok = status < 400
        self.text = json.dumps(payload) if text is None else text

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeHTTPSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.cookies = requests.cookies.RequestsCookieJar()
        self.get_calls = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.closed = True
        return False

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


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

    result = fetch.fetch_all(output_path=str(output_path), transport="browser")

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

    result = fetch.fetch_all(output_path=str(output_path), transport="browser")

    assert result == []
    assert json.loads(output_path.read_text()) == []


def test_http_fetch_uses_saved_cookies_without_starting_playwright(
    tmp_path, monkeypatch
):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "isSignedIn",
                        "value": "true",
                        "domain": ".playstation.com",
                        "path": "/",
                        "secure": True,
                        "expires": -1,
                    }
                ]
            }
        )
    )
    output_path = tmp_path / "http.json"
    session = FakeHTTPSession([FakeHTTPResponse(success_result([])["body"])])

    monkeypatch.setattr(fetch, "AUTH_FILE", auth_file)
    monkeypatch.setattr(fetch.requests, "Session", lambda: session)
    monkeypatch.setattr(
        fetch,
        "sync_playwright",
        lambda: pytest.fail("HTTP transport should not start Playwright"),
    )

    result = fetch.fetch_all(output_path=str(output_path), transport="http")

    assert result == []
    assert json.loads(output_path.read_text()) == []
    assert session.cookies.get("isSignedIn", domain=".playstation.com") == "true"
    assert session.closed is True


def test_http_fetch_rejects_malformed_saved_cookie_state(tmp_path, monkeypatch):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({"cookies": "not-a-list"}))
    output_path = tmp_path / "http.json"

    monkeypatch.setattr(fetch, "AUTH_FILE", auth_file)

    with pytest.raises(PSNTransactionsError, match="unexpected cookie format"):
        fetch.fetch_all(output_path=str(output_path), transport="http")

    assert not output_path.exists()


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

    with pytest.raises(PSNTransactionsError, match="GraphQL errors"):
        fetch.fetch_all(output_path=str(output_path), transport="browser")

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

    with pytest.raises(PSNTransactionsError, match="UNAUTHENTICATED"):
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

    with pytest.raises(PSNTransactionsError, match="unexpected response shape"):
        fetch._fetch_transaction_history_page(page, "2025-01-01T00:00:00.000Z")


def test_fetch_helper_raises_on_page_evaluate_failure():
    page = FakePage([PlaywrightError("page crashed")])

    with pytest.raises(PSNTransactionsError, match="page crashed"):
        fetch._fetch_transaction_history_page(page, "2025-01-01T00:00:00.000Z")


def test_fetch_helper_passes_date_range_to_browser():
    page = FakePage([success_result([])])

    fetch._fetch_transaction_history_page(
        page,
        "2025-12-31T23:59:59.999Z",
        "2025-01-01T00:00:00.000Z",
    )

    assert page.evaluate_calls == [
        (
            fetch._JS_FETCH,
            {
                "startDate": "2025-01-01T00:00:00.000Z",
                "endDate": "2025-12-31T23:59:59.999Z",
                "url": fetch.GRAPHQL_URL,
                "hash": fetch.GRAPHQL_HASH,
                "headers": fetch.GRAPHQL_HEADERS,
            },
        )
    ]


def test_http_fetch_passes_equivalent_graphql_request():
    session = FakeHTTPSession([FakeHTTPResponse(success_result([])["body"])])

    result = fetch._fetch_transaction_history_page_http(
        session,
        "2025-12-31T23:59:59.999Z",
        "2025-01-01T00:00:00.000Z",
    )

    assert result == []
    assert len(session.get_calls) == 1
    url, request = session.get_calls[0]
    assert url == fetch.GRAPHQL_URL
    assert request["headers"] == fetch.GRAPHQL_HEADERS
    assert request["timeout"] == fetch.HTTP_TIMEOUT_SECONDS
    assert json.loads(request["params"]["variables"]) == {
        "startDate": "2025-01-01T00:00:00.000Z",
        "endDate": "2025-12-31T23:59:59.999Z",
        "limit": 100,
    }
    assert json.loads(request["params"]["extensions"]) == {
        "persistedQuery": {"version": 1, "sha256Hash": fetch.GRAPHQL_HASH}
    }


def test_http_fetch_converts_request_failure():
    session = FakeHTTPSession([requests.ConnectionError("connection reset")])

    with pytest.raises(PSNTransactionsError, match="connection reset"):
        fetch._fetch_transaction_history_page_http(
            session,
            "2025-12-31T23:59:59.999Z",
        )


def test_http_fetch_rejects_non_json_response():
    session = FakeHTTPSession(
        [FakeHTTPResponse(ValueError("not JSON"), text="access denied")]
    )

    with pytest.raises(PSNTransactionsError, match="non-JSON response"):
        fetch._fetch_transaction_history_page_http(
            session,
            "2025-12-31T23:59:59.999Z",
        )


def test_http_fetch_recommends_browser_fallback_when_rejected():
    session = FakeHTTPSession(
        [FakeHTTPResponse({"error": "forbidden"}, status=403, reason="Forbidden")]
    )

    with pytest.raises(
        PSNTransactionsError,
        match=r"fetch --transport browser",
    ):
        fetch._fetch_transaction_history_page_http(
            session,
            "2025-12-31T23:59:59.999Z",
        )


def test_fetch_defaults_to_http_transport():
    assert inspect.signature(fetch.fetch_all).parameters["transport"].default == "http"


def test_auth_login_saves_state_only_after_validation(tmp_path, monkeypatch):
    auth_dir = tmp_path / ".psn-transactions"
    auth_file = auth_dir / "auth.json"
    page = FakePage()
    context = FakeContext(page)
    context.cookies_result = [
        {"name": "npsso", "value": "authenticated"},
        {"name": "isSignedIn", "value": "true"},
    ]
    browser = FakeBrowser(context)
    saved = []
    validated = []

    monkeypatch.setattr(auth, "AUTH_DIR", auth_dir)
    monkeypatch.setattr(auth, "AUTH_FILE", auth_file)
    monkeypatch.setattr(auth, "sync_playwright", lambda: FakePlaywrightRunner(browser))
    monkeypatch.setattr(auth, "_launch_browser", lambda p: (browser, "Chromium"))
    monkeypatch.setattr(
        builtins,
        "input",
        lambda _: pytest.fail("automatic login should not prompt for ENTER"),
    )
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
    auth_dir = tmp_path / ".psn-transactions"
    auth_file = auth_dir / "auth.json"
    page = FakePage()
    context = FakeContext(page)
    context.cookies_result = [
        {"name": "npsso", "value": "authenticated"},
        {"name": "isSignedIn", "value": "true"},
    ]
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
            PSNTransactionsError("Sony rejected the browser session (HTTP 401).")
        ),
    )

    with pytest.raises(PSNTransactionsError, match="session was not saved"):
        auth.login()

    assert context.storage_state_calls == []
    assert saved == []
    assert browser.closed is True


def test_auth_debug_output_never_discloses_cookie_values(
    tmp_path, monkeypatch, capsys
):
    auth_dir = tmp_path / ".psn-transactions"
    auth_file = auth_dir / "auth.json"
    page = FakePage()
    context = FakeContext(page)
    context.cookies_result = [
        {"name": "npsso", "value": "super-secret-npsso-value"},
        {"name": "JSESSIONID", "value": "another-secret-value"},
        {"name": "isSignedIn", "value": "true"},
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

    with pytest.raises(PSNTransactionsError, match=message):
        auth._validate_authenticated_session(page)


def test_auth_validation_converts_navigation_failure():
    page = FakePage(goto_results=[PlaywrightError("page was closed")])

    with pytest.raises(PSNTransactionsError, match="session-validation endpoint"):
        auth._validate_authenticated_session(page)


def test_automatic_sign_in_waits_for_authenticated_cookie(monkeypatch):
    class SequencedContext:
        def __init__(self):
            self.results = [
                [{"name": "npsso", "value": "authenticated"}],
                [
                    {"name": "npsso", "value": "authenticated"},
                    {"name": "isSignedIn", "value": "true"},
                ],
            ]
            self.calls = 0

        def cookies(self):
            self.calls += 1
            return self.results.pop(0)

    context = SequencedContext()
    monkeypatch.setattr(auth.time, "monotonic", lambda: 0)
    monkeypatch.setattr(auth.time, "sleep", lambda _: None)

    auth._wait_for_sign_in(context, timeout=10, poll_interval=0)

    assert context.calls == 2


def test_automatic_sign_in_ignores_empty_session_cookie(monkeypatch):
    context = FakeContext(FakePage())
    context.cookies_result = [
        {"name": "npsso", "value": ""},
        {"name": "isSignedIn", "value": "true"},
    ]
    clock = iter([0, 2])
    monkeypatch.setattr(auth.time, "monotonic", lambda: next(clock))

    with pytest.raises(PSNTransactionsError, match="not detected within 1 seconds"):
        auth._wait_for_sign_in(context, timeout=1, poll_interval=0)


def test_automatic_sign_in_reports_closed_browser():
    class ClosedContext:
        def cookies(self):
            raise PlaywrightError("Target page, context or browser has been closed")

    with pytest.raises(PSNTransactionsError, match="Keep the browser window open"):
        auth._wait_for_sign_in(ClosedContext())


def test_manual_confirmation_uses_enter_instead_of_cookie_wait(tmp_path, monkeypatch):
    auth_dir = tmp_path / ".psn-transactions"
    auth_file = auth_dir / "auth.json"
    page = FakePage()
    context = FakeContext(page)
    browser = FakeBrowser(context)
    prompts = []

    monkeypatch.setattr(auth, "AUTH_DIR", auth_dir)
    monkeypatch.setattr(auth, "AUTH_FILE", auth_file)
    monkeypatch.setattr(auth, "sync_playwright", lambda: FakePlaywrightRunner(browser))
    monkeypatch.setattr(auth, "_launch_browser", lambda p: (browser, "Chromium"))
    monkeypatch.setattr(auth, "_validate_authenticated_session", lambda page_arg: None)
    monkeypatch.setattr(
        auth,
        "_wait_for_sign_in",
        lambda context_arg: pytest.fail("manual mode should not poll cookies"),
    )
    monkeypatch.setattr(builtins, "input", lambda prompt: prompts.append(prompt) or "")
    monkeypatch.setattr(auth.cfg, "get_locale", lambda: cfg.DEFAULT_LOCALE)
    monkeypatch.setattr(auth.cfg, "save", lambda data: None)

    auth.login(manual_confirmation=True)

    assert prompts == ["Press ENTER once you are signed in... "]
    assert auth_file.exists()


def test_login_converts_browser_launch_failure():
    class FailingChromium:
        def launch(self, **kwargs):
            raise PlaywrightError("browser executable is missing")

    class FailingPlaywright:
        chromium = FailingChromium()

    with pytest.raises(PSNTransactionsError, match="Could not launch Chrome, Edge") as exc_info:
        auth._launch_browser(FailingPlaywright())

    assert "python3 -m playwright install chromium" in str(exc_info.value)


def test_fetch_browser_prefers_system_chrome():
    expected_browser = object()

    class RecordingChromium:
        def __init__(self):
            self.calls = []

        def launch(self, **kwargs):
            self.calls.append(kwargs)
            return expected_browser

    class RecordingPlaywright:
        chromium = RecordingChromium()

    playwright = RecordingPlaywright()

    assert fetch._launch_fetch_browser(playwright) is expected_browser
    assert playwright.chromium.calls == [{"channel": "chrome", "headless": True}]


def test_fetch_browser_uses_bundled_fallback_after_system_channels_fail():
    expected_browser = object()

    class FallbackChromium:
        def __init__(self):
            self.calls = []

        def launch(self, **kwargs):
            self.calls.append(kwargs)
            if "channel" in kwargs:
                raise PlaywrightError("channel unavailable")
            return expected_browser

    class FallbackPlaywright:
        chromium = FallbackChromium()

    playwright = FallbackPlaywright()

    assert fetch._launch_fetch_browser(playwright) is expected_browser
    assert playwright.chromium.calls == [
        {"channel": "chrome", "headless": True},
        {"channel": "msedge", "headless": True},
        {"headless": True},
    ]


def test_login_converts_storage_state_save_failure(tmp_path, monkeypatch):
    auth_dir = tmp_path / ".psn-transactions"
    auth_file = auth_dir / "auth.json"
    page = FakePage()
    context = FakeContext(page)
    context.cookies_result = [
        {"name": "npsso", "value": "authenticated"},
        {"name": "isSignedIn", "value": "true"},
    ]
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

    with pytest.raises(PSNTransactionsError, match="Could not save the browser session"):
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

    with pytest.raises(PSNTransactionsError, match="Could not load the saved browser session"):
        fetch.fetch_all(
            output_path=str(tmp_path / "output.json"), transport="browser"
        )

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

    with pytest.raises(PSNTransactionsError, match="Could not navigate to PlayStation Store"):
        fetch.fetch_all(output_path=str(output_path), transport="browser")

    assert browser.closed is True
    assert not output_path.exists()


def test_fetch_restricts_existing_auth_file_permissions(tmp_path, monkeypatch):
    auth_directory = tmp_path / ".psn-transactions"
    auth_directory.mkdir(mode=0o755)
    auth_file = auth_directory / "auth.json"
    auth_file.write_text("{}")
    auth_file.chmod(0o644)
    output_path = tmp_path / "empty.json"
    browser = FakeBrowser(FakeContext(FakePage([success_result([])])))

    monkeypatch.setattr(fetch, "AUTH_FILE", auth_file)
    monkeypatch.setattr(fetch, "sync_playwright", lambda: FakePlaywrightRunner(browser))
    monkeypatch.setattr(fetch.cfg, "get_locale", lambda: cfg.DEFAULT_LOCALE)

    fetch.fetch_all(output_path=str(output_path), transport="browser")

    assert stat.S_IMODE(auth_directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(auth_file.stat().st_mode) == 0o600


def test_auth_security_rejects_symlinked_session_file(tmp_path):
    real_auth_file = tmp_path / "real-auth.json"
    real_auth_file.write_text("{}")
    linked_auth_file = tmp_path / "auth.json"
    linked_auth_file.symlink_to(real_auth_file)

    with pytest.raises(PSNTransactionsError, match="must be a regular file"):
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
    with pytest.raises(PSNTransactionsError, match=message):
        fetch._pagination_end_date(transaction, page_number=2)


def test_pagination_normalises_valid_timestamp_to_utc():
    assert fetch._pagination_end_date(
        {"id": "TX001", "date": "2025-01-15T10:00:00.000+10:00"},
        page_number=1,
    ) == "2025-01-14T23:59:59.999Z"


def test_date_range_converts_local_day_boundaries_to_utc_across_dst():
    assert fetch._resolve_date_range(
        "2025-01-01",
        "2025-08-02",
        "Australia/Sydney",
    ) == (
        "2024-12-31T13:00:00.000Z",
        "2025-08-02T13:59:59.999Z",
        "Australia/Sydney",
    )


def test_date_range_supports_explicit_utc_boundaries():
    assert fetch._resolve_date_range("2025-01-01", "2025-12-31", "UTC") == (
        "2025-01-01T00:00:00.000Z",
        "2025-12-31T23:59:59.999Z",
        "UTC",
    )


def test_date_range_keeps_defaults_for_omitted_bounds(monkeypatch):
    monkeypatch.setattr(
        fetch,
        "_current_end_date",
        lambda: "2026-08-02T10:30:00.000Z",
    )

    assert fetch._resolve_date_range(None, "2025-12-31", "UTC") == (
        fetch.DEFAULT_START_DATE,
        "2025-12-31T23:59:59.999Z",
        "UTC",
    )
    assert fetch._resolve_date_range("2025-01-01", None, "UTC") == (
        "2025-01-01T00:00:00.000Z",
        "2026-08-02T10:30:00.000Z",
        "UTC",
    )


def test_date_range_detects_local_timezone(monkeypatch):
    monkeypatch.setattr(fetch, "get_localzone_name", lambda: "Europe/London")

    assert fetch._resolve_date_range("2025-07-01", "2025-07-01") == (
        "2025-06-30T23:00:00.000Z",
        "2025-07-01T22:59:59.999Z",
        "Europe/London",
    )


def test_full_history_does_not_require_timezone_detection(monkeypatch):
    monkeypatch.setattr(
        fetch,
        "get_localzone_name",
        lambda: pytest.fail("timezone detection should not run"),
    )
    monkeypatch.setattr(
        fetch,
        "_current_end_date",
        lambda: "2026-08-02T10:30:00.000Z",
    )

    assert fetch._resolve_date_range(None, None) == (
        fetch.DEFAULT_START_DATE,
        "2026-08-02T10:30:00.000Z",
        "UTC",
    )


def test_date_range_rejects_unknown_timezone():
    with pytest.raises(PSNTransactionsError, match="Unknown timezone 'Mars/Olympus'"):
        fetch._resolve_date_range("2025-01-01", None, "Mars/Olympus")


@pytest.mark.parametrize(
    ("start_date", "end_date", "message"),
    [
        ("01-01-2025", None, "Invalid --start date"),
        (None, "2025-02-29", "Invalid --end date"),
        ("2025-1-01", None, "Invalid --start date"),
        ("", None, "Invalid --start date"),
        ("2025-12-31", "2025-01-01", "--start 2025-12-31 is after --end 2025-01-01"),
    ],
)
def test_date_range_rejects_invalid_values(start_date, end_date, message):
    with pytest.raises(PSNTransactionsError, match=message):
        fetch._resolve_date_range(start_date, end_date)


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

    with pytest.raises(PSNTransactionsError, match="transaction 'TX001' has malformed date"):
        fetch.fetch_all(output_path=str(output_path), transport="browser")

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

    with pytest.raises(PSNTransactionsError, match="Pagination did not advance"):
        fetch.fetch_all(output_path=str(output_path), transport="browser")

    assert browser.closed is True
    assert not output_path.exists()


def test_fetch_stops_at_requested_start_date(tmp_path, monkeypatch):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{}")
    output_path = tmp_path / "output.json"
    transactions = [
        {"id": "TX001", "date": "2025-01-01T00:00:00.000Z"},
    ]
    page = FakePage([success_result(transactions)])
    browser = FakeBrowser(FakeContext(page))

    monkeypatch.setattr(fetch, "AUTH_FILE", auth_file)
    monkeypatch.setattr(fetch, "sync_playwright", lambda: FakePlaywrightRunner(browser))
    monkeypatch.setattr(fetch.time, "sleep", lambda _: None)
    monkeypatch.setattr(fetch.cfg, "get_locale", lambda: cfg.DEFAULT_LOCALE)

    result = fetch.fetch_all(
        output_path=str(output_path),
        start_date="2025-01-01",
        end_date="2025-12-31",
        timezone_name="UTC",
        transport="browser",
    )

    assert result == transactions
    assert len(page.evaluate_calls) == 1
    assert page.evaluate_calls[0][1]["startDate"] == "2025-01-01T00:00:00.000Z"
    assert page.evaluate_calls[0][1]["endDate"] == "2025-12-31T23:59:59.999Z"
    assert json.loads(output_path.read_text()) == transactions


def test_atomic_output_failure_preserves_existing_export(tmp_path, monkeypatch):
    output_path = tmp_path / "transactions.json"
    output_path.write_text("existing export")

    def fail_replace(source, destination):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(storage.os, "replace", fail_replace)

    with pytest.raises(PSNTransactionsError, match="Could not save transaction JSON"):
        storage.atomic_write_json(output_path, [{"id": "TX001"}])

    assert output_path.read_text() == "existing export"
    assert list(tmp_path.glob(".transactions.json.*.tmp")) == []


def test_fetch_cli_prints_expected_failure(monkeypatch):
    def fail_fetch(**kwargs):
        raise PSNTransactionsError("Could not load the saved browser session.")

    monkeypatch.setattr(fetch, "fetch_all", fail_fetch)

    result = CliRunner().invoke(app, ["fetch"])

    assert result.exit_code == 1
    assert "Could not load the saved browser session." in result.output


def test_fetch_cli_forwards_date_range(monkeypatch):
    calls = []

    def record_fetch(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(fetch, "fetch_all", record_fetch)

    result = CliRunner().invoke(
        app,
        ["fetch", "--start", "2025-01-01", "--end", "2025-12-31"],
    )

    assert result.exit_code == 0
    assert calls == [
        {
            "output_path": "psn_transactions.json",
            "limit": None,
            "start_date": "2025-01-01",
            "end_date": "2025-12-31",
            "timezone_name": None,
            "transport": "http",
        }
    ]


def test_fetch_cli_forwards_timezone_override(monkeypatch):
    calls = []

    def record_fetch(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(fetch, "fetch_all", record_fetch)

    result = CliRunner().invoke(
        app,
        [
            "fetch",
            "--start",
            "2025-01-01",
            "--timezone",
            "Australia/Perth",
        ],
    )

    assert result.exit_code == 0
    assert calls[0]["timezone_name"] == "Australia/Perth"


def test_fetch_cli_forwards_http_transport(monkeypatch):
    calls = []

    def record_fetch(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(fetch, "fetch_all", record_fetch)

    result = CliRunner().invoke(app, ["fetch", "--transport", "http"])

    assert result.exit_code == 0
    assert calls[0]["transport"] == "http"


def test_fetch_cli_rejects_unknown_transport_before_opening_browser():
    result = CliRunner().invoke(app, ["fetch", "--transport", "ftp"])

    assert result.exit_code == 1
    assert "Unknown fetch transport 'ftp'" in result.output


def test_fetch_cli_reports_invalid_date_before_opening_browser():
    result = CliRunner().invoke(app, ["fetch", "--start", "not-a-date"])

    assert result.exit_code == 1
    assert "Invalid --start date 'not-a-date'; expected YYYY-MM-DD." in result.output


def test_login_cli_prints_expected_failure(monkeypatch):
    def fail_login(**kwargs):
        raise PSNTransactionsError("Could not launch Playwright Chromium.")

    monkeypatch.setattr(auth, "login", fail_login)

    result = CliRunner().invoke(app, ["login"])

    assert result.exit_code == 1
    assert "Could not launch Playwright Chromium." in result.output


def test_login_cli_forwards_manual_confirmation(monkeypatch):
    calls = []

    def record_login(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(auth, "login", record_login)

    result = CliRunner().invoke(app, ["login", "--manual-confirmation"])

    assert result.exit_code == 0
    assert calls == [
        {
            "force": False,
            "debug": False,
            "locale": None,
            "manual_confirmation": True,
        }
    ]


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

    with pytest.raises(PSNTransactionsError, match="Could not read configuration"):
        cfg.load()
