"""Built-in Google Setup plugin — MCP specs + service handlers.

Two plugin classes:

**GoogleMcpPlugin** — provides base MCP server specs for ``gdrive`` and
``gcal``.  These are templates: they exist only to be inherited by config
instances (e.g., ``[mcp_servers.gdrive.mycompany]``).

**GoogleSetupPlugin** — provides host-side handlers for GCP project setup,
API enablement, OAuth consent screen configuration, and OAuth token
exchange.  Single idempotent tool: ``setup_google(chrome_profile=...)``.

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
    project_root,
    start_virtual_display,
    stop_procs,
)
from pynchy.logger import logger

hookimpl = pluggy.HookimplMarker("pynchy")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GCP_CONSOLE = "https://console.cloud.google.com"
_OAUTH_CALLBACK_PORT = 8085
_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_DEFAULT_PROJECT_ID = "pynchy-gdrive"

# ---------------------------------------------------------------------------
# Scope registry — maps MCP server template names to OAuth scopes + API IDs
# ---------------------------------------------------------------------------

_SERVER_SCOPES: dict[str, tuple[list[str], str]] = {
    "gdrive": (
        ["https://www.googleapis.com/auth/drive.readonly"],
        "drive.googleapis.com",
    ),
    "gcal": (
        ["https://www.googleapis.com/auth/calendar"],
        "calendar-json.googleapis.com",
    ),
}

# Service management scope is always included (enables REST API enablement)
_SERVICE_MANAGEMENT_SCOPE = "https://www.googleapis.com/auth/service.management"

_SERVICE_USAGE_URL = "https://serviceusage.googleapis.com/v1"


# ---------------------------------------------------------------------------
# Paths — chrome-profile-aware
# ---------------------------------------------------------------------------


def _chrome_profile_dir(profile_name: str) -> Path:
    """Host directory for a chrome profile's auth artifacts."""
    d = project_root() / "data" / "chrome-profiles" / profile_name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _keys_path(profile_name: str) -> Path:
    """OAuth client credentials (gcp-oauth.keys.json) for a chrome profile."""
    return _chrome_profile_dir(profile_name) / "gcp-oauth.keys.json"


def _credentials_path(profile_name: str) -> Path:
    """OAuth tokens (credentials.json) for a chrome profile."""
    return _chrome_profile_dir(profile_name) / "credentials.json"


def _download_dir() -> Path:
    """Temporary download directory for credential files."""
    d = project_root() / "data" / "tmp" / "google-setup"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Scope computation — union scopes from all services referencing a profile
# ---------------------------------------------------------------------------


def _compute_scopes_for_profile(profile_name: str) -> tuple[str, list[str]]:
    """Compute the union of OAuth scopes and API IDs for a chrome profile.

    Checks which MCP server instances reference this profile across all
    workspaces.  Returns (space-separated scopes, sorted API IDs).
    """
    from pynchy.config import get_settings

    scopes: set[str] = set()
    apis: set[str] = set()

    for svc, (svc_scopes, api_id) in _SERVER_SCOPES.items():
        instance_name = f"{svc}.{profile_name}"
        for ws in get_settings().workspaces.values():
            if instance_name in (ws.mcp_servers or []):
                scopes.update(svc_scopes)
                apis.add(api_id)
                break

    # Always include service management scope for REST API enablement
    scopes.add(_SERVICE_MANAGEMENT_SCOPE)

    return " ".join(sorted(scopes)), sorted(apis)


# ---------------------------------------------------------------------------
# REST API helpers
# ---------------------------------------------------------------------------


def _get_project_number(keys_path: Path) -> str | None:
    """Extract the GCP project number from the OAuth client_id."""
    if not keys_path.exists():
        return None
    try:
        with open(keys_path) as f:
            data = json.load(f)
        client = data.get("installed") or data.get("web")
        if client and client.get("client_id"):
            return client["client_id"].split("-", 1)[0]
    except Exception:
        pass
    return None


def _read_project_id(keys_path: Path) -> str | None:
    """Auto-detect project ID from existing credentials JSON."""
    if not keys_path.exists():
        return None
    try:
        with open(keys_path) as f:
            data = json.load(f)
        client = data.get("installed") or data.get("web")
        if client and client.get("project_id"):
            return client["project_id"]
    except Exception:
        pass
    return None


def _refresh_access_token(profile_name: str) -> str | None:
    """Refresh the OAuth access token using stored credentials.

    Reads from chrome profile directory (not Docker volume).
    """
    kp = _keys_path(profile_name)
    if not kp.exists():
        return None

    try:
        client_id, client_secret = _parse_client_credentials(kp)
    except Exception:
        return None

    creds_path = _credentials_path(profile_name)
    if not creds_path.exists():
        return None

    try:
        creds = json.loads(creds_path.read_text())
        refresh_token = creds.get("refresh_token")
        if not refresh_token:
            return None
    except Exception:
        return None

    data = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode()
    req = urllib.request.Request(
        _GOOGLE_TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            tokens = json.loads(resp.read())
        return tokens.get("access_token")
    except Exception:
        return None


def _enable_api_via_rest(project_number: str, access_token: str, api_id: str) -> bool:
    """Enable a Google API via the Service Usage REST API."""
    url = f"{_SERVICE_USAGE_URL}/projects/{project_number}/services/{api_id}:enable"
    req = urllib.request.Request(
        url,
        data=b"{}",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
        logger.info("API enabled via REST", api=api_id, result_name=result.get("name", ""))
        return True
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        if "SCOPE_INSUFFICIENT" in body or "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in body:
            logger.info("REST enable failed (insufficient scopes)", api=api_id)
        else:
            logger.warning("REST enable failed", api=api_id, status=exc.code, body=body[:200])
        return False
    except Exception as exc:
        logger.warning("REST enable failed", api=api_id, error=str(exc))
        return False


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
    """Attempt an automated Console step; fall back to manual + noVNC."""
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


async def _ensure_api(page, project_id: str, api_id: str) -> None:
    """Enable a Google API for the project."""
    logger.info("Enabling Google API", project_id=project_id, api=api_id)

    await page.goto(
        f"{_GCP_CONSOLE}/apis/library/{api_id}?project={project_id}",
        wait_until="domcontentloaded",
    )
    await page.wait_for_timeout(5000)
    await _dismiss_modals(page)

    enable_btn = page.get_by_role("button", name=re.compile(r"^enable$", re.I))
    if await enable_btn.count() == 0:
        logger.info("API already enabled (no Enable button found)", api=api_id)
        return

    async def _automate(p):
        btn = p.get_by_role("button", name=re.compile(r"^enable$", re.I))
        await btn.click()
        await p.wait_for_timeout(5000)

    async def _api_enabled(p) -> bool:
        btn = p.get_by_role("button", name=re.compile(r"^enable$", re.I))
        return await btn.count() == 0

    await _try_step(
        page,
        _automate,
        f'Click the "Enable" button for {api_id}.',
        done_check=_api_enabled,
    )
    logger.info("API enabled", api=api_id)


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
        try:
            async with page.expect_download(timeout=180_000) as download_info:
                download = await download_info.value
                await download.save_as(str(dest))
        except Exception as exc:
            raise RuntimeError(
                "Could not detect credential JSON download. "
                "Download it manually and place at "
                "data/chrome-profiles/<profile>/gcp-oauth.keys.json"
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


def _build_auth_url(client_id: str, scopes: str) -> str:
    """Build the Google OAuth authorization URL."""
    params = {
        "client_id": client_id,
        "redirect_uri": f"http://localhost:{_OAUTH_CALLBACK_PORT}",
        "response_type": "code",
        "scope": scopes,
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{_GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"


def _start_callback_server() -> tuple[threading.Event, list[str], HTTPServer]:
    """Start HTTP server to receive the OAuth callback."""
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


def _save_credentials_to_profile(tokens: dict, profile_name: str) -> Path:
    """Write credentials.json to the chrome profile directory."""
    dest = _credentials_path(profile_name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(tokens, indent=2))
    logger.info(
        "OAuth tokens saved to chrome profile",
        profile=profile_name,
        path=str(dest),
        has_refresh_token="refresh_token" in tokens,
    )
    return dest


# ---------------------------------------------------------------------------
# OAuth authorization flow
# ---------------------------------------------------------------------------


async def _run_oauth_flow(page, keys_path: Path, scopes: str) -> dict:
    """Run the OAuth consent + token exchange flow."""
    client_id, client_secret = _parse_client_credentials(keys_path)
    done_event, auth_codes, callback_server = _start_callback_server()

    auth_url = _build_auth_url(client_id, scopes)
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
# Idempotent setup handler
# ---------------------------------------------------------------------------


def _workspace_chrome_profiles(source_group: str) -> set[str]:
    """Return the set of chrome profiles attached to a workspace's MCP servers."""
    from pynchy.config import get_settings

    s = get_settings()
    ws = s.workspaces.get(source_group)
    if not ws or not ws.mcp_servers:
        return set()

    profiles: set[str] = set()
    for entry in ws.mcp_servers:
        if "." in entry:
            _, inst_name = entry.split(".", 1)
            if inst_name in s.chrome_profiles:
                profiles.add(inst_name)
    return profiles


async def _handle_setup_google(data: dict) -> dict:
    """Idempotent Google setup for a chrome profile.

    Checks state and does only what's missing:
    1. GCP project exists? → skip creation if so
    2. Required APIs enabled? → enable any missing ones (REST first)
    3. OAuth client credentials exist? → skip consent screen setup if so
    4. Tokens exist and valid? → skip OAuth if so
    """
    from playwright.async_api import async_playwright

    profile_name = data.get("chrome_profile")
    if not profile_name:
        return {"error": "chrome_profile is required"}

    # Workspace access control: non-admin workspaces can only set up
    # profiles attached to their MCP servers.
    source_group = data.get("source_group")
    if source_group:
        from pynchy.config import get_settings

        ws = get_settings().workspaces.get(source_group)
        is_admin = ws.is_admin if ws else False
        if not is_admin:
            allowed = _workspace_chrome_profiles(source_group)
            if profile_name not in allowed:
                return {
                    "error": (
                        f"Workspace '{source_group}' does not have access to "
                        f"chrome profile '{profile_name}'. "
                        f"Available profiles: {sorted(allowed) or 'none'}"
                    )
                }

    kp = _keys_path(profile_name)
    cp = _credentials_path(profile_name)
    scopes, api_ids = _compute_scopes_for_profile(profile_name)

    # If no services reference this profile, use default scopes
    if not api_ids:
        logger.info(
            "No services reference this chrome profile, using default gdrive scopes",
            profile=profile_name,
        )
        api_ids = ["drive.googleapis.com"]
        scopes = " ".join(
            sorted(
                [
                    "https://www.googleapis.com/auth/drive.readonly",
                    _SERVICE_MANAGEMENT_SCOPE,
                ]
            )
        )

    steps_done: list[str] = []

    # --- Check if we can skip everything ---
    if kp.exists() and cp.exists():
        # Try refreshing the token to see if credentials are still valid
        access_token = _refresh_access_token(profile_name)
        if access_token:
            # Try enabling any missing APIs via REST
            project_number = _get_project_number(kp)
            if project_number:
                for api_id in api_ids:
                    _enable_api_via_rest(project_number, access_token, api_id)
                steps_done.append("APIs verified/enabled via REST")

            return {
                "result": {
                    "status": "already_configured",
                    "message": (
                        f"Google setup for profile '{profile_name}' is already "
                        f"configured. Tokens are valid."
                    ),
                    "steps": steps_done,
                }
            }

    # --- Need interactive setup ---
    project_id = data.get("project_id") or _read_project_id(kp) or _DEFAULT_PROJECT_ID
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

            # 1. Ensure GCP project
            await _ensure_project(page, project_id)
            steps_done.append(f"GCP project '{project_id}' ready")

            # 2. Enable required APIs
            for api_id in api_ids:
                await _ensure_api(page, project_id, api_id)
                steps_done.append(f"API '{api_id}' enabled")

            # 3. Ensure OAuth consent + credentials
            if not kp.exists():
                await _ensure_consent_screen(page, project_id)
                creds_path = await _create_oauth_credentials(page, project_id)

                # Copy to chrome profile directory
                dest = _keys_path(profile_name)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(creds_path, dest)
                steps_done.append("OAuth credentials created")

                # Clean up temp download dir
                dl = _download_dir()
                if dl.exists():
                    shutil.rmtree(dl, ignore_errors=True)
            else:
                steps_done.append("OAuth credentials already exist")

            # 4. Run OAuth flow
            tokens = await _run_oauth_flow(page, _keys_path(profile_name), scopes)
            _save_credentials_to_profile(tokens, profile_name)
            steps_done.append("OAuth tokens obtained")

            await context.close()

        result: dict[str, Any] = {
            "status": "ok",
            "message": f"Google setup complete for profile '{profile_name}'",
            "steps": steps_done,
            "keys_path": str(_keys_path(profile_name)),
        }
        if novnc_url:
            result["novnc_url"] = novnc_url
        return {"result": result}

    except Exception as exc:
        logger.error("setup_google failed", profile=profile_name, error=str(exc))
        return {"error": str(exc)}

    finally:
        stop_procs(vnc_procs)
        if original_display is not None:
            os.environ["DISPLAY"] = original_display
        elif "DISPLAY" in os.environ and vnc_procs:
            del os.environ["DISPLAY"]


# ---------------------------------------------------------------------------
# Plugin classes
# ---------------------------------------------------------------------------


class GoogleMcpPlugin:
    """Base MCP specs for Google services (gdrive, gcal).

    These are templates — they exist only to be inherited by config
    instances (e.g., ``[mcp_servers.gdrive.mycompany]``).  If no instances
    are declared, the template sits unused.
    """

    @hookimpl
    def pynchy_mcp_server_spec(self) -> list[dict]:
        return [
            {
                "name": "gdrive",
                "type": "docker",
                "image": "pynchy-mcp-gdrive:latest",
                "dockerfile": "container/mcp/gdrive.Dockerfile",
                "port": 3100,
                "transport": "streamable_http",
                "env": {"GDRIVE_OAUTH_PATH": "/home/chrome/gcp-oauth.keys.json"},
            },
            {
                "name": "gcal",
                "type": "docker",
                "image": "pynchy-mcp-gcal:latest",
                "dockerfile": "container/mcp/gcal.Dockerfile",
                "port": 3200,
                "transport": "streamable_http",
            },
        ]


class GoogleSetupPlugin:
    """Host-side handlers for Google OAuth setup.

    Registers one ``setup_google_{profile}`` handler per chrome profile
    defined in config.toml.  Each handler is a closure that injects the
    profile name into the request data before calling the shared handler.
    """

    @hookimpl
    def pynchy_service_handler(self) -> dict[str, Any]:
        from pynchy.config import get_settings

        tools: dict[str, Any] = {}
        for profile in get_settings().chrome_profiles:
            # Closure captures profile by value via default arg
            async def _handler(data: dict, _profile: str = profile) -> dict:
                data["chrome_profile"] = _profile
                return await _handle_setup_google(data)

            tools[f"setup_google_{profile}"] = _handler

        return {"tools": tools}
