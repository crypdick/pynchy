from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from pynchy.plugins.integrations.google_setup import (
    _handler as google_handler,  # allow: private-test-imports -- _run_interactive_setup only exposes browser-side effects; handle_setup_google does not surface the internal novnc_url path we need to pin here.
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


async def _raise_login_failed(_page) -> None:
    raise RuntimeError("login failed")
