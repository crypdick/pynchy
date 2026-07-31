"""Hermetic end-to-end coverage for Pynchy's mutating X service actions."""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest

from pynchy.plugins.integrations.x_integration import XIntegrationPlugin

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


class _FakeLocator:
    """Small Playwright-shaped fake that records the action's visible effects."""

    def __init__(self, selector: str, calls: list[tuple[str, str, str | None]]) -> None:
        self._selector = selector
        self._calls = calls

    @property
    def first(self) -> _FakeLocator:
        return self

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(selector, self._calls)

    def filter(self, **_kwargs: object) -> _FakeLocator:
        return self

    async def wait_for(self, **_kwargs: object) -> None:
        self._calls.append(("wait", self._selector, None))

    async def click(self) -> None:
        self._calls.append(("click", self._selector, None))

    async def fill(self, content: str) -> None:
        self._calls.append(("fill", self._selector, content))

    async def get_attribute(self, _name: str) -> None:
        return None

    async def is_visible(self) -> bool:
        return self._calls.pop(0)[2] == "visible"


class _FakePage:
    def __init__(
        self,
        *,
        visible: tuple[bool, ...] = (),
        navigation_error: str | None = None,
        selector_error: str | None = None,
    ) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self._visible = iter(visible)
        self._navigation_error = navigation_error
        self._selector_error = selector_error

    def locator(self, selector: str) -> _FakeLocator:
        locator = _FakeLocator(selector, self.calls)
        locator.is_visible = AsyncMock(side_effect=self._visible)  # type: ignore[method-assign]
        return locator

    def get_by_role(self, role: str) -> _FakeLocator:
        return _FakeLocator(role, self.calls)

    async def goto(self, url: str, **_kwargs: object) -> None:
        if self._navigation_error:
            raise RuntimeError(self._navigation_error)
        self.calls.append(("goto", url, None))

    async def wait_for_timeout(self, _milliseconds: int) -> None:
        return None

    async def wait_for_selector(self, _selector: str, **_kwargs: object) -> None:
        if self._selector_error:
            raise RuntimeError(self._selector_error)


def _handler(tool_name: str) -> Callable[[dict[str, Any]], Any]:
    action = XIntegrationPlugin().pynchy_service_handler().action_for(tool_name)
    assert action is not None
    return action.handler


def _action_handler(handler: Callable[[dict[str, Any]], Any]) -> Callable[[dict[str, Any]], Any]:
    """Unwrap service-tool and runtime type decorators to reach the action module."""
    current = handler
    while "with_browser" not in current.__globals__:
        current = current.__wrapped__
    return current


class _FakeBrowserContext:
    def __init__(self, page: _FakePage, closed: list[bool]) -> None:
        self.pages = [page]
        self._closed = closed

    async def close(self) -> None:
        self._closed.append(True)


class _FakeChromium:
    def __init__(self, context: _FakeBrowserContext, launches: list[dict[str, object]]) -> None:
        self._context = context
        self._launches = launches

    async def launch_persistent_context(self, **kwargs: object) -> _FakeBrowserContext:
        self._launches.append(kwargs)
        return self._context


class _FakePlaywright:
    def __init__(self, context: _FakeBrowserContext, launches: list[dict[str, object]]) -> None:
        self.chromium = _FakeChromium(context, launches)

    async def __aenter__(self) -> _FakePlaywright:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


def _install_playwright(
    monkeypatch: pytest.MonkeyPatch,
    page: _FakePage,
    profile_path: Path,
) -> tuple[list[bool], list[dict[str, object]]]:
    """Install only the optional Playwright surface reached by public X actions."""
    closed: list[bool] = []
    launches: list[dict[str, object]] = []
    context = _FakeBrowserContext(page, closed)
    playwright = _FakePlaywright(context, launches)
    async_api = type(sys)("playwright.async_api")
    async_api.async_playwright = lambda: playwright
    package = type(sys)("playwright")
    package.async_api = async_api
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.async_api", async_api)
    monkeypatch.setattr(
        "pynchy.plugins.integrations.x_integration._browser.ensure_xvfb", lambda: None
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.x_integration._browser.cleanup_lock_files", lambda _path: None
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.x_integration._browser.profile_dir",
        lambda _name: profile_path,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.x_integration._browser.chrome_path",
        lambda: "/usr/bin/google-chrome",
    )
    return closed, launches


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "data", "expected_message", "visibility"),
    [
        pytest.param(
            "x_post",
            {"content": "Pynchy coverage post"},
            "Tweet posted: Pynchy coverage post",
            (),
            marks=pytest.mark.action("social.x.post"),
        ),
        pytest.param(
            "x_like",
            {"tweet_url": "123"},
            "Like successful",
            (False, True),
            marks=pytest.mark.action("social.x.like"),
        ),
        pytest.param(
            "x_reply",
            {"tweet_url": "123", "content": "Pynchy coverage reply"},
            "Reply posted: Pynchy coverage reply",
            (),
            marks=pytest.mark.action("social.x.reply"),
        ),
        pytest.param(
            "x_retweet",
            {"tweet_url": "123"},
            "Retweet successful",
            (False, True),
            marks=pytest.mark.action("social.x.repost"),
        ),
        pytest.param(
            "x_quote",
            {"tweet_url": "123", "comment": "Pynchy coverage quote"},
            "Quote tweet posted: Pynchy coverage quote",
            (),
            marks=pytest.mark.action("social.x.quote"),
        ),
    ],
)
async def test_x_mutations_drive_browser_and_report_verified_effects(
    tool_name: str,
    data: dict[str, str],
    expected_message: str,
    visibility: tuple[bool, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run each public action through its handler against a stateful browser fake."""
    handler = _handler(tool_name)
    action_handler = _action_handler(handler)
    page = _FakePage()

    async def fake_with_browser(action: Callable[[_FakePage], Any]) -> dict[str, Any]:
        return await action(page)

    action_globals = action_handler.__globals__
    monkeypatch.setitem(action_globals, "with_browser", fake_with_browser)
    monkeypatch.setitem(action_globals, "navigate_to_tweet", AsyncMock(return_value=None))
    monkeypatch.setitem(action_globals, "check_login", AsyncMock(return_value=None))
    if visibility:
        monkeypatch.setitem(action_globals, "is_visible", AsyncMock(side_effect=visibility))

    result = await handler(data)

    assert result["result"]["status"] == "ok"
    assert result["result"]["message"] == expected_message
    assert any(call[0] == "click" for call in page.calls)
    if tool_name in {"x_post", "x_reply", "x_quote"}:
        assert any(call[0] == "fill" for call in page.calls)


@pytest.mark.asyncio
async def test_x_post_uses_the_configured_persistent_browser(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The public action owns display setup, profile cleanup, and browser closure."""
    page = _FakePage(visible=(True,))
    profile_path = tmp_path / "x-profile"
    closed, launches = _install_playwright(monkeypatch, page, profile_path)

    result = await _handler("x_post")({"content": "Pynchy release note"})

    assert result["result"]["message"] == "Tweet posted: Pynchy release note"
    assert launches == [
        {
            "user_data_dir": str(profile_path),
            "executable_path": "/usr/bin/google-chrome",
            "headless": False,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-sync",
            ],
            "ignore_default_args": ["--enable-automation"],
        }
    ]
    assert closed == [True]


@pytest.mark.asyncio
async def test_x_session_setup_reports_existing_login_with_vnc(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    page = _FakePage(visible=(True,))
    profile_path = tmp_path / "x-profile"
    closed, launches = _install_playwright(monkeypatch, page, profile_path)
    action_handler = _action_handler(_handler("setup_x_session"))
    action_globals = action_handler.__globals__
    monkeypatch.setitem(action_globals, "has_display", lambda: False)
    monkeypatch.setitem(action_globals, "ensure_xvfb", lambda: None)
    monkeypatch.setitem(
        action_globals,
        "start_vnc_layer",
        lambda: ([], "http://host:6080/vnc.html?autoconnect=true"),
    )
    monkeypatch.setitem(action_globals, "profile_dir", lambda _name: profile_path)
    monkeypatch.setitem(action_globals, "cleanup_lock_files", lambda _path: None)
    monkeypatch.setitem(action_globals, "launch_kwargs", lambda _path: {"headless": False})
    monkeypatch.setitem(action_globals, "is_visible", AsyncMock(return_value=True))
    monkeypatch.setitem(action_globals, "stop_procs", lambda _procs: None)

    result = await _handler("setup_x_session")({"timeout_seconds": 1})

    assert result == {
        "result": {
            "status": "ok",
            "message": f"Already logged in to X. Profile saved at {profile_path}",
            "novnc_url": "http://host:6080/vnc.html?autoconnect=true",
        }
    }
    assert launches == [{"headless": False}]
    assert closed == [True]


@pytest.mark.asyncio
async def test_x_session_setup_omits_vnc_url_when_display_exists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profile_path = tmp_path / "x-profile"
    action_handler = _action_handler(_handler("setup_x_session"))
    action_globals = action_handler.__globals__
    monkeypatch.setitem(action_globals, "has_display", lambda: True)
    monkeypatch.setitem(action_globals, "ensure_xvfb", lambda: None)
    monkeypatch.setitem(action_globals, "profile_dir", lambda _name: profile_path)
    monkeypatch.setitem(action_globals, "cleanup_lock_files", lambda _path: None)
    monkeypatch.setitem(action_globals, "launch_kwargs", lambda _path: {"headless": False})
    monkeypatch.setitem(action_globals, "stop_procs", lambda _procs: None)

    logged_in_page = _FakePage(visible=(True,))
    _install_playwright(monkeypatch, logged_in_page, profile_path)
    monkeypatch.setitem(action_globals, "is_visible", AsyncMock(return_value=True))
    assert await _handler("setup_x_session")({}) == {
        "result": {
            "status": "ok",
            "message": f"Already logged in to X. Profile saved at {profile_path}",
        }
    }

    completed_login_page = _FakePage(visible=(False,))
    _install_playwright(monkeypatch, completed_login_page, profile_path)
    monkeypatch.setitem(action_globals, "is_visible", AsyncMock(return_value=False))
    assert await _handler("setup_x_session")({}) == {
        "result": {
            "status": "ok",
            "profile_dir": str(profile_path),
            "message": "X session saved. Future tool calls will use this session.",
        }
    }


@pytest.mark.asyncio
async def test_x_session_setup_waits_for_login_and_reports_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profile_path = tmp_path / "x-profile"
    page = _FakePage(visible=(False,), selector_error="timed out")
    _install_playwright(monkeypatch, page, profile_path)
    action_handler = _action_handler(_handler("setup_x_session"))
    action_globals = action_handler.__globals__
    monkeypatch.setitem(action_globals, "has_display", lambda: True)
    monkeypatch.setitem(action_globals, "ensure_xvfb", lambda: None)
    monkeypatch.setitem(action_globals, "profile_dir", lambda _name: profile_path)
    monkeypatch.setitem(action_globals, "cleanup_lock_files", lambda _path: None)
    monkeypatch.setitem(action_globals, "launch_kwargs", lambda _path: {"headless": False})
    monkeypatch.setitem(action_globals, "is_visible", AsyncMock(return_value=False))
    monkeypatch.setitem(action_globals, "stop_procs", lambda _procs: None)

    result = await _handler("setup_x_session")({"timeout_seconds": 1})

    assert result == {"error": "Login not completed within 1s. Try again with a longer timeout."}


@pytest.mark.asyncio
async def test_x_session_setup_saves_completed_login_and_surfaces_setup_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    profile_path = tmp_path / "x-profile"
    page = _FakePage(visible=(False,))
    _install_playwright(monkeypatch, page, profile_path)
    action_handler = _action_handler(_handler("setup_x_session"))
    action_globals = action_handler.__globals__
    monkeypatch.setitem(action_globals, "has_display", lambda: False)
    monkeypatch.setitem(action_globals, "ensure_xvfb", lambda: None)
    monkeypatch.setitem(
        action_globals,
        "start_vnc_layer",
        lambda: ([], "http://host:6080/vnc.html?autoconnect=true"),
    )
    monkeypatch.setitem(action_globals, "profile_dir", lambda _name: profile_path)
    monkeypatch.setitem(action_globals, "cleanup_lock_files", lambda _path: None)
    monkeypatch.setitem(action_globals, "launch_kwargs", lambda _path: {"headless": False})
    monkeypatch.setitem(action_globals, "is_visible", AsyncMock(return_value=False))
    monkeypatch.setitem(action_globals, "stop_procs", lambda _procs: None)

    result = await _handler("setup_x_session")({"timeout_seconds": 1})

    assert result["result"]["profile_dir"] == str(profile_path)
    assert result["result"]["novnc_url"] == "http://host:6080/vnc.html?autoconnect=true"

    def fail_setup() -> None:
        raise RuntimeError("display missing")

    monkeypatch.setitem(action_globals, "ensure_xvfb", fail_setup)
    assert await _handler("setup_x_session")({}) == {"error": "display missing"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "page", "expected"),
    [
        ({"content": ""}, None, "Tweet content cannot be empty"),
        (
            {"tweet_url": "123"},
            _FakePage(visible=(False,)),
            "Tweet not found. It may have been deleted or the URL is invalid.",
        ),
        (
            {"tweet_url": "https://x.com/broken"},
            _FakePage(navigation_error="offline"),
            "Navigation failed: offline",
        ),
    ],
)
async def test_x_actions_surface_input_and_navigation_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    data: dict[str, str],
    page: _FakePage | None,
    expected: str,
) -> None:
    if page is not None:
        _install_playwright(monkeypatch, page, tmp_path / "x-profile")
        result = await _handler("x_like")(data)
    else:
        result = await _handler("x_post")(data)

    assert result == {"error": expected}


@pytest.mark.asyncio
async def test_x_post_treats_detached_login_locators_as_not_visible(monkeypatch):
    handler = _handler("x_post")
    action_handler = _action_handler(handler)
    page = _FakePage()

    async def detached(_self):
        await asyncio.sleep(0)
        raise RuntimeError("detached")

    async def fake_with_browser(action):
        return await action(page)

    monkeypatch.setitem(action_handler.__globals__, "with_browser", fake_with_browser)
    monkeypatch.setattr(_FakeLocator, "is_visible", detached)

    result = await handler({"content": "posted after reload"})

    assert result["result"]["status"] == "ok"


@pytest.mark.asyncio
async def test_x_like_normalizes_a_host_without_an_http_scheme(monkeypatch):
    handler = _handler("x_like")
    action_handler = _action_handler(handler)
    page = _FakePage(visible=(True,))

    async def fake_with_browser(action):
        return await action(page)

    monkeypatch.setitem(action_handler.__globals__, "with_browser", fake_with_browser)
    monkeypatch.setitem(action_handler.__globals__, "is_visible", AsyncMock(return_value=True))

    result = await handler({"tweet_url": "x.com/status/123"})

    assert result["result"]["message"] == "Tweet already liked"
    assert page.calls[0][1] == "https://x.com/status/123"


@pytest.mark.asyncio
async def test_x_post_reports_an_expired_login_from_the_public_action(monkeypatch):
    handler = _handler("x_post")
    action_handler = _action_handler(handler)
    page = _FakePage(visible=(False, True))

    async def fake_with_browser(action):
        return await action(page)

    monkeypatch.setitem(action_handler.__globals__, "with_browser", fake_with_browser)

    assert await handler({"content": "hello"}) == {
        "error": "X login expired. Run setup_x_session to re-authenticate."
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "data"),
    [
        ("x_reply", {"tweet_url": "123", "content": "reply"}),
        ("x_retweet", {"tweet_url": "123"}),
        ("x_quote", {"tweet_url": "123", "comment": "quote"}),
    ],
)
async def test_x_actions_surface_navigation_failures_for_all_tweet_actions(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    data: dict[str, str],
) -> None:
    handler = _handler(tool_name)
    action_handler = _action_handler(handler)
    page = _FakePage()

    async def fake_with_browser(action):
        return await action(page)

    monkeypatch.setitem(action_handler.__globals__, "with_browser", fake_with_browser)
    monkeypatch.setitem(
        action_handler.__globals__, "navigate_to_tweet", AsyncMock(return_value="bad nav")
    )

    assert await handler(data) == {"error": "bad nav"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "data", "expected"),
    [
        ("x_like", {}, "Please provide a tweet URL"),
        ("x_reply", {}, "Please provide a tweet URL"),
        ("x_reply", {"tweet_url": "123"}, "Reply content cannot be empty"),
        ("x_retweet", {}, "Please provide a tweet URL"),
        ("x_quote", {}, "Please provide a tweet URL"),
        ("x_quote", {"tweet_url": "123"}, "Comment content cannot be empty"),
        ("x_post", {"content": "x" * 281}, "Tweet exceeds 280 char limit (current: 281)"),
    ],
)
async def test_x_actions_validate_public_input(tool_name, data, expected):
    assert await _handler(tool_name)(data) == {"error": expected}


@pytest.mark.asyncio
async def test_x_post_reports_login_and_disabled_button_states(monkeypatch):
    action_handler = _action_handler(_handler("x_post"))
    page = _FakePage()

    async def fake_with_browser(action):
        return await action(page)

    monkeypatch.setitem(action_handler.__globals__, "with_browser", fake_with_browser)
    monkeypatch.setitem(
        action_handler.__globals__,
        "check_login",
        AsyncMock(return_value="Please log in to X first"),
    )
    assert await _handler("x_post")({"content": "hello"}) == {"error": "Please log in to X first"}

    monkeypatch.setitem(action_handler.__globals__, "check_login", AsyncMock(return_value=None))

    async def disabled(_self, _name):
        await asyncio.sleep(0)
        return "true"

    monkeypatch.setattr(_FakeLocator, "get_attribute", disabled)
    assert await _handler("x_post")({"content": "hello"}) == {
        "error": "Post button disabled. Content may be empty or exceed limit."
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "visibility", "expected"),
    [
        ("x_like", (True,), "Tweet already liked"),
        (
            "x_like",
            (False, False),
            "Like action completed but could not verify success",
        ),
        ("x_retweet", (True,), "Tweet already retweeted"),
        (
            "x_retweet",
            (False, False),
            "Retweet action completed but could not verify success",
        ),
    ],
)
async def test_x_actions_report_existing_and_unverified_effects(
    monkeypatch, tool_name, visibility, expected
):
    handler = _handler(tool_name)
    action_handler = _action_handler(handler)
    page = _FakePage()

    async def fake_with_browser(action):
        return await action(page)

    monkeypatch.setitem(action_handler.__globals__, "with_browser", fake_with_browser)
    monkeypatch.setitem(
        action_handler.__globals__, "navigate_to_tweet", AsyncMock(return_value=None)
    )
    monkeypatch.setitem(action_handler.__globals__, "is_visible", AsyncMock(side_effect=visibility))

    result = await handler({"tweet_url": "123"})

    assert result["result"]["message"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["x_reply", "x_quote"])
async def test_x_modal_actions_report_disabled_submit_button(monkeypatch, tool_name):
    handler = _handler(tool_name)
    action_handler = _action_handler(handler)
    page = _FakePage()

    async def fake_with_browser(action):
        return await action(page)

    async def disabled(_self, _name):
        await asyncio.sleep(0)
        return "true"

    monkeypatch.setitem(action_handler.__globals__, "with_browser", fake_with_browser)
    monkeypatch.setitem(
        action_handler.__globals__, "navigate_to_tweet", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(_FakeLocator, "get_attribute", disabled)
    data = (
        {"tweet_url": "123", "content": "reply"}
        if tool_name == "x_reply"
        else {
            "tweet_url": "123",
            "comment": "quote",
        }
    )

    assert await handler(data) == {
        "error": "Submit button disabled. Content may be empty or exceed limit."
    }
