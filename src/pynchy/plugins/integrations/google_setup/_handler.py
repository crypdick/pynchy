"""Idempotent Google setup handler — the ``setup_google`` service handler."""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any

from pynchy.logger import logger
from pynchy.plugins.integrations.browser import (
    chrome_path,
    has_display,
    profile_dir,
    start_virtual_display,
    stop_procs,
)
from pynchy.plugins.integrations.google_setup._console import (
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
    keys_path,
    workspace_chrome_profiles,
)
from pynchy.plugins.integrations.google_setup._rest_api import (
    enable_api_via_rest,
    get_project_number,
    read_project_id,
    refresh_access_token,
)


async def handle_setup_google(data: dict) -> dict:
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
        is_admin = bool(ws.is_admin) if ws else False
        if not is_admin:
            allowed = workspace_chrome_profiles(source_group)
            if profile_name not in allowed:
                return {
                    "error": (
                        f"Workspace '{source_group}' does not have access to "
                        f"chrome profile '{profile_name}'. "
                        f"Available profiles: {sorted(allowed) or 'none'}"
                    )
                }

    kp = keys_path(profile_name)
    cp = credentials_path(profile_name)
    scopes, api_ids = compute_scopes_for_profile(profile_name)

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
                    SERVICE_MANAGEMENT_SCOPE,
                ]
            )
        )

    steps_done: list[str] = []

    # --- Check if we can skip everything ---
    if kp.exists() and cp.exists():
        # Try refreshing the token to see if credentials are still valid
        access_token = refresh_access_token(profile_name)
        if access_token:
            # Try enabling any missing APIs via REST
            project_number = get_project_number(kp)
            if project_number:
                for api_id in api_ids:
                    enable_api_via_rest(project_number, access_token, api_id)
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
    project_id = data.get("project_id") or read_project_id(kp) or DEFAULT_PROJECT_ID
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
            await page.goto(GCP_CONSOLE, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)
            await wait_for_login(page)
            await dismiss_modals(page)

            # 1. Ensure GCP project
            await ensure_project(page, project_id)
            steps_done.append(f"GCP project '{project_id}' ready")

            # 2. Enable required APIs
            for api_id in api_ids:
                await ensure_api(page, project_id, api_id)
                steps_done.append(f"API '{api_id}' enabled")

            # 3. Ensure OAuth consent + credentials
            if not kp.exists():
                await ensure_consent_screen(page, project_id)
                creds_path = await create_oauth_credentials(page, project_id)

                # Copy to chrome profile directory
                dest = keys_path(profile_name)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(creds_path, dest)
                steps_done.append("OAuth credentials created")

                # Clean up temp download dir
                dl = download_dir()
                if dl.exists():
                    shutil.rmtree(dl, ignore_errors=True)
            else:
                steps_done.append("OAuth credentials already exist")

            # 4. Run OAuth flow
            tokens = await run_oauth_flow(page, keys_path(profile_name), scopes)
            save_credentials_to_profile(tokens, profile_name)
            steps_done.append("OAuth tokens obtained")

            await context.close()

        result: dict[str, Any] = {
            "status": "ok",
            "message": f"Google setup complete for profile '{profile_name}'",
            "steps": steps_done,
            "keys_path": str(keys_path(profile_name)),
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
