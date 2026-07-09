"""Built-in Slack token extractor plugin (service handler).

Provides host-side handlers for extracting Slack browser tokens (xoxc/xoxd)
via Playwright persistent browser contexts.  Uses the system Chrome binary
(``CHROME_PATH``) — Playwright's vendored Chromium is never used (see
``integrations.browser`` for rationale).

After one manual login (human handles CAPTCHA/magic-link), subsequent token
extractions run headlessly using the saved session.

Two handlers:
- ``refresh_slack_tokens`` — headless: extract tokens and write to ``.env``
- ``setup_slack_session`` — headed: open browser for manual login

The container-side IPC relay (_tools_slack_tokens.py) sends service requests
through IPC; the host service handler dispatches to these handlers after
policy enforcement.
"""

from __future__ import annotations

import contextlib
import os
import re
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003, RUF100 - beartype resolves this runtime annotation.
from typing import TYPE_CHECKING, Any

import pluggy

from pynchy.logger import logger
from pynchy.plugins.integrations.browser import (
    check_browser_plugin_deps,
    chrome_path,
    has_display,
    profile_dir,
    project_root,
    start_virtual_display,
    stop_procs,
)

if TYPE_CHECKING:
    import subprocess

hookimpl = pluggy.HookimplMarker("pynchy")


@dataclass(frozen=True)
class _SlackSetupRequest:
    workspace_name: str
    workspace_url: str
    timeout_seconds: int
    profile: Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_dotenv() -> Path:
    return project_root() / ".env"


def _update_dotenv_var(dotenv_path: Path, key: str, value: str) -> None:
    import dotenv

    dotenv_path.touch(exist_ok=True)
    dotenv.set_key(str(dotenv_path), key, value)


def _launch_kwargs(profile: Path, *, headless: bool) -> dict[str, Any]:
    """Build kwargs for ``launch_persistent_context``.

    Always uses the system Chrome binary (``CHROME_PATH``).
    See ``integrations.browser`` module docstring for rationale.
    """
    return {
        "user_data_dir": str(profile),
        "executable_path": chrome_path(),
        "headless": headless,
    }


# ---------------------------------------------------------------------------
# Token extraction
# ---------------------------------------------------------------------------


async def _extract_tokens(
    profile: Path,
    workspace_url: str,
) -> dict[str, str]:
    """Open Slack with a persistent context and extract xoxc + xoxd tokens.

    Expects the profile to already have a valid session (from setup_slack_session).

    Extraction strategy (handles both regular and Enterprise Grid Slack):
    1. If the page has ``boot_data.api_token``, use that as xoxc.
    2. Otherwise, if we landed on ``/client/``, extract xoxc from localStorage.
    3. xoxd always comes from the ``d`` cookie.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            **_launch_kwargs(profile, headless=True),
        )
        page = context.pages[0] if context.pages else await context.new_page()

        await page.goto(workspace_url, wait_until="networkidle")

        # Strategy 1: boot_data.api_token (works on any authenticated page)
        xoxc = await page.evaluate(
            "() => typeof boot_data !== 'undefined' && boot_data.api_token || null"
        )

        # Strategy 2: localStorage on /client/ pages
        if not xoxc and "/client/" in page.url:
            await page.wait_for_timeout(3000)
            xoxc = await page.evaluate("""() => {
                const raw = localStorage.getItem('localConfig_v2');
                if (!raw) return null;
                const config = JSON.parse(raw);
                const teams = config.teams || {};
                const match = location.pathname.match(/\\/client\\/([A-Z0-9]+)/);
                if (!match) {
                    const firstTeam = Object.values(teams)[0];
                    return firstTeam ? firstTeam.token : null;
                }
                const team = teams[match[1]];
                return team ? team.token : null;
            }""")

        # xoxd from cookie
        cookies = await context.cookies()
        xoxd = None
        for cookie in cookies:
            if cookie["name"] == "d" and cookie["value"].startswith("xoxd-"):
                xoxd = cookie["value"]
                break

        await context.close()

        if not xoxc:
            raise RuntimeError(
                "Not logged in — persistent session expired or never set up. "
                "Run setup_slack_session first to complete manual login."
            )
        if not xoxd:
            raise RuntimeError("Failed to extract xoxd cookie (d)")

        return {"xoxc": xoxc, "xoxd": xoxd}


# ---------------------------------------------------------------------------
# Handler functions
# ---------------------------------------------------------------------------


async def _handle_refresh_slack_tokens(data: dict[str, Any]) -> dict[str, Any]:
    """Extract fresh Slack browser tokens and write to .env."""
    workspace_name = data.get("workspace_name", "")
    if not workspace_name:
        return {"error": "workspace_name is required"}

    xoxc_var = data.get("xoxc_var", "")
    xoxd_var = data.get("xoxd_var", "")
    if not xoxc_var or not xoxd_var:
        return {"error": "xoxc_var and xoxd_var are required"}

    workspace_url = data.get("workspace_url", "https://app.slack.com")
    dotenv_path = _find_dotenv()
    profile = profile_dir(workspace_name)

    try:
        tokens = await _extract_tokens(profile, workspace_url)
    except Exception as exc:  # noqa: BLE001, RUF100 - token extraction failures are surfaced to the caller.
        logger.error("Slack token extraction failed", error=str(exc))
        return {"error": str(exc)}

    _update_dotenv_var(dotenv_path, xoxc_var, tokens["xoxc"])
    _update_dotenv_var(dotenv_path, xoxd_var, tokens["xoxd"])

    return {
        "result": {
            "status": "ok",
            "xoxc_var": xoxc_var,
            "xoxd_var": xoxd_var,
            "message": f"Tokens written to {dotenv_path}",
        }
    }


async def _handle_setup_slack_session(data: dict[str, Any]) -> dict[str, Any]:
    """Launch a headed browser for manual Slack login. Saves the session."""
    request = _parse_slack_setup_request(data)
    if request is None:
        return {"error": "workspace_name is required"}

    vnc_procs: list[subprocess.Popen[Any]] = []
    original_display = os.environ.get("DISPLAY")
    novnc_url: str | None = None

    try:
        if not has_display():
            vnc_procs, novnc_url = start_virtual_display()
        return await _run_slack_session_setup(request, novnc_url)

    except Exception as exc:  # noqa: BLE001, RUF100 - session setup failures are surfaced to the caller.
        logger.error("Slack session setup failed", error=str(exc))
        return {"error": str(exc)}

    finally:
        stop_procs(vnc_procs)
        if original_display is not None:
            os.environ["DISPLAY"] = original_display
        elif "DISPLAY" in os.environ and vnc_procs:
            del os.environ["DISPLAY"]


async def _run_slack_session_setup(
    request: _SlackSetupRequest,
    novnc_url: str | None,
) -> dict[str, Any]:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw, contextlib.AsyncExitStack() as stack:
        context = await pw.chromium.launch_persistent_context(
            **_launch_kwargs(request.profile, headless=False),
        )
        stack.push_async_callback(context.close)
        page = context.pages[0] if context.pages else await context.new_page()

        await page.goto(request.workspace_url, wait_until="networkidle")

        # Already logged in?
        if "/client/" in page.url:
            return {"result": _already_logged_in_result(request.profile, novnc_url)}

        # Wait for the human to complete login
        try:
            await page.wait_for_url(
                re.compile(r"/client/"),
                timeout=request.timeout_seconds * 1000,
            )
        except Exception as exc:  # noqa: BLE001, RUF100 - login timeout handling is caller-facing.
            logger.warning(
                "Slack login not completed before timeout",
                timeout_seconds=request.timeout_seconds,
                error=str(exc),
            )
            return _timeout_result(request.timeout_seconds, novnc_url)

    return {"result": _saved_session_result(request, novnc_url)}


def _parse_slack_setup_request(data: dict[str, Any]) -> _SlackSetupRequest | None:
    workspace_name = data.get("workspace_name", "")
    if not workspace_name:
        return None

    return _SlackSetupRequest(
        workspace_name=workspace_name,
        workspace_url=data.get("workspace_url", "https://app.slack.com"),
        timeout_seconds=data.get("timeout_seconds", 120),
        profile=profile_dir(workspace_name),
    )


def _already_logged_in_result(profile: Path, novnc_url: str | None) -> dict[str, Any]:
    return _with_optional_novnc_url(
        {
            "status": "ok",
            "message": f"Already logged in. Profile saved at {profile}",
        },
        novnc_url,
    )


def _saved_session_result(
    request: _SlackSetupRequest,
    novnc_url: str | None,
) -> dict[str, Any]:
    return _with_optional_novnc_url(
        {
            "status": "ok",
            "profile_dir": str(request.profile),
            "message": (
                "Session saved. Future refresh_slack_tokens calls with "
                f'workspace_name="{request.workspace_name}" will use this session.'
            ),
        },
        novnc_url,
    )


def _timeout_result(timeout_seconds: int, novnc_url: str | None) -> dict[str, Any]:
    payload = {
        "error": f"Login not completed within {timeout_seconds}s. Try again with a longer timeout."
    }
    return _with_optional_novnc_url(payload, novnc_url)


def _with_optional_novnc_url(payload: dict[str, Any], novnc_url: str | None) -> dict[str, Any]:
    if novnc_url is None:
        return payload
    return {**payload, "novnc_url": novnc_url}


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------


# Validate browser deps at import time so failures surface on plugin load
check_browser_plugin_deps("setup_slack_session")


class SlackTokenExtractorPlugin:
    @hookimpl
    def pynchy_service_handler(self) -> dict[str, Any]:
        return {
            "tools": {
                "refresh_slack_tokens": _handle_refresh_slack_tokens,
                "setup_slack_session": _handle_setup_slack_session,
            },
        }
