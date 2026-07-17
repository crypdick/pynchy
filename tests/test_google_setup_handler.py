from __future__ import annotations

import asyncio
import io
import sys
import threading
import types
import urllib.request
from http.server import HTTPServer
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from pynchy.plugins.integrations.google_setup import (
    GoogleSetupPlugin,
    run_oauth_flow,
)

CALLBACK_EVENT_SLEEP_MESSAGE = "OAuth flow should wait on the callback event, not sleep-poll"
LOGIN_FAILED_MESSAGE = "login failed"
UNSAFE_URLOPEN_MESSAGE = "urlopen should not be called for unsafe URLs"

if TYPE_CHECKING:
    from pathlib import Path


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
    credentials_file = tmp_path / "credentials.json"

    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._handler.has_display",
        lambda: False,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._handler.start_virtual_display",
        lambda: ([], "http://novnc.local/google"),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._handler.stop_procs",
        stop_procs,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._handler.profile_dir",
        lambda _name: tmp_path / "profile",
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._handler.chrome_path",
        lambda: "/usr/bin/google-chrome",
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._handler.keys_path",
        lambda _name: keys_file,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._handler.credentials_path",
        lambda _name: credentials_file,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._handler.compute_scopes_for_profile",
        lambda _name: ("scope-a scope-b", ["drive.googleapis.com"]),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._handler.wait_for_login",
        _raise_login_failed,
    )

    def fake_async_playwright() -> _FakePlaywright:
        return _FakePlaywright(context)

    async_api_module = types.ModuleType("playwright.async_api")
    async_api_module.async_playwright = fake_async_playwright
    playwright_module = types.ModuleType("playwright")
    playwright_module.async_api = async_api_module
    monkeypatch.setitem(sys.modules, "playwright", playwright_module)
    monkeypatch.setitem(sys.modules, "playwright.async_api", async_api_module)

    setup_google_drive = _google_setup_tool(monkeypatch, "drive")

    response = await setup_google_drive({})

    assert response == {
        "error": "login failed",
        "novnc_url": "http://novnc.local/google",
    }
    assert context.closed is False
    stop_procs.assert_called_once_with([])


@pytest.mark.asyncio
async def test_oauth_exchange_rejects_non_https_endpoint_before_opening(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    keys_file = tmp_path / "gcp-oauth.keys.json"
    keys_file.write_text(
        '{"installed":{"client_id":"123.apps.googleusercontent.com","client_secret":"secret"}}'
    )
    done = threading.Event()
    done.set()

    class FakeServer:
        def shutdown(self) -> None:
            return None

    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._oauth.start_callback_server",
        lambda: (done, ["auth-code"], FakeServer()),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._oauth.GOOGLE_OAUTH_ENDPOINT_URL",
        "file:///tmp/token",
    )
    monkeypatch.setattr(urllib.request, "urlopen", _fail_urlopen)

    with pytest.raises(RuntimeError, match="must use https"):
        await run_oauth_flow(_FakePage(), keys_file, "scope-a")


@pytest.mark.asyncio
async def test_oauth_callback_server_binds_to_localhost(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, int]] = []

    class FakeServer(HTTPServer):
        def __init__(self, server_address, handler) -> None:
            calls.append(server_address)
            self._handler = handler

        def serve_forever(self) -> None:
            request_handler = object.__new__(self._handler)
            request_handler.path = "/?code=auth-code"
            request_handler.wfile = io.BytesIO()
            request_handler.send_response = lambda _code: None
            request_handler.send_header = lambda _name, _value: None
            request_handler.end_headers = lambda: None
            self._handler.do_GET(request_handler)

        def shutdown(self) -> None:
            return None

    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._oauth.HTTPServer",
        FakeServer,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._oauth.exchange_code_for_tokens",
        lambda *_args: {"ok": True},
    )

    tokens = await run_oauth_flow(_FakePage(), _keys_file(tmp_path), "scope-a")

    assert calls == [("localhost", 8085)]
    assert tokens == {"ok": True}


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

    async def _fail_sleep(_delay: float) -> None:
        future = asyncio.get_running_loop().create_future()
        future.set_result(None)
        await future
        raise AssertionError(CALLBACK_EVENT_SLEEP_MESSAGE)

    async def fail_sleep(_delay: float) -> None:
        return await _fail_sleep(_delay)

    def set_callback() -> None:
        auth_codes.append("auth-code")
        done.set()

    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._oauth.start_callback_server",
        lambda: (done, auth_codes, FakeServer()),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._oauth.asyncio.sleep",
        fail_sleep,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._oauth.exchange_code_for_tokens",
        lambda *_args: {"ok": True},
    )

    loop = asyncio.get_running_loop()
    loop.call_soon(set_callback)

    tokens = await run_oauth_flow(_FakePage(), keys_file, "scope-a")

    assert tokens == {"ok": True}


@pytest.mark.action("integration.google.profile.setup")
@pytest.mark.asyncio
async def test_rest_token_refresh_rejects_non_https_endpoint_before_opening(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    keys_file = tmp_path / "gcp-oauth.keys.json"
    keys_file.write_text(
        '{"installed":{"client_id":"123.apps.googleusercontent.com","client_secret":"secret"}}'
    )
    credentials_file = tmp_path / "credentials.json"
    credentials_file.write_text('{"refresh_token":"refresh-token"}')

    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._rest_api.keys_path",
        lambda _profile: keys_file,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._rest_api.credentials_path",
        lambda _profile: credentials_file,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._rest_api.GOOGLE_OAUTH_ENDPOINT_URL",
        "file:///tmp/token",
    )
    monkeypatch.setattr(urllib.request, "urlopen", _fail_urlopen)

    async def fake_interactive_setup(*_args):
        await asyncio.sleep(0)
        return {"result": {"status": "interactive_setup_required"}}

    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._handler.keys_path",
        lambda _profile: keys_file,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._handler.credentials_path",
        lambda _profile: credentials_file,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._handler.compute_scopes_for_profile",
        lambda _profile: ("scope-a", ["drive.googleapis.com"]),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._handler._run_interactive_setup",
        fake_interactive_setup,
    )
    setup_google_profile = _google_setup_tool(monkeypatch, "profile")

    assert await setup_google_profile({}) == {"result": {"status": "interactive_setup_required"}}


async def _raise_login_failed(_page) -> None:
    await asyncio.sleep(0)
    raise RuntimeError(LOGIN_FAILED_MESSAGE)


def _google_setup_tool(monkeypatch: pytest.MonkeyPatch, profile: str):
    class FakeSettings:
        chrome_profiles = [profile]

    def fake_get_settings() -> FakeSettings:
        return FakeSettings()

    monkeypatch.setattr("pynchy.config.get_settings", fake_get_settings)
    tools = GoogleSetupPlugin().pynchy_service_handler()["tools"]
    return tools[f"setup_google_{profile}"]


def _keys_file(tmp_path: Path) -> Path:
    keys_file = tmp_path / "gcp-oauth.keys.json"
    keys_file.write_text(
        '{"installed":{"client_id":"123.apps.googleusercontent.com","client_secret":"secret"}}'
    )
    return keys_file


def _fail_urlopen(*_args, **_kwargs):
    raise AssertionError(UNSAFE_URLOPEN_MESSAGE)
