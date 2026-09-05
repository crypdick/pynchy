"""GCP Console browser-automation steps for Google Setup.

Each step attempts Playwright automation first and falls back to printed
instructions + noVNC if selectors fail (Google changes their UI often).
"""

from __future__ import annotations

import contextlib
import json
import re
import tempfile
import time
from collections.abc import (  # noqa: TC003 - beartype resolves these runtime annotations.
    Awaitable,
    Callable,
)
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.parse import urlparse

from pynchy.logger import logger
from pynchy.plugins.integrations.google_setup._paths import GCP_CONSOLE, download_dir

COULD_NOT_DETECT_CREDENTIAL_DOWNLOAD_ERROR = (
    "Could not detect credential JSON download. "
    "Download it manually and place at "
    "data/chrome-profiles/<profile>/gcp-oauth.keys.json"
)
CREDENTIALS_FILE_NOT_FOUND_ERROR = "Credentials file not found at {path}"
INVALID_CREDENTIALS_JSON_ERROR = "Invalid credentials JSON — missing 'installed' or 'web' key"


@runtime_checkable
class ConsoleControl(Protocol):
    @property
    def first(self) -> ConsoleControl: ...

    def nth(self, index: int) -> ConsoleControl: ...

    async def click(self) -> object: ...

    async def clear(self) -> object: ...

    async def fill(self, value: str) -> object: ...

    async def count(self) -> int: ...

    async def is_visible(self, **kwargs: object) -> bool: ...


class ConsoleDownload(Protocol):
    async def save_as(self, path: str) -> object: ...


class ConsoleDownloadInfo(Protocol):
    value: Awaitable[ConsoleDownload]


@runtime_checkable
class ConsolePage(Protocol):
    """Browser surface used by Google Console automation."""

    @property
    def url(self) -> str: ...

    async def goto(self, url: str, *, wait_until: str) -> object: ...  # noqa: V107

    async def wait_for_timeout(self, milliseconds: int) -> object: ...

    async def wait_for_url(self, predicate: Callable[[str], bool], **kwargs: object) -> object: ...  # noqa: V107

    async def text_content(self, selector: str) -> str | None: ...

    async def screenshot(self, *, path: str) -> object: ...

    def get_by_role(self, role: str, *, name: object | None = None) -> ConsoleControl: ...

    def get_by_text(self, text: str, **kwargs: object) -> ConsoleControl: ...

    def locator(self, selector: str) -> ConsoleControl: ...

    def expect_download(
        self, **kwargs: object
    ) -> contextlib.AbstractAsyncContextManager[ConsoleDownloadInfo]: ...


# ---------------------------------------------------------------------------
# GCP Console helpers
# ---------------------------------------------------------------------------


async def dismiss_modals(page: ConsolePage) -> None:
    """Try to dismiss common GCP Console popups/modals."""
    for text in ("Got it", "Dismiss", "No thanks", "Skip", "Not now"):
        with contextlib.suppress(Exception):
            btn = page.get_by_role("button", name=re.compile(text, re.I)).first
            if await btn.is_visible(timeout=500):
                await btn.click()
                await page.wait_for_timeout(300)
    with contextlib.suppress(Exception):
        close = page.locator('[aria-label="Close"]').first
        if await close.is_visible(timeout=500):
            await close.click()


async def wait_for_login(page: ConsolePage) -> None:
    """Wait until Google login is complete (if a login page appeared)."""
    if urlparse(page.url).hostname == "accounts.google.com":
        logger.info("Waiting for Google login via noVNC")
        await page.wait_for_url(
            lambda url: urlparse(url).hostname != "accounts.google.com",
            timeout=300_000,  # 5 minutes
        )
        logger.info("Google login complete")
        await page.wait_for_timeout(2000)


async def try_step(
    page: ConsolePage,
    step_fn: Callable[[ConsolePage], Awaitable[None]],
    fallback_msg: str,
    done_check: Callable[[ConsolePage], Awaitable[bool]],
    manual_step_timeout_seconds: int = 60,
) -> None:
    """Attempt an automated Console step; use the manual + noVNC path when needed."""
    try:
        await step_fn(page)
    except Exception as exc:  # noqa: BLE001 - UI automation can require manual noVNC completion.
        logger.warning("GCP automation step failed; using manual path", error=str(exc))
        with contextlib.suppress(Exception):
            debug_screenshot = Path(tempfile.gettempdir()) / "gdrive-setup-debug.png"
            await page.screenshot(path=str(debug_screenshot))
    else:
        return

    logger.info("Manual step required", instructions=fallback_msg)

    deadline = time.time() + manual_step_timeout_seconds
    while time.time() < deadline:
        with contextlib.suppress(Exception):
            if await done_check(page):
                logger.info("Manual step completed")
                return
        await page.wait_for_timeout(5000)
    logger.warning(
        "Timed out waiting for manual step",
        manual_step_timeout_seconds=manual_step_timeout_seconds,
    )


# ---------------------------------------------------------------------------
# GCP Console steps
# ---------------------------------------------------------------------------


async def ensure_project(page: ConsolePage, project_id: str) -> None:
    """Create a GCP project (or verify it exists)."""
    logger.info("Ensuring GCP project exists", project_id=project_id)

    await page.goto(
        f"{GCP_CONSOLE}/home/dashboard?project={project_id}",
        wait_until="domcontentloaded",
    )
    await page.wait_for_timeout(8000)
    await wait_for_login(page)
    await dismiss_modals(page)

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
    await page.goto(f"{GCP_CONSOLE}/projectcreate", wait_until="domcontentloaded")
    await page.wait_for_timeout(5000)
    await dismiss_modals(page)

    async def _automate(p: ConsolePage) -> None:
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
            f"{GCP_CONSOLE}/home/dashboard?project={project_id}",
            wait_until="domcontentloaded",
        )

    async def _project_exists(p: ConsolePage) -> bool:
        if project_id not in p.url:
            return False
        body = await p.text_content("body") or ""
        return "doesn't exist" not in body.lower()

    await try_step(
        page,
        _automate,
        f'Create a new project named "{project_id}" and wait for it to finish.',
        done_check=_project_exists,
    )
    logger.info("GCP project ready", project_id=project_id)


async def ensure_api(page: ConsolePage, project_id: str, api_id: str) -> None:
    """Enable a Google API for the project."""
    logger.info("Enabling Google API", project_id=project_id, api=api_id)

    await page.goto(
        f"{GCP_CONSOLE}/apis/library/{api_id}?project={project_id}",
        wait_until="domcontentloaded",
    )
    await page.wait_for_timeout(5000)
    await dismiss_modals(page)

    enable_btn = page.get_by_role("button", name=re.compile(r"^enable$", re.I))
    if await enable_btn.count() == 0:
        logger.info("API already enabled (no Enable button found)", api=api_id)
        return

    async def _automate(p: ConsolePage) -> None:
        btn = p.get_by_role("button", name=re.compile(r"^enable$", re.I))
        await btn.click()
        await p.wait_for_timeout(5000)

    async def _api_enabled(p: ConsolePage) -> bool:
        btn = p.get_by_role("button", name=re.compile(r"^enable$", re.I))
        return bool(await btn.count() == 0)

    await try_step(
        page,
        _automate,
        f'Click the "Enable" button for {api_id}.',
        done_check=_api_enabled,
    )
    logger.info("API enabled", api=api_id)


async def ensure_consent_screen(page: ConsolePage, project_id: str) -> None:
    """Configure OAuth consent screen (External, Testing mode)."""
    logger.info("Configuring OAuth consent screen", project_id=project_id)

    await page.goto(
        f"{GCP_CONSOLE}/apis/credentials/consent?project={project_id}",
        wait_until="domcontentloaded",
    )
    await dismiss_modals(page)
    await page.wait_for_timeout(5000)

    body = await page.text_content("body") or ""
    if "edit app" in body.lower() or "publishing status" in body.lower():
        logger.info("OAuth consent screen already configured")
        return

    async def _automate(p: ConsolePage) -> None:
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

    async def _consent_configured(p: ConsolePage) -> bool:
        body = await p.text_content("body") or ""
        return "edit app" in body.lower() or "publishing status" in body.lower()

    await try_step(
        page,
        _automate,
        (
            "Configure the OAuth consent screen:\n"
            '  1. Select "External" and click Create\n'
            '  2. Fill in App name ("pynchy-gdrive"), support email, dev email\n'
            '  3. Click "Save and Continue" through all pages (skip scopes/test users)'
        ),
        done_check=_consent_configured,
        manual_step_timeout_seconds=180,
    )
    logger.info("OAuth consent screen configured")


async def create_oauth_credentials(page: ConsolePage, project_id: str) -> Path:
    """Create Desktop App OAuth credentials and download the JSON."""
    logger.info("Creating OAuth Desktop App credentials", project_id=project_id)

    dl_dir = download_dir()

    await page.goto(
        f"{GCP_CONSOLE}/apis/credentials/oauthclient?project={project_id}",
        wait_until="domcontentloaded",
    )
    await dismiss_modals(page)
    await page.wait_for_timeout(2000)

    dest = dl_dir / "gcp-oauth.keys.json"

    async def _automate(p: ConsolePage) -> None:
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
    except Exception as exc:  # noqa: BLE001 - credential download automation falls back to manual steps.
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
        except Exception as exc:  # missing download remains a manual credential-creation path.
            raise RuntimeError(COULD_NOT_DETECT_CREDENTIAL_DOWNLOAD_ERROR) from exc

    if not dest.exists():
        raise RuntimeError(CREDENTIALS_FILE_NOT_FOUND_ERROR.format(path=dest))

    with dest.open(encoding="utf-8") as f:
        data = json.load(f)
    if "installed" not in data and "web" not in data:
        raise RuntimeError(INVALID_CREDENTIALS_JSON_ERROR)

    logger.info("OAuth credentials saved", path=str(dest))
    return dest
