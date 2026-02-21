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

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pluggy

from pynchy.integrations.browser import (
    check_browser_plugin_deps,
    chrome_path,
    has_display,
    profile_dir,
    start_virtual_display,
    stop_procs,
)
from pynchy.logger import logger

hookimpl = pluggy.HookimplMarker("pynchy")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _project_root() -> Path:
    root = os.environ.get("PYNCHY_PROJECT_ROOT", "")
    return Path(root) if root else Path.cwd()


def _find_dotenv() -> Path:
    return _project_root() / ".env"


def _update_dotenv_var(dotenv_path: Path, key: str, value: str) -> None:
    import dotenv

    dotenv_path.touch(exist_ok=True)
    dotenv.set_key(str(dotenv_path), key, value)


def _launch_kwargs(profile: Path, *, headless: bool) -> dict:
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
        if not xoxc and re.search(r"/client/", page.url):
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


async def _handle_refresh_slack_tokens(data: dict) -> dict:
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
    except Exception as exc:
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


async def _handle_setup_slack_session(data: dict) -> dict:
    """Launch a headed browser for manual Slack login. Saves the session."""
    from playwright.async_api import async_playwright

    workspace_name = data.get("workspace_name", "")
    if not workspace_name:
        return {"error": "workspace_name is required"}

    workspace_url = data.get("workspace_url", "https://app.slack.com")
    timeout_seconds = data.get("timeout_seconds", 120)
    profile = profile_dir(workspace_name)
    vnc_procs: list[subprocess.Popen] = []
    original_display = os.environ.get("DISPLAY")

    try:
        novnc_url: str | None = None
        if not has_display():
            vnc_procs, novnc_url = start_virtual_display()

        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                **_launch_kwargs(profile, headless=False),
            )
            page = context.pages[0] if context.pages else await context.new_page()

            await page.goto(workspace_url, wait_until="networkidle")

            # Already logged in?
            if re.search(r"/client/", page.url):
                await context.close()
                result: dict[str, Any] = {
                    "status": "ok",
                    "message": f"Already logged in. Profile saved at {profile}",
                }
                if novnc_url:
                    result["novnc_url"] = novnc_url
                return {"result": result}

            # Wait for the human to complete login
            try:
                await page.wait_for_url(
                    re.compile(r"/client/"),
                    timeout=timeout_seconds * 1000,
                )
            except Exception:
                await context.close()
                result = {
                    "status": "error",
                    "error": (
                        f"Login not completed within {timeout_seconds}s. "
                        "Try again with a longer timeout."
                    ),
                }
                if novnc_url:
                    result["novnc_url"] = novnc_url
                return {"error": result["error"]}

            await context.close()

        result = {
            "status": "ok",
            "profile_dir": str(profile),
            "message": (
                f"Session saved. Future refresh_slack_tokens calls with "
                f'workspace_name="{workspace_name}" will use this session.'
            ),
        }
        if novnc_url:
            result["novnc_url"] = novnc_url
        return {"result": result}

    except Exception as exc:
        logger.error("Slack session setup failed", error=str(exc))
        return {"error": str(exc)}

    finally:
        stop_procs(vnc_procs)
        if original_display is not None:
            os.environ["DISPLAY"] = original_display
        elif "DISPLAY" in os.environ and vnc_procs:
            del os.environ["DISPLAY"]


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------


# Run at import time (same behavior as the old standalone script)
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
