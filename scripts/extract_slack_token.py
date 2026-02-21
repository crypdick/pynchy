# /// script
# requires-python = ">=3.12"
# dependencies = ["fastmcp>=2.0", "playwright", "python-dotenv"]
# ///
"""Slack browser token extractor — FastMCP server.

Standalone PEP 723 uv script. Heavy dependencies (Playwright, FastMCP) are
resolved ad-hoc by ``uv run`` and never touch pynchy's virtualenv.

Uses Playwright **persistent browser contexts** so that a single manual login
(via VNC or SSH X-forwarding) persists across subsequent headless runs. Each
Slack workspace gets its own browser profile directory.

Two tools:

- ``refresh_slack_tokens`` — headless: navigates to Slack using persistent
  session, extracts tokens, writes them to ``.env``. Requires a prior
  manual login via ``setup_slack_session``.

- ``setup_slack_session`` — **headed** (visible browser): opens Slack login
  page for the human to complete CAPTCHA / magic-link / SSO. Once logged in,
  the session is saved to the persistent profile for future headless use.

Usage::

    uv run scripts/extract_slack_token.py
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import dotenv
from fastmcp import FastMCP


def _ensure_playwright_browsers():
    """Install Chromium and its system dependencies if not already present.

    Uses ``--with-deps`` to also install OS-level libraries (libgbm, libnss3,
    etc.) that Chromium needs. This requires root on Linux; if it fails due to
    permissions, falls back to browser-only install and warns.
    """
    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # --with-deps may fail without root; try browser-only as fallback
        print("playwright install --with-deps failed, trying browser-only...", flush=True)
        fallback = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            text=True,
        )
        if fallback.returncode != 0:
            print(f"Playwright install warning: {fallback.stderr}", flush=True)


def _check_system_deps():
    """Warn at startup about missing system packages.

    On headless servers, ``setup_slack_session`` needs Xvfb + noVNC for
    interactive browser login via a web-based VNC viewer.
    """
    if not os.environ.get("DISPLAY"):
        missing = [t for t in ("Xvfb", "x11vnc", "websockify") if not shutil.which(t)]
        if missing:
            print(
                f"Headless server — setup_slack_session needs: "
                f"{', '.join(missing)}. Install with: apt install xvfb x11vnc novnc",
                flush=True,
            )


_ensure_playwright_browsers()
_check_system_deps()

mcp = FastMCP("Slack Token Extractor")


def _project_root() -> Path:
    root = os.environ.get("PYNCHY_PROJECT_ROOT", "")
    return Path(root) if root else Path.cwd()


def _find_dotenv() -> Path:
    return _project_root() / ".env"


def _profile_dir(workspace_name: str) -> Path:
    """Per-workspace persistent browser profile directory."""
    d = _project_root() / "data" / "playwright-profiles" / workspace_name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _update_dotenv_var(dotenv_path: Path, key: str, value: str) -> None:
    dotenv_path.touch(exist_ok=True)
    dotenv.set_key(str(dotenv_path), key, value)


# ---------------------------------------------------------------------------
# Virtual display (headless server support)
# ---------------------------------------------------------------------------

_XVFB_DISPLAY = ":99"
_VNC_PORT = 5999
_NOVNC_PORT = 6080
_NOVNC_WEB_DIR = "/usr/share/novnc"


def _has_display() -> bool:
    """Return True if a working X display is available."""
    if not os.environ.get("DISPLAY"):
        return False
    try:
        r = subprocess.run(["xdpyinfo"], capture_output=True, timeout=5)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _start_virtual_display() -> tuple[list[subprocess.Popen], str]:
    """Start Xvfb + x11vnc + noVNC. Returns (processes, novnc_url).

    Requires system packages: ``apt install xvfb x11vnc novnc``
    """
    missing = [t for t in ("Xvfb", "x11vnc", "websockify") if not shutil.which(t)]
    if missing:
        raise RuntimeError(
            f"Headless display requires: {', '.join(missing)}. "
            "Install with: apt install xvfb x11vnc novnc"
        )

    procs: list[subprocess.Popen] = []
    try:
        xvfb = subprocess.Popen(
            ["Xvfb", _XVFB_DISPLAY, "-screen", "0", "1280x720x24"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(xvfb)
        time.sleep(0.5)
        if xvfb.poll() is not None:
            raise RuntimeError(f"Xvfb exited immediately (code {xvfb.returncode})")

        x11vnc = subprocess.Popen(
            [
                "x11vnc", "-display", _XVFB_DISPLAY,
                "-forever", "-nopw", "-rfbport", str(_VNC_PORT), "-quiet",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(x11vnc)
        time.sleep(0.5)
        if x11vnc.poll() is not None:
            raise RuntimeError(f"x11vnc exited immediately (code {x11vnc.returncode})")

        ws_cmd = ["websockify", str(_NOVNC_PORT), f"localhost:{_VNC_PORT}"]
        if os.path.isdir(_NOVNC_WEB_DIR):
            ws_cmd[1:1] = ["--web", _NOVNC_WEB_DIR]
        websockify_proc = subprocess.Popen(
            ws_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        procs.append(websockify_proc)
        time.sleep(0.5)
        if websockify_proc.poll() is not None:
            raise RuntimeError(
                f"websockify exited immediately (code {websockify_proc.returncode})"
            )

        os.environ["DISPLAY"] = _XVFB_DISPLAY
        return procs, f"http://HOST:{_NOVNC_PORT}/vnc.html?autoconnect=true"

    except Exception:
        _stop_procs(procs)
        raise


def _stop_procs(procs: list[subprocess.Popen]) -> None:
    """Terminate processes gracefully, then force-kill stragglers."""
    for proc in reversed(procs):
        if proc.poll() is None:
            proc.terminate()
    for proc in reversed(procs):
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)


async def _extract_tokens(
    profile: Path,
    workspace_url: str,
) -> dict[str, str]:
    """Open Slack with a persistent context and extract xoxc + xoxd tokens.

    Expects the profile to already have a valid session (from setup_slack_session).
    Returns {"xoxc": "xoxc-...", "xoxd": "xoxd-..."}.

    Extraction strategy (handles both regular and Enterprise Grid Slack):
    1. If the page has ``boot_data.api_token`` (enterprise workspace selector
       or any authenticated Slack page), use that as xoxc.
    2. Otherwise, if we landed on ``/client/``, extract xoxc from localStorage.
    3. xoxd always comes from the ``d`` cookie.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=True,
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


@mcp.tool()
async def refresh_slack_tokens(
    workspace_name: str,
    xoxc_var: str,
    xoxd_var: str,
    workspace_url: str = "https://app.slack.com",
) -> dict:
    """Extract fresh Slack browser tokens from a persistent browser session.

    Requires a prior ``setup_slack_session`` call to establish the browser
    session via manual login. Once set up, this tool can run headlessly to
    extract fresh tokens whenever the old ones expire.

    Args:
        workspace_name: Identifier for the browser profile (e.g., "acme").
            Must match the name used during ``setup_slack_session``.
        xoxc_var: Env var name to write the new xoxc token to (e.g., "SLACK_XOXC_ACME")
        xoxd_var: Env var name to write the new xoxd token to (e.g., "SLACK_XOXD_ACME")
        workspace_url: Slack workspace URL (default: https://app.slack.com)

    Returns:
        {"status": "ok", ...} on success, {"status": "error", "error": "..."} on failure.
    """
    dotenv_path = _find_dotenv()
    profile = _profile_dir(workspace_name)

    try:
        tokens = await _extract_tokens(profile, workspace_url)
    except Exception as exc:
        return {"status": "error", "error": str(exc)}

    _update_dotenv_var(dotenv_path, xoxc_var, tokens["xoxc"])
    _update_dotenv_var(dotenv_path, xoxd_var, tokens["xoxd"])

    return {
        "status": "ok",
        "xoxc_var": xoxc_var,
        "xoxd_var": xoxd_var,
        "message": f"Tokens written to {dotenv_path}",
    }


@mcp.tool()
async def setup_slack_session(
    workspace_name: str,
    workspace_url: str = "https://app.slack.com",
    timeout_seconds: int = 120,
) -> dict:
    """Launch a headed browser for manual Slack login. Saves the session.

    Opens a **visible** Chromium window navigated to the Slack login page.
    The human completes the login (CAPTCHA, magic link, SSO — whatever Slack
    requires). Once the browser reaches ``/client/``, the session is saved to
    a persistent profile directory for future headless use.

    On headless servers (no X display), automatically starts a virtual display
    with noVNC web access on port 6080. **Before calling this tool on a
    headless server**, tell the human to open
    ``http://<server>:6080/vnc.html?autoconnect=true`` so they can interact
    with the browser to complete the login.

    Args:
        workspace_name: Identifier for the browser profile (e.g., "acme").
            Used by ``refresh_slack_tokens`` to find this session later.
        workspace_url: Slack workspace URL (default: https://app.slack.com)
        timeout_seconds: How long to wait for login completion (default: 120s)

    Returns:
        {"status": "ok", ...} on success, {"status": "error", "error": "..."} on failure.
    """
    from playwright.async_api import async_playwright

    profile = _profile_dir(workspace_name)
    vnc_procs: list[subprocess.Popen] = []
    original_display = os.environ.get("DISPLAY")

    try:
        # Provision virtual display if running headless
        novnc_url: str | None = None
        if not _has_display():
            vnc_procs, novnc_url = _start_virtual_display()

        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                headless=False,
            )
            page = context.pages[0] if context.pages else await context.new_page()

            await page.goto(workspace_url, wait_until="networkidle")

            # Already logged in?
            if re.search(r"/client/", page.url):
                await context.close()
                result: dict = {
                    "status": "ok",
                    "message": f"Already logged in. Profile saved at {profile}",
                }
                if novnc_url:
                    result["novnc_url"] = novnc_url
                return result

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
                return result

            # Session is now saved in the persistent profile
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
        return result

    except Exception as exc:
        return {"status": "error", "error": str(exc)}

    finally:
        _stop_procs(vnc_procs)
        # Restore original DISPLAY
        if original_display is not None:
            os.environ["DISPLAY"] = original_display
        elif "DISPLAY" in os.environ and vnc_procs:
            del os.environ["DISPLAY"]


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8457)
