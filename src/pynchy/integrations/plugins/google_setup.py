"""Built-in Google Setup plugin (service handler).

Provides host-side handlers for GCP project setup, Drive API enablement,
OAuth consent screen configuration, and OAuth token exchange.  Consolidates
the standalone ``scripts/setup_gdrive_oauth.py`` and
``scripts/gdrive_oauth_authorize.py`` into the plugin system.

Three handlers:
- ``enable_gdrive_api`` — enable Drive API for an existing GCP project
- ``setup_gdrive`` — full flow: project + API + consent + credentials + OAuth
- ``authorize_gdrive`` — OAuth token exchange (assumes credentials JSON exists)

Uses the system Chrome/Chromium binary (``chrome_path()``) — Playwright's
vendored Chromium is never used (see ``integrations.browser`` for rationale).
On headless servers, auto-starts Xvfb + noVNC so the user can interact
via web browser for Google login and OAuth consent.

Each GCP Console step attempts Playwright automation first and falls back
to printed instructions + noVNC if selectors fail (Google changes their
UI often).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pluggy

from pynchy.integrations.browser import (
    chrome_path,
    has_display,
    profile_dir,
    start_virtual_display,
    stop_procs,
)
from pynchy.logger import logger

hookimpl = pluggy.HookimplMarker("pynchy")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GCP_CONSOLE = "https://console.cloud.google.com"
_OAUTH_CALLBACK_PORT = 3000
_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
_DEFAULT_PROJECT_ID = "pynchy-gdrive"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _project_root() -> Path:
    root = os.environ.get("PYNCHY_PROJECT_ROOT", "")
    return Path(root) if root else Path.cwd()


def _download_dir() -> Path:
    """Temporary download directory for credential files."""
    d = _project_root() / "data" / "tmp" / "gdrive-setup"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _keys_path() -> Path:
    return _project_root() / "data" / "gcp-oauth.keys.json"


# ---------------------------------------------------------------------------
# GCP Console helpers
# ---------------------------------------------------------------------------


async def _dismiss_modals(page) -> None:
    """Try to dismiss common GCP Console popups/modals."""
    for text in ("Got it", "Dismiss", "No thanks", "Skip", "Not now"):
        try:
            btn = page.get_by_role("button", name=re.compile(text, re.I)).first
            if await btn.is_visible(timeout=500):
                await btn.click()
                await page.wait_for_timeout(300)
        except Exception:
            pass
    try:
        close = page.locator('[aria-label="Close"]').first
        if await close.is_visible(timeout=500):
            await close.click()
    except Exception:
        pass


async def _wait_for_login(page) -> None:
    """Wait until Google login is complete (if a login page appeared)."""
    if "accounts.google.com" in page.url:
        logger.info("Waiting for Google login via noVNC")
        await page.wait_for_url(
            lambda url: "accounts.google.com" not in url,
            timeout=300_000,  # 5 minutes
        )
        logger.info("Google login complete")
        await page.wait_for_timeout(2000)


async def _try_step(page, step_fn, fallback_msg: str, done_check=None, timeout: int = 60):
    """Attempt an automated Console step; fall back to manual + noVNC.

    Args:
        page: Playwright page.
        step_fn: Async callable that attempts the automation.
        fallback_msg: Instructions printed if automation fails.
        done_check: Async callable(page) -> bool that returns True when the
            step is complete (used for manual fallback polling).
        timeout: Max seconds to wait for manual completion.
    """
    try:
        await step_fn(page)
        return
    except Exception as exc:
        logger.warning("GCP automation step failed, falling back to manual", error=str(exc))
        with contextlib.suppress(Exception):
            await page.screenshot(path="/tmp/gdrive-setup-debug.png")

    logger.info("Manual step required", instructions=fallback_msg)

    if done_check:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if await done_check(page):
                    logger.info("Manual step completed")
                    return
            except Exception:
                pass
            await page.wait_for_timeout(5000)
        logger.warning("Timed out waiting for manual step", timeout=timeout)


# ---------------------------------------------------------------------------
# GCP Console steps
# ---------------------------------------------------------------------------


async def _ensure_project(page, project_id: str) -> None:
    """Create a GCP project (or verify it exists)."""
    logger.info("Ensuring GCP project exists", project_id=project_id)

    await page.goto(
        f"{_GCP_CONSOLE}/home/dashboard?project={project_id}",
        wait_until="domcontentloaded",
    )
    await page.wait_for_timeout(8000)
    await _wait_for_login(page)
    await _dismiss_modals(page)

    page_text = await page.text_content("body") or ""
    has_access = (
        project_id in page.url
        and "error" not in page.url.lower()
        and "doesn't exist" not in page_text.lower()
        and "not found" not in page_text.lower()
        and "need additional access" not in page_text.lower()
        and "permission" not in page_text.lower()[:500]
    )
    if has_access:
        logger.info("GCP project already exists", project_id=project_id)
        return

    logger.info("Creating GCP project", project_id=project_id)
    await page.goto(f"{_GCP_CONSOLE}/projectcreate", wait_until="domcontentloaded")
    await page.wait_for_timeout(5000)
    await _dismiss_modals(page)

    async def _automate(p):
        name_input = p.get_by_role("textbox").first
        await name_input.click()
        await name_input.fill(project_id)
        await p.wait_for_timeout(1000)

        create_btn = p.get_by_role("button", name=re.compile(r"^create$", re.I))
        await create_btn.click()

        await p.wait_for_timeout(3000)
        for _ in range(20):
            await p.wait_for_timeout(3000)
            if "dashboard" in p.url and project_id in p.url:
                return
            body = await p.text_content("body") or ""
            if "has been created" in body.lower():
                return
        await p.goto(
            f"{_GCP_CONSOLE}/home/dashboard?project={project_id}",
            wait_until="domcontentloaded",
        )

    async def _project_exists(p) -> bool:
        if project_id not in p.url:
            return False
        body = await p.text_content("body") or ""
        return "doesn't exist" not in body.lower()

    await _try_step(
        page,
        _automate,
        f'Create a new project named "{project_id}" and wait for it to finish.',
        done_check=_project_exists,
    )
    logger.info("GCP project ready", project_id=project_id)


async def _ensure_drive_api(page, project_id: str) -> None:
    """Enable Google Drive API for the project."""
    logger.info("Enabling Google Drive API", project_id=project_id)

    await page.goto(
        f"{_GCP_CONSOLE}/apis/library/drive.googleapis.com?project={project_id}",
        wait_until="domcontentloaded",
    )
    await page.wait_for_timeout(5000)
    await _dismiss_modals(page)

    body = await page.text_content("body") or ""
    if "manage" in body.lower() or "api enabled" in body.lower():
        logger.info("Drive API already enabled")
        return

    async def _automate(p):
        enable_btn = p.get_by_role("button", name=re.compile(r"enable", re.I))
        await enable_btn.click()
        await p.wait_for_timeout(5000)

    async def _api_enabled(p) -> bool:
        body = await p.text_content("body") or ""
        return (
            "manage" in body.lower() or "api enabled" in body.lower() or "disable" in body.lower()
        )

    await _try_step(
        page,
        _automate,
        'Click the "Enable" button for Google Drive API.',
        done_check=_api_enabled,
    )
    logger.info("Drive API enabled")


async def _ensure_consent_screen(page, project_id: str) -> None:
    """Configure OAuth consent screen (External, Testing mode)."""
    logger.info("Configuring OAuth consent screen", project_id=project_id)

    await page.goto(
        f"{_GCP_CONSOLE}/apis/credentials/consent?project={project_id}",
        wait_until="domcontentloaded",
    )
    await _dismiss_modals(page)
    await page.wait_for_timeout(5000)

    body = await page.text_content("body") or ""
    if "edit app" in body.lower() or "publishing status" in body.lower():
        logger.info("OAuth consent screen already configured")
        return

    async def _automate(p):
        external = p.get_by_text("External", exact=False).first
        await external.click()
        await p.wait_for_timeout(500)

        create_btn = p.get_by_role("button", name=re.compile(r"^create$", re.I))
        await create_btn.click()
        await p.wait_for_timeout(3000)

        inputs = p.get_by_role("textbox")
        count = await inputs.count()
        if count >= 1:
            await inputs.nth(0).fill("pynchy-gdrive")
        if count >= 2:
            await inputs.nth(1).fill("")  # needs user's email

        for _ in range(4):
            await p.wait_for_timeout(1000)
            save_btn = p.get_by_role("button", name=re.compile(r"save and continue", re.I))
            if await save_btn.count() > 0:
                await save_btn.click()
                await p.wait_for_timeout(2000)
            else:
                break

    async def _consent_configured(p) -> bool:
        body = await p.text_content("body") or ""
        return "edit app" in body.lower() or "publishing status" in body.lower()

    await _try_step(
        page,
        _automate,
        (
            "Configure the OAuth consent screen:\n"
            '  1. Select "External" and click Create\n'
            '  2. Fill in App name ("pynchy-gdrive"), support email, dev email\n'
            '  3. Click "Save and Continue" through all pages (skip scopes/test users)'
        ),
        done_check=_consent_configured,
        timeout=180,
    )
    logger.info("OAuth consent screen configured")


async def _create_oauth_credentials(page, project_id: str) -> Path:
    """Create Desktop App OAuth credentials and download the JSON."""
    logger.info("Creating OAuth Desktop App credentials", project_id=project_id)

    dl_dir = _download_dir()

    await page.goto(
        f"{_GCP_CONSOLE}/apis/credentials/oauthclient?project={project_id}",
        wait_until="domcontentloaded",
    )
    await _dismiss_modals(page)
    await page.wait_for_timeout(2000)

    dest = dl_dir / "gcp-oauth.keys.json"

    async def _automate(p):
        type_dropdown = p.locator("mat-select, [role='listbox'], [role='combobox']").first
        await type_dropdown.click()
        await p.wait_for_timeout(500)

        desktop_opt = p.get_by_text("Desktop app", exact=False).first
        await desktop_opt.click()
        await p.wait_for_timeout(1000)

        name_input = p.get_by_role("textbox").first
        if await name_input.count() > 0:
            await name_input.clear()
            await name_input.fill("pynchy-gdrive")

        create_btn = p.get_by_role("button", name=re.compile(r"^create$", re.I))
        await create_btn.click()
        await p.wait_for_timeout(3000)

        async with p.expect_download(timeout=10_000) as download_info:
            dl_btn = p.get_by_role("button", name=re.compile(r"download.*json", re.I))
            await dl_btn.click()
        download = await download_info.value
        await download.save_as(str(dest))

    try:
        await _automate(page)
    except Exception as exc:
        logger.warning("Credential creation automation failed", error=str(exc))
        logger.info(
            "Manual step required: create Desktop App credentials",
            instructions=(
                '1. Select application type "Desktop app"\n'
                '2. Name it "pynchy-gdrive"\n'
                '3. Click "Create"\n'
                '4. Click "Download JSON" in the dialog'
            ),
        )
        # Watch for download
        try:
            async with page.expect_download(timeout=180_000) as download_info:
                download = await download_info.value
                await download.save_as(str(dest))
        except Exception as exc:
            raise RuntimeError(
                "Could not detect credential JSON download. "
                "Download it manually and place at data/gcp-oauth.keys.json"
            ) from exc

    if not dest.exists():
        raise RuntimeError(f"Credentials file not found at {dest}")

    with open(dest) as f:
        data = json.load(f)
    if "installed" not in data and "web" not in data:
        raise RuntimeError("Invalid credentials JSON — missing 'installed' or 'web' key")

    logger.info("OAuth credentials saved", path=str(dest))
    return dest


# ---------------------------------------------------------------------------
# OAuth token exchange
# ---------------------------------------------------------------------------


def _parse_client_credentials(keys_path: Path) -> tuple[str, str]:
    """Extract client_id and client_secret from the GCP OAuth JSON."""
    with open(keys_path) as f:
        data = json.load(f)
    client = data.get("installed") or data.get("web")
    if not client:
        raise RuntimeError("Invalid credentials JSON")
    return client["client_id"], client["client_secret"]


def _build_auth_url(client_id: str) -> str:
    """Build the Google OAuth authorization URL."""
    params = {
        "client_id": client_id,
        "redirect_uri": f"http://localhost:{_OAUTH_CALLBACK_PORT}",
        "response_type": "code",
        "scope": _DRIVE_READONLY_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{_GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"


def _start_callback_server() -> tuple[threading.Event, list[str], HTTPServer]:
    """Start HTTP server to receive the OAuth callback.

    Returns (done_event, auth_code_list, server).
    """
    auth_codes: list[str] = []
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code = query.get("code", [None])[0]
            if code:
                auth_codes.append(code)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>Authorization successful!</h2>"
                b"<p>You can close this tab. Setup will continue.</p>"
                b"</body></html>"
            )
            done.set()

        def log_message(self, *args):
            pass

    server = HTTPServer(("0.0.0.0", _OAUTH_CALLBACK_PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return done, auth_codes, server


def _exchange_code_for_tokens(code: str, client_id: str, client_secret: str) -> dict:
    """Exchange the authorization code for access + refresh tokens."""
    data = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": f"http://localhost:{_OAUTH_CALLBACK_PORT}",
            "grant_type": "authorization_code",
        }
    ).encode()

    req = urllib.request.Request(
        _GOOGLE_TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req) as resp:
        tokens = json.loads(resp.read())

    if "error" in tokens:
        raise RuntimeError(f"Token exchange failed: {tokens['error']}")

    # Add expiry_date (ms) for compatibility with googleapis Node.js client
    if "expires_in" in tokens:
        tokens["expiry_date"] = int(time.time() * 1000) + tokens["expires_in"] * 1000

    return tokens


def _save_credentials_to_volume(tokens: dict) -> None:
    """Write credentials.json into the mcp-gdrive Docker volume."""
    import tempfile

    subprocess.run(
        ["docker", "volume", "create", "mcp-gdrive"],
        capture_output=True,
    )

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(tokens, f)
        tmp_path = f.name

    try:
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                "mcp-gdrive:/gdrive-server",
                "-v",
                f"{tmp_path}:/tmp/credentials.json:ro",
                "busybox",
                "cp",
                "/tmp/credentials.json",
                "/gdrive-server/credentials.json",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to save to Docker volume: {result.stderr}")
    finally:
        os.unlink(tmp_path)


def _read_project_id() -> str | None:
    """Auto-detect project ID from existing credentials JSON.

    The GCP OAuth client JSON contains a ``project_id`` field that holds
    the human-readable project ID (e.g., ``vocal-invention-488106-k6``).
    """
    kp = _keys_path()
    if not kp.exists():
        return None
    try:
        with open(kp) as f:
            data = json.load(f)
        client = data.get("installed") or data.get("web")
        if client and client.get("project_id"):
            return client["project_id"]
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# OAuth authorization flow (shared between setup_gdrive and authorize_gdrive)
# ---------------------------------------------------------------------------


async def _run_oauth_flow(page, keys_path: Path) -> dict:
    """Run the OAuth consent + token exchange flow.

    Opens the Google OAuth URL in the given Playwright page, waits for
    the user to click "Allow", captures the callback, and exchanges the
    code for tokens.

    Returns the token dict.
    """
    client_id, client_secret = _parse_client_credentials(keys_path)
    done_event, auth_codes, callback_server = _start_callback_server()

    auth_url = _build_auth_url(client_id)
    await page.goto(auth_url, wait_until="domcontentloaded")

    logger.info("Waiting for OAuth consent (click Allow in the browser)")

    deadline = time.time() + 300  # 5 minutes
    while not done_event.is_set() and time.time() < deadline:
        await asyncio.sleep(0.5)

    callback_server.shutdown()

    if not auth_codes:
        raise RuntimeError(
            "OAuth callback not received within 5 minutes. "
            "Make sure you clicked 'Allow' in the browser."
        )

    logger.info("Exchanging authorization code for tokens")
    tokens = _exchange_code_for_tokens(auth_codes[0], client_id, client_secret)

    if "refresh_token" not in tokens:
        logger.warning("No refresh_token received — access token will expire")

    return tokens


# ---------------------------------------------------------------------------
# Handler functions
# ---------------------------------------------------------------------------


async def _handle_enable_gdrive_api(data: dict) -> dict:
    """Enable Google Drive API for an existing GCP project.

    Returns noVNC URL for human interaction if on a headless server.
    """
    from playwright.async_api import async_playwright

    project_id = data.get("project_id") or _read_project_id() or _DEFAULT_PROJECT_ID
    profile = profile_dir("google")
    vnc_procs: list[subprocess.Popen] = []
    original_display = os.environ.get("DISPLAY")

    try:
        novnc_url: str | None = None
        if not has_display():
            vnc_procs, novnc_url = start_virtual_display()

        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                executable_path=chrome_path(),
                headless=False,
                viewport={"width": 1280, "height": 720},
                timeout=60_000,
            )
            context.set_default_navigation_timeout(60_000)
            context.set_default_timeout(15_000)
            page = context.pages[0] if context.pages else await context.new_page()

            await page.goto(_GCP_CONSOLE, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)
            await _wait_for_login(page)
            await _dismiss_modals(page)

            await _ensure_drive_api(page, project_id)
            await context.close()

        result: dict[str, Any] = {
            "status": "ok",
            "message": f"Google Drive API enabled for project {project_id}",
        }
        if novnc_url:
            result["novnc_url"] = novnc_url
        return {"result": result}

    except Exception as exc:
        logger.error("enable_gdrive_api failed", error=str(exc))
        return {"error": str(exc)}

    finally:
        stop_procs(vnc_procs)
        if original_display is not None:
            os.environ["DISPLAY"] = original_display
        elif "DISPLAY" in os.environ and vnc_procs:
            del os.environ["DISPLAY"]


async def _handle_setup_gdrive(data: dict) -> dict:
    """Full Google Drive setup: project + API + consent + credentials + OAuth."""
    from playwright.async_api import async_playwright

    project_id = data.get("project_id") or _read_project_id() or _DEFAULT_PROJECT_ID
    profile = profile_dir("google")
    vnc_procs: list[subprocess.Popen] = []
    original_display = os.environ.get("DISPLAY")

    try:
        novnc_url: str | None = None
        if not has_display():
            vnc_procs, novnc_url = start_virtual_display()

        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                executable_path=chrome_path(),
                headless=False,
                accept_downloads=True,
                viewport={"width": 1280, "height": 720},
                timeout=60_000,
            )
            context.set_default_navigation_timeout(60_000)
            context.set_default_timeout(15_000)
            page = context.pages[0] if context.pages else await context.new_page()

            # Navigate to GCP Console (triggers login if needed)
            await page.goto(_GCP_CONSOLE, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)
            await _wait_for_login(page)
            await _dismiss_modals(page)

            # GCP Console setup
            await _ensure_project(page, project_id)
            await _ensure_drive_api(page, project_id)
            await _ensure_consent_screen(page, project_id)
            creds_path = await _create_oauth_credentials(page, project_id)

            # OAuth authorization flow
            tokens = await _run_oauth_flow(page, creds_path)

            await context.close()

        # Save to Docker volume
        _save_credentials_to_volume(tokens)

        # Back up the OAuth client keys
        dest = _keys_path()
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(creds_path, dest)

        # Clean up temp download dir
        dl = _download_dir()
        if dl.exists():
            shutil.rmtree(dl, ignore_errors=True)

        result: dict[str, Any] = {
            "status": "ok",
            "message": "Google Drive OAuth setup complete",
            "keys_path": str(dest),
        }
        if novnc_url:
            result["novnc_url"] = novnc_url
        return {"result": result}

    except Exception as exc:
        logger.error("setup_gdrive failed", error=str(exc))
        return {"error": str(exc)}

    finally:
        stop_procs(vnc_procs)
        if original_display is not None:
            os.environ["DISPLAY"] = original_display
        elif "DISPLAY" in os.environ and vnc_procs:
            del os.environ["DISPLAY"]


async def _handle_authorize_gdrive(data: dict) -> dict:
    """OAuth token exchange only (assumes credentials JSON exists)."""
    from playwright.async_api import async_playwright

    keys_path = Path(data.get("keys_path", str(_keys_path())))
    if not keys_path.exists():
        return {
            "error": (
                f"{keys_path} not found. Run setup_gdrive first to create "
                "GCP credentials, or copy your OAuth client JSON there."
            )
        }

    profile = profile_dir("google")
    vnc_procs: list[subprocess.Popen] = []
    original_display = os.environ.get("DISPLAY")

    try:
        novnc_url: str | None = None
        if not has_display():
            vnc_procs, novnc_url = start_virtual_display()

        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                executable_path=chrome_path(),
                headless=False,
                viewport={"width": 1280, "height": 720},
                timeout=60_000,
            )
            page = context.pages[0] if context.pages else await context.new_page()

            tokens = await _run_oauth_flow(page, keys_path)
            await context.close()

        _save_credentials_to_volume(tokens)

        result: dict[str, Any] = {
            "status": "ok",
            "message": "Credentials saved to mcp-gdrive Docker volume",
            "has_refresh_token": "refresh_token" in tokens,
        }
        if novnc_url:
            result["novnc_url"] = novnc_url
        return {"result": result}

    except Exception as exc:
        logger.error("authorize_gdrive failed", error=str(exc))
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


class GoogleSetupPlugin:
    @hookimpl
    def pynchy_service_handler(self) -> dict[str, Any]:
        return {
            "tools": {
                "enable_gdrive_api": _handle_enable_gdrive_api,
                "setup_gdrive": _handle_setup_gdrive,
                "authorize_gdrive": _handle_authorize_gdrive,
            },
        }
