from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from pynchy.plugins.integrations.google_setup import (
    _handler as google_handler,  # allow: private-test-imports - public API hides novnc_url.
)
from pynchy.plugins.integrations.google_setup import (
    _oauth as google_oauth,  # allow: private-test-imports - URL safety is module-local.
)
from pynchy.plugins.integrations.google_setup import (
    _rest_api as google_rest_api,  # allow: private-test-imports - URL safety is module-local.
)


class _FakePage:
    async def goto(self, _url: str, *, wait_until: str) -> None:
        assert wait_until == "domcontentloaded"

    async def wait_for_timeout(self, _timeout_ms: int) -> None:
        return None


class _FakeContext:
    def __init__(self) -> None:
        self.pages = [_FakePage()]
        self.closed = False
        self.navigation_timeout: int | None = None
        self.default_timeout: int | None = None

    def set_default_navigation_timeout(self, timeout: int) -> None:
        self.navigation_timeout = timeout

    def set_default_timeout(self, timeout: int) -> None:
        self.default_timeout = timeout

    async def new_page(self) -> _FakePage:
        page = _FakePage()
        self.pages.append(page)
        return page

    async def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self, context: _FakeContext) -> None:
        self._context = context

    async def launch_persistent_context(self, **_kwargs) -> _FakeContext:
        return self._context


class _FakePlaywright:
    def __init__(self, context: _FakeContext) -> None:
        self.chromium = _FakeChromium(context)

    async def __aenter__(self) -> _FakePlaywright:
        return self

    async def __aexit__(self, exc_type, exc, _tb) -> None:
        return None


@pytest.mark.asyncio
async def test_interactive_setup_error_returns_novnc_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    context = _FakeContext()
    stop_procs = Mock()
    keys_file = tmp_path / "gcp-oauth.keys.json"

    monkeypatch.setattr(google_handler, "has_display", lambda: False)
    monkeypatch.setattr(
        google_handler,
        "start_virtual_display",
        lambda: ([], "http://novnc.local/google"),
    )
    monkeypatch.setattr(google_handler, "stop_procs", stop_procs)
    monkeypatch.setattr(google_handler, "profile_dir", lambda _name: tmp_path / "profile")
    monkeypatch.setattr(google_handler, "chrome_path", lambda: "/usr/bin/google-chrome")
    monkeypatch.setattr(google_handler, "wait_for_login", _raise_login_failed)

    def fake_async_playwright() -> _FakePlaywright:
        return _FakePlaywright(context)

    monkeypatch.setitem(
        __import__("sys").modules,
        "playwright.async_api",
        type("_PlaywrightModule", (), {"async_playwright": fake_async_playwright}),
    )

    response = await google_handler._run_interactive_setup(
        "drive",
        keys_file,
        ["drive.googleapis.com"],
        "scope-a scope-b",
        {},
    )

    assert response == {
        "error": "login failed",
        "novnc_url": "http://novnc.local/google",
    }
    assert context.closed is False
    stop_procs.assert_called_once_with([])


def test_oauth_exchange_rejects_non_https_endpoint_before_opening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(google_oauth, "GOOGLE_OAUTH_ENDPOINT_URL", "file:///tmp/token")
    monkeypatch.setattr(google_oauth.urllib.request, "urlopen", _fail_urlopen)

    with pytest.raises(RuntimeError, match="must use https"):
        google_oauth.exchange_code_for_tokens("code", "client-id", "client-secret")


def test_oauth_callback_server_binds_to_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, int]] = []

    class FakeServer(google_oauth.HTTPServer):
        def __init__(self, server_address, _handler) -> None:
            calls.append(server_address)

        def serve_forever(self) -> None:
            return None

    class FakeThread:
        def __init__(self, *, target, daemon: bool) -> None:
            self._target = target
            self.daemon = daemon

        def start(self) -> None:
            self._target()

    monkeypatch.setattr(google_oauth, "HTTPServer", FakeServer)
    monkeypatch.setattr(google_oauth.threading, "Thread", FakeThread)

    google_oauth.start_callback_server()

    assert calls == [(google_oauth.OAUTH_CALLBACK_HOST, google_oauth.OAUTH_CALLBACK_PORT)]


@pytest.mark.asyncio
async def test_oauth_flow_waits_for_callback_thread_event_without_async_sleep(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    keys_file = tmp_path / "gcp-oauth.keys.json"
    keys_file.write_text(
        '{"installed":{"client_id":"123.apps.googleusercontent.com","client_secret":"secret"}}'
    )
    done = threading.Event()
    auth_codes: list[str] = []

    class FakeServer:
        def shutdown(self) -> None:
            return None

    async def fail_sleep(_delay: float) -> None:
        raise AssertionError("OAuth flow should wait on the callback event, not sleep-poll")

    def set_callback() -> None:
        auth_codes.append("auth-code")
        done.set()

    monkeypatch.setattr(
        google_oauth,
        "start_callback_server",
        lambda: (done, auth_codes, FakeServer()),
    )
    monkeypatch.setattr(google_oauth.asyncio, "sleep", fail_sleep)
    monkeypatch.setattr(google_oauth, "exchange_code_for_tokens", lambda *_args: {"ok": True})

    loop = asyncio.get_running_loop()
    loop.call_soon(set_callback)

    tokens = await google_oauth.run_oauth_flow(_FakePage(), keys_file, "scope-a")

    assert tokens == {"ok": True}


def test_rest_token_refresh_rejects_non_https_endpoint_before_opening(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    keys_file = tmp_path / "gcp-oauth.keys.json"
    keys_file.write_text(
        '{"installed":{"client_id":"123.apps.googleusercontent.com","client_secret":"secret"}}'
    )
    credentials_file = tmp_path / "credentials.json"
    credentials_file.write_text('{"refresh_token":"refresh-token"}')

    monkeypatch.setattr(google_rest_api, "keys_path", lambda _profile: keys_file)
    monkeypatch.setattr(google_rest_api, "credentials_path", lambda _profile: credentials_file)
    monkeypatch.setattr(google_rest_api, "GOOGLE_OAUTH_ENDPOINT_URL", "file:///tmp/token")
    monkeypatch.setattr(google_rest_api.urllib.request, "urlopen", _fail_urlopen)

    assert google_rest_api.refresh_access_token("profile") is None


async def _raise_login_failed(_page) -> None:
    raise RuntimeError("login failed")


def _fail_urlopen(*_args, **_kwargs):
    raise AssertionError("urlopen should not be called for unsafe URLs")
