"""Hermetic end-to-end coverage for Pynchy's mutating X service actions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest

from pynchy.plugins.integrations.x_integration import XIntegrationPlugin

if TYPE_CHECKING:
    from collections.abc import Callable


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


class _FakePage:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(selector, self.calls)

    def get_by_role(self, role: str) -> _FakeLocator:
        return _FakeLocator(role, self.calls)

    async def goto(self, url: str, **_kwargs: object) -> None:
        self.calls.append(("goto", url, None))

    async def wait_for_timeout(self, _milliseconds: int) -> None:
        return None


def _handler(tool_name: str) -> Callable[[dict[str, Any]], Any]:
    return XIntegrationPlugin().pynchy_service_handler().handlers[tool_name]


def _action_handler(handler: Callable[[dict[str, Any]], Any]) -> Callable[[dict[str, Any]], Any]:
    """Unwrap service-tool and runtime type decorators to reach the action module."""
    current = handler
    while "with_browser" not in current.__globals__:
        current = current.__wrapped__
    return current


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
