"""Idempotent Google setup handler — the ``setup_google`` service handler."""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

from pynchy.logger import logger
from pynchy.plugins.integrations.browser import (
    chrome_path,
    has_display,
    profile_dir,
    start_virtual_display,
    stop_procs,
)
from pynchy.plugins.integrations.google_setup._console import (
    ConsolePage,
    create_oauth_credentials,
    dismiss_modals,
    ensure_api,
    ensure_consent_screen,
    ensure_project,
    wait_for_login,
)
from pynchy.plugins.integrations.google_setup._oauth import (
    run_oauth_flow,
    save_credentials_to_profile,
)
from pynchy.plugins.integrations.google_setup._paths import (
    DEFAULT_PROJECT_ID,
    GCP_CONSOLE,
    SERVICE_MANAGEMENT_SCOPE,
    compute_scopes_for_profile,
    credentials_path,
    download_dir,
    google_setup_runtime,
    keys_path,
    workspace_chrome_profiles,
)
from pynchy.plugins.integrations.google_setup._rest_api import (
    enable_api_via_rest,
    get_project_number,
    read_project_id,
    refresh_access_token,
)

if TYPE_CHECKING:
    import subprocess

    from pynchy.plugins.integrations.google_setup._oauth import OAuthPage


@runtime_checkable
class _BrowserPage(Protocol):
    async def goto(self, url: str, *, wait_until: str) -> object: ...  # noqa: V107

    async def wait_for_timeout(self, _milliseconds: int) -> object: ...


@dataclass(frozen=True)
class _GoogleInteractiveSetup:
    profile_name: str
    project_id: str
    api_ids: list[str]
    scopes: str
    profile_dir: Path


def _check_workspace_access(
    profile_name: str,
    source_group: str | None,
) -> dict[str, object] | None:
    """Return an error dict if source_group lacks access to profile_name, else None.

    Non-admin workspaces can only set up profiles attached to their MCP servers.
    """
    if not source_group:
        return None

    if google_setup_runtime().workspace_is_admin(source_group):
        return None

    allowed = workspace_chrome_profiles(source_group)
    if profile_name in allowed:
        return None

    return {
        "error": (
            f"Workspace '{source_group}' does not have access to "
            f"chrome profile '{profile_name}'. "
            f"Available profiles: {sorted(allowed) or 'none'}"
        )
    }


def _resolve_scopes(profile_name: str) -> tuple[str, list[str]]:
    """Return (scopes, api_ids) for profile_name, falling back to default gdrive scopes."""
    scopes, api_ids = compute_scopes_for_profile(profile_name)
    if api_ids:
        return scopes, api_ids

    logger.info(
        "No services reference this chrome profile, using default gdrive scopes",
        profile=profile_name,
    )
    api_ids = ["drive.googleapis.com"]
    scopes = " ".join(
        sorted(["https://www.googleapis.com/auth/drive.readonly", SERVICE_MANAGEMENT_SCOPE])
    )
    return scopes, api_ids


def _try_fast_path(
    profile_name: str, kp: Path, cp: Path, api_ids: list[str]
) -> dict[str, object] | None:
    """Return an 'already_configured' result if valid tokens already exist, else None."""
    if not (kp.exists() and cp.exists()):
        return None

    access_token = refresh_access_token(profile_name)
    if not access_token:
        return None

    steps_done: list[str] = []
    project_number = get_project_number(kp)
    if project_number:
        if not all(enable_api_via_rest(project_number, access_token, api_id) for api_id in api_ids):
            return None
        steps_done.append("APIs verified/enabled via REST")

    return {
        "result": {
            "status": "already_configured",
            "message": (
                f"Google setup for profile '{profile_name}' is already configured. "
                f"Tokens are valid."
            ),
            "steps": steps_done,
        }
    }


async def _ensure_oauth_credentials(
    page: _BrowserPage,
    project_id: str,
    kp: Path,
    profile_name: str,
) -> str:
    """Ensure OAuth client credentials exist for profile_name, creating them if missing."""
    if await asyncio.to_thread(kp.exists):
        return "OAuth credentials already exist"

    console_page = cast("ConsolePage", page)
    await ensure_consent_screen(console_page, project_id)
    creds_path = await create_oauth_credentials(console_page, project_id)

    dest = keys_path(profile_name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(creds_path, dest)

    dl = download_dir()
    await asyncio.to_thread(shutil.rmtree, dl, ignore_errors=True)

    return "OAuth credentials created"


async def _run_interactive_setup(
    profile_name: str, kp: Path, api_ids: list[str], scopes: str, data: dict[str, object]
) -> dict[str, object]:
    """Drive the browser through GCP project/API/OAuth setup for profile_name."""
    steps_done: list[str] = []
    setup = _google_interactive_setup(profile_name, kp, api_ids, scopes, data)
    vnc_procs: list[subprocess.Popen[bytes]] = []
    original_display = os.environ.get("DISPLAY")
    novnc_url: str | None = None

    try:
        if not has_display():
            vnc_procs, novnc_url = start_virtual_display()
        return await _run_interactive_setup_body(setup, kp, steps_done, novnc_url)

    except Exception as exc:  # noqa: BLE001 - interactive setup failure is converted into a caller-facing error.
        logger.error("setup_google failed", profile=profile_name, error=str(exc))
        return _interactive_error_result(str(exc), novnc_url)

    finally:
        stop_procs(vnc_procs)
        if original_display is not None:
            os.environ["DISPLAY"] = original_display
        elif "DISPLAY" in os.environ and vnc_procs:
            del os.environ["DISPLAY"]


async def _run_interactive_setup_body(
    setup: _GoogleInteractiveSetup,
    kp: Path,
    steps_done: list[str],
    novnc_url: str | None,
) -> dict[str, object]:
    from playwright.async_api import (  # noqa: PLC0415 - optional browser automation dependency.
        async_playwright,
    )

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(setup.profile_dir),
            executable_path=chrome_path(),
            headless=False,
            accept_downloads=True,
            viewport={"width": 1280, "height": 720},
            timeout=60_000,
        )
        context.set_default_navigation_timeout(60_000)
        context.set_default_timeout(15_000)
        page = context.pages[0] if context.pages else await context.new_page()
        console_page = cast("ConsolePage", page)

        # Navigate to GCP Console (triggers login if needed)
        await page.goto(GCP_CONSOLE, wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)
        await wait_for_login(console_page)
        await dismiss_modals(console_page)

        # 1. Ensure GCP project
        await ensure_project(console_page, setup.project_id)
        steps_done.append(f"GCP project '{setup.project_id}' ready")

        # 2. Enable required APIs
        for api_id in setup.api_ids:
            await ensure_api(console_page, setup.project_id, api_id)
            steps_done.append(f"API '{api_id}' enabled")

        # 3. Ensure OAuth consent + credentials
        steps_done.append(
            await _ensure_oauth_credentials(
                cast("_BrowserPage", page),
                setup.project_id,
                kp,
                setup.profile_name,
            )
        )

        # 4. Run OAuth flow
        tokens = await run_oauth_flow(
            cast("OAuthPage", page),
            keys_path(setup.profile_name),
            setup.scopes,
        )
        save_credentials_to_profile(tokens, setup.profile_name)
        steps_done.append("OAuth tokens obtained")
        await context.close()

    return {"result": _interactive_success_result(setup, steps_done, novnc_url)}


def _google_interactive_setup(
    profile_name: str,
    kp: Path,
    api_ids: list[str],
    scopes: str,
    data: dict[str, object],
) -> _GoogleInteractiveSetup:
    raw_project_id = data.get("project_id")
    project_id = raw_project_id if isinstance(raw_project_id, str) else None
    return _GoogleInteractiveSetup(
        profile_name=profile_name,
        project_id=project_id or read_project_id(kp) or DEFAULT_PROJECT_ID,
        api_ids=api_ids,
        scopes=scopes,
        profile_dir=profile_dir("google"),
    )


def _interactive_success_result(
    setup: _GoogleInteractiveSetup,
    steps_done: list[str],
    novnc_url: str | None,
) -> dict[str, object]:
    return _with_optional_novnc_url(
        {
            "status": "ok",
            "message": f"Google setup complete for profile '{setup.profile_name}'",
            "steps": steps_done,
            "keys_path": str(keys_path(setup.profile_name)),
        },
        novnc_url,
    )


def _interactive_error_result(error: str, novnc_url: str | None) -> dict[str, object]:
    return _with_optional_novnc_url({"error": error}, novnc_url)


def _with_optional_novnc_url(
    payload: dict[str, object],
    novnc_url: str | None,
) -> dict[str, object]:
    if novnc_url is None:
        return payload
    return {**payload, "novnc_url": novnc_url}


async def handle_setup_google(data: dict[str, object]) -> dict[str, object]:
    """Idempotent Google setup for a chrome profile.

    Checks state and does only what's missing:
    1. GCP project exists? → skip creation if so
    2. Required APIs enabled? → enable any missing ones (REST first)
    3. OAuth client credentials exist? → skip consent screen setup if so
    4. Tokens exist and valid? → skip OAuth if so
    """
    profile_name = cast("str", data["chrome_profile"])

    raw_source_group = data.get("source_group")
    source_group = raw_source_group if isinstance(raw_source_group, str) else None
    access_error = _check_workspace_access(profile_name, source_group)
    if access_error:
        return access_error

    kp = keys_path(profile_name)
    cp = credentials_path(profile_name)
    scopes, api_ids = _resolve_scopes(profile_name)

    fast_path_result = _try_fast_path(profile_name, kp, cp, api_ids)
    if fast_path_result:
        return fast_path_result

    return await _run_interactive_setup(profile_name, kp, api_ids, scopes, data)
