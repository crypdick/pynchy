from __future__ import annotations

import os
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


class _TokenPage:
    def __init__(self, evaluations: list[object], url: str) -> None:
        self._evaluations = evaluations
        self.url = url

    async def goto(self, _url: str, *, wait_until: str) -> None:
        assert wait_until == "networkidle"

    async def evaluate(self, _script: str) -> object:
        return self._evaluations.pop(0)

    async def wait_for_timeout(self, milliseconds: int) -> None:
        assert milliseconds == 3000


class _TokenContext:
    def __init__(self, page: _TokenPage, cookies: list[dict[str, str]]) -> None:
        self.pages: list[_TokenPage] = []
        self._page = page
        self._cookies = cookies
        self.closed = False

    async def new_page(self) -> _TokenPage:
        self.pages.append(self._page)
        return self._page

    async def cookies(self) -> list[dict[str, str]]:
        return self._cookies

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


@pytest.mark.asyncio
async def test_refresh_slack_tokens_extracts_local_storage_tokens_from_an_empty_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    context = _TokenContext(
        _TokenPage([None, "xoxc-local"], "https://app.slack.com/client/T123"),
        [{"name": "other", "value": "ignored"}, {"name": "d", "value": "xoxd-test"}],
    )
    monkeypatch.setattr(f"{_SLACK_EXTRACTOR_MODULE}._find_dotenv", lambda: tmp_path / ".env")
    monkeypatch.setattr(
        f"{_SLACK_EXTRACTOR_MODULE}.profile_dir", lambda _name: tmp_path / "profile"
    )
    monkeypatch.setattr(f"{_SLACK_EXTRACTOR_MODULE}.chrome_path", lambda: "/usr/bin/google-chrome")
    monkeypatch.setitem(
        __import__("sys").modules,
        "playwright.async_api",
        type(
            "_PlaywrightModule",
            (),
            {"async_playwright": lambda: _FakePlaywright(context)},
        ),
    )

    response = await _handler("refresh_slack_tokens")(
        {"workspace_name": "acme", "xoxc_var": "XOXC", "xoxd_var": "XOXD"}
    )

    assert response["result"]["status"] == "ok"
    assert "XOXC='xoxc-local'" in (tmp_path / ".env").read_text()
    assert "XOXD='xoxd-test'" in (tmp_path / ".env").read_text()
    assert context.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("evaluations", "url", "cookies", "message"),
    [
        ([None], "https://app.slack.com/signin", [], "Not logged in"),
        (["xoxc-test"], "https://app.slack.com/client/T123", [], "Failed to extract xoxd"),
    ],
)
async def test_refresh_slack_tokens_reports_missing_browser_tokens(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    evaluations: list[object],
    url: str,
    cookies: list[dict[str, str]],
    message: str,
) -> None:
    context = _TokenContext(_TokenPage(evaluations, url), cookies)
    monkeypatch.setattr(f"{_SLACK_EXTRACTOR_MODULE}._find_dotenv", lambda: tmp_path / ".env")
    monkeypatch.setattr(
        f"{_SLACK_EXTRACTOR_MODULE}.profile_dir", lambda _name: tmp_path / "profile"
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "playwright.async_api",
        type("_PlaywrightModule", (), {"async_playwright": lambda: _FakePlaywright(context)}),
    )

    response = await _handler("refresh_slack_tokens")(
        {"workspace_name": "acme", "xoxc_var": "XOXC", "xoxd_var": "XOXD"}
    )

    assert message in response["error"]
    assert context.closed is True


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


@pytest.mark.asyncio
async def test_setup_slack_session_rejects_missing_workspace_name() -> None:
    assert await _handler("setup_slack_session")({}) == {"error": "workspace_name is required"}


@pytest.mark.asyncio
async def test_setup_slack_session_restores_display_after_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    stopped = Mock()

    def start_display() -> tuple[list[object], str]:
        os.environ["DISPLAY"] = ":99"
        return [object()], "http://novnc.local/session"

    monkeypatch.setattr(f"{_SLACK_EXTRACTOR_MODULE}.has_display", lambda: False)
    monkeypatch.setattr(f"{_SLACK_EXTRACTOR_MODULE}.start_virtual_display", start_display)
    monkeypatch.setattr(f"{_SLACK_EXTRACTOR_MODULE}.stop_procs", stopped)
    monkeypatch.setattr(
        f"{_SLACK_EXTRACTOR_MODULE}._run_slack_session_setup",
        AsyncMock(side_effect=RuntimeError("browser unavailable")),
    )

    response = await _handler("setup_slack_session")({"workspace_name": "acme"})

    assert response == {"error": "browser unavailable"}
    stopped.assert_called_once()
    assert "DISPLAY" not in os.environ


@pytest.mark.asyncio
async def test_setup_slack_session_restores_an_existing_display_after_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISPLAY", ":7")
    monkeypatch.setattr(f"{_SLACK_EXTRACTOR_MODULE}.has_display", lambda: True)
    monkeypatch.setattr(f"{_SLACK_EXTRACTOR_MODULE}.stop_procs", Mock())
    monkeypatch.setattr(
        f"{_SLACK_EXTRACTOR_MODULE}._run_slack_session_setup",
        AsyncMock(side_effect=RuntimeError("browser unavailable")),
    )

    response = await _handler("setup_slack_session")({"workspace_name": "acme"})

    assert response == {"error": "browser unavailable"}
    assert os.environ["DISPLAY"] == ":7"


def test_plugin_reports_no_skill_path_when_the_skill_directory_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(f"{_SLACK_EXTRACTOR_MODULE}.Path.is_dir", lambda _path: False)

    assert SlackTokenExtractorPlugin().pynchy_skill_paths() == []
