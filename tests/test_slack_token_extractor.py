from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest

from pynchy.plugins.integrations.slack_token_extractor import SlackTokenExtractorPlugin

TIMED_OUT_MESSAGE = "timed out"
_SLACK_EXTRACTOR_MODULE = "pynchy.plugins.integrations.slack_token_extractor._plugin"

if TYPE_CHECKING:
    from pathlib import Path


class _FakePage:
    def __init__(self, url: str = "https://app.slack.com/signin") -> None:
        self.url = url

    async def goto(self, _url: str, *, wait_until: str) -> None:
        assert wait_until == "networkidle"

    async def wait_for_url(self, _pattern, *, timeout: int) -> None:  # noqa: ASYNC109
        # Mirrors Playwright's timeout= API shape for this fake.
        assert timeout == 5_000
        raise TimeoutError(TIMED_OUT_MESSAGE)


class _FakeContext:
    def __init__(self, page: _FakePage | None = None) -> None:
        self.pages = [page or _FakePage()]
        self.closed = False

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


def _handler(name: str):
    action = SlackTokenExtractorPlugin().pynchy_service_handler().action_for(name)
    assert action is not None
    return action.handler


@pytest.mark.action("integration.slack.tokens.refresh")
@pytest.mark.asyncio
async def test_refresh_slack_tokens_persists_tokens_from_the_authenticated_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Exercise extraction through the service boundary without a real Slack account."""
    dotenv_path = tmp_path / ".env"

    monkeypatch.setattr(f"{_SLACK_EXTRACTOR_MODULE}._find_dotenv", lambda: dotenv_path)
    monkeypatch.setattr(
        f"{_SLACK_EXTRACTOR_MODULE}.profile_dir", lambda _name: tmp_path / "profile"
    )
    monkeypatch.setattr(
        f"{_SLACK_EXTRACTOR_MODULE}._extract_tokens",
        AsyncMock(return_value={"xoxc": "xoxc-test", "xoxd": "xoxd-test"}),
    )

    action = SlackTokenExtractorPlugin().pynchy_service_handler().action_for("refresh_slack_tokens")
    assert action is not None
    handler = action.handler
    response = await handler(
        {
            "workspace_name": "acme",
            "xoxc_var": "SLACK_XOXC_ACME",
            "xoxd_var": "SLACK_XOXD_ACME",
        }
    )

    assert response["result"]["status"] == "ok"
    assert "SLACK_XOXC_ACME='xoxc-test'" in dotenv_path.read_text()
    assert "SLACK_XOXD_ACME='xoxd-test'" in dotenv_path.read_text()


@pytest.mark.action("integration.slack.session.setup")
@pytest.mark.asyncio
async def test_setup_slack_session_timeout_returns_novnc_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    context = _FakeContext()
    stop_procs = Mock()

    monkeypatch.setattr(f"{_SLACK_EXTRACTOR_MODULE}.has_display", lambda: False)
    monkeypatch.setattr(
        f"{_SLACK_EXTRACTOR_MODULE}.start_virtual_display",
        lambda: ([], "http://novnc.local/session"),
    )
    monkeypatch.setattr(f"{_SLACK_EXTRACTOR_MODULE}.stop_procs", stop_procs)
    monkeypatch.setattr(
        f"{_SLACK_EXTRACTOR_MODULE}.profile_dir", lambda _name: tmp_path / "profile"
    )
    monkeypatch.setattr(f"{_SLACK_EXTRACTOR_MODULE}.chrome_path", lambda: "/usr/bin/google-chrome")

    def fake_async_playwright() -> _FakePlaywright:
        return _FakePlaywright(context)

    monkeypatch.setitem(
        __import__("sys").modules,
        "playwright.async_api",
        type("_PlaywrightModule", (), {"async_playwright": fake_async_playwright}),
    )

    action = SlackTokenExtractorPlugin().pynchy_service_handler().action_for("setup_slack_session")
    assert action is not None
    handler = action.handler
    response = await handler(
        {
            "workspace_name": "acme",
            "workspace_url": "https://app.slack.com/client",
            "timeout_seconds": 5,
        }
    )

    assert response == {
        "error": "Login not completed within 5s. Try again with a longer timeout.",
        "novnc_url": "http://novnc.local/session",
    }
    assert context.closed is True
    stop_procs.assert_called_once_with([])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({}, "workspace_name is required"),
        ({"workspace_name": "acme"}, "xoxc_var and xoxd_var are required"),
    ],
)
async def test_refresh_slack_tokens_rejects_incomplete_requests(data, expected) -> None:
    assert await _handler("refresh_slack_tokens")(data) == {"error": expected}


@pytest.mark.asyncio
async def test_refresh_slack_tokens_surfaces_browser_extraction_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(f"{_SLACK_EXTRACTOR_MODULE}._find_dotenv", lambda: tmp_path / ".env")
    monkeypatch.setattr(
        f"{_SLACK_EXTRACTOR_MODULE}.profile_dir", lambda _name: tmp_path / "profile"
    )
    monkeypatch.setattr(
        f"{_SLACK_EXTRACTOR_MODULE}._extract_tokens", AsyncMock(side_effect=RuntimeError("expired"))
    )

    response = await _handler("refresh_slack_tokens")(
        {"workspace_name": "acme", "xoxc_var": "XOXC", "xoxd_var": "XOXD"}
    )

    assert response == {"error": "expired"}


@pytest.mark.action("integration.slack.session.setup")
@pytest.mark.asyncio
async def test_setup_slack_session_reports_an_existing_authenticated_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    context = _FakeContext(_FakePage("https://app.slack.com/client/T123"))
    monkeypatch.setattr(f"{_SLACK_EXTRACTOR_MODULE}.has_display", lambda: True)
    monkeypatch.setattr(f"{_SLACK_EXTRACTOR_MODULE}.stop_procs", Mock())
    monkeypatch.setattr(
        f"{_SLACK_EXTRACTOR_MODULE}.profile_dir", lambda _name: tmp_path / "profile"
    )
    monkeypatch.setattr(f"{_SLACK_EXTRACTOR_MODULE}.chrome_path", lambda: "/usr/bin/google-chrome")
    monkeypatch.setitem(
        __import__("sys").modules,
        "playwright.async_api",
        type("_PlaywrightModule", (), {"async_playwright": lambda: _FakePlaywright(context)}),
    )

    response = await _handler("setup_slack_session")({"workspace_name": "acme"})

    assert response["result"]["status"] == "ok"
    assert "Already logged in" in response["result"]["message"]
    assert context.closed is True
