"""Hermetic public-action coverage for Google profile provisioning."""

from __future__ import annotations

import asyncio
import os
import sys
import types
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from pynchy.plugins.integrations.google_setup import (
    GoogleSetupPlugin,
    GoogleSetupRuntime,
    configure_google_setup_runtime,
)

if TYPE_CHECKING:
    from collections.abc import Callable


class _Download:
    def __init__(self, contents: bytes, *, saves_file: bool) -> None:
        self._contents = contents
        self._saves_file = saves_file

    async def save_as(self, path: str) -> None:
        if self._saves_file:
            await asyncio.to_thread(Path(path).write_bytes, self._contents)


class _DownloadInfo:
    def __init__(self, download: _Download) -> None:
        self.value = asyncio.get_running_loop().create_future()
        self.value.set_result(download)

    async def __aenter__(self) -> _DownloadInfo:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Control:
    def __init__(self, page: _SetupPage, name: str) -> None:
        self._page = page
        self._name = name

    @property
    def first(self) -> _Control:
        return self

    async def clear(self) -> None:
        self._page.calls.append(("clear", self._name))

    async def click(self) -> None:
        self._page.calls.append(("click", self._name))
        if self._name == "oauth-type" and self._page.fail_automated_download:
            raise RuntimeError("selector changed")
        if self._name == "external" and self._page.fail_consent_creation:
            self._page.consent_configured = True
            raise RuntimeError("consent selector changed")
        if self._name == "textbox" and self._page.manual_project:
            raise RuntimeError("project selector changed")
        if (
            self._name == "create"
            and self._page.phase == "project-create"
            and not self._page.project_confirmation
            and not self._page.project_stalls
        ):
            self._page.url = "https://console.cloud.google.com/home/dashboard?project=pynchy-gdrive"
        if self._name == "enable":
            self._page.api_enabled = False
            if self._page.fail_api_enable:
                raise RuntimeError("enable selector changed")
        if self._name == "save-and-continue":
            self._page.save_steps -= 1

    async def count(self) -> int:
        if self._name == "enable":
            return int(self._page.api_enabled)
        if self._name == "save-and-continue":
            return int(self._page.save_steps > 0)
        if self._name == "textbox":
            if self._page.phase == "consent":
                return self._page.consent_textbox_count
            if self._page.phase == "oauth-client":
                return self._page.oauth_textbox_count
            return 1
        return 1

    async def fill(self, value: str) -> None:
        self._page.calls.append(("fill", f"{self._name}:{value}"))

    async def is_visible(self, **_kwargs: object) -> bool:
        return self._page.modal_visible and self._name in {"close", "modal"}

    def nth(self, _index: int) -> _Control:
        return self


class _SetupPage:
    def __init__(
        self,
        *,
        credential_contents: bytes,
        api_already_enabled: bool = False,
        consent_configured: bool = False,
        download_fails: bool = False,
        download_saves: bool = True,
        fail_automated_download: bool = False,
        fail_api_enable: bool = False,
        fail_consent_creation: bool = False,
        manual_project: bool = False,
        modal_visible: bool = False,
        project_confirmation: bool = False,
        project_stalls: bool = False,
        project_exists: bool = False,
        requires_login: bool = False,
        login_return_url: str = "https://console.cloud.google.com/home",
        save_steps: int = 0,
        consent_textbox_count: int = 2,
        oauth_textbox_count: int = 1,
        timeout_before_manual_check: bool = False,
    ) -> None:
        self.api_already_enabled = api_already_enabled
        self.api_enabled = True
        self.calls: list[tuple[str, str]] = []
        self.consent_configured = consent_configured
        self.credential_contents = credential_contents
        self.download_fails = download_fails
        self.download_saves = download_saves
        self.fail_automated_download = fail_automated_download
        self.fail_api_enable = fail_api_enable
        self.fail_consent_creation = fail_consent_creation
        self.manual_project = manual_project
        self.manual_project_waits = 0
        self.modal_visible = modal_visible
        self.phase = ""
        self.project_confirmation = project_confirmation
        self.project_stalls = project_stalls
        self.project_exists = project_exists
        self.requires_login = requires_login
        self.login_return_url = login_return_url
        self.save_steps = save_steps
        self.consent_textbox_count = consent_textbox_count
        self.oauth_textbox_count = oauth_textbox_count
        self.timeout_before_manual_check = timeout_before_manual_check
        self.screenshoted = False
        self.url = "about:blank"

    def expect_download(self, **_kwargs: object) -> _DownloadInfo:
        if self.download_fails:
            raise RuntimeError("download unavailable")
        return _DownloadInfo(_Download(self.credential_contents, saves_file=self.download_saves))

    def get_by_role(self, role: str, *, name: object | None = None) -> _Control:
        if role == "textbox":
            return _Control(self, "textbox")
        pattern = str(getattr(name, "pattern", "")).lower()
        if pattern == "^enable$":
            return _Control(self, "enable")
        if pattern == "^create$":
            return _Control(self, "create")
        if "save and continue" in pattern:
            return _Control(self, "save-and-continue")
        if "download" in pattern:
            return _Control(self, "download-json")
        return _Control(self, "modal")

    def get_by_text(self, text: str, **_kwargs: object) -> _Control:
        return _Control(self, "external" if text == "External" else "desktop-app")

    def locator(self, selector: str) -> _Control:
        return _Control(self, "oauth-type" if "mat-select" in selector else "close")

    async def goto(self, url: str, *, wait_until: str) -> None:
        assert wait_until == "domcontentloaded"
        self.calls.append(("goto", url))
        self.url = url
        if "projectcreate" in url:
            self.phase = "project-create"
        elif "/apis/library/" in url:
            self.phase = "api"
            self.api_enabled = not self.api_already_enabled
        elif "/consent" in url:
            self.phase = "consent"
        elif "/oauthclient" in url:
            self.phase = "oauth-client"
        elif "/dashboard" in url:
            if self.project_stalls and self.phase == "project-create":
                self.project_exists = True
            self.phase = "dashboard"
        else:
            self.phase = "console"
            if self.requires_login:
                self.url = "https://accounts.google.com/signin"

    async def screenshot(self, **_kwargs: object) -> None:
        self.screenshoted = True

    async def text_content(self, _selector: str) -> str:
        if self.phase == "dashboard":
            return "" if self.project_exists else "This project doesn't exist"
        if self.phase == "project-create" and self.project_confirmation:
            return "Project has been created"
        if self.phase == "consent" and self.consent_configured:
            return "Edit app"
        return ""

    async def wait_for_timeout(self, milliseconds: int) -> None:
        if milliseconds == 5000 and self.manual_project and self.phase == "project-create":
            self.manual_project_waits += 1
            if self.manual_project_waits == 2:
                self.project_exists = True
                self.url = "https://console.cloud.google.com/home/dashboard?project=pynchy-gdrive"

    async def wait_for_url(self, predicate: Callable[[str], bool], **_kwargs: object) -> None:
        self.url = self.login_return_url
        assert predicate(self.url)


class _BrowserContext:
    def __init__(self, page: _SetupPage) -> None:
        self.closed = False
        self.default_timeout: int | None = None
        self.navigation_timeout: int | None = None
        self.pages = [page]

    async def close(self) -> None:
        self.closed = True

    async def new_page(self) -> _SetupPage:
        return self.pages[0]

    def set_default_navigation_timeout(self, timeout: int) -> None:
        self.navigation_timeout = timeout

    def set_default_timeout(self, timeout: int) -> None:
        self.default_timeout = timeout


class _Playwright:
    def __init__(self, context: _BrowserContext) -> None:
        self.chromium = self
        self._context = context

    async def __aenter__(self) -> _Playwright:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def launch_persistent_context(self, **_kwargs: object) -> _BrowserContext:
        return self._context


def _configure_runtime(tmp_path: Path, *, assigned: bool = True) -> None:
    configure_google_setup_runtime(
        GoogleSetupRuntime(
            data_dir=tmp_path,
            chrome_profiles=frozenset({"personal"}),
            workspace_names=("assigned", "unassigned"),
            workspace_tools=(
                lambda workspace: (
                    ("gdrive.personal",) if assigned and workspace == "assigned" else ()
                )
            ),
            workspace_is_admin=lambda _workspace: False,
            mcp_tool_names=frozenset({"gdrive.personal", "gcal.personal"}),
        )
    )


def _handler() -> Callable[[dict[str, Any]], Any]:
    action = (
        GoogleSetupPlugin(("personal",))
        .pynchy_service_handler()
        .action_for("setup_google_personal")
    )
    assert action is not None
    return action.handler


def _install_playwright(monkeypatch: pytest.MonkeyPatch, page: _SetupPage) -> _BrowserContext:
    context = _BrowserContext(page)
    async_api = types.ModuleType("playwright.async_api")
    async_api.async_playwright = lambda: _Playwright(context)
    playwright = types.ModuleType("playwright")
    playwright.async_api = async_api
    monkeypatch.setitem(sys.modules, "playwright", playwright)
    monkeypatch.setitem(sys.modules, "playwright.async_api", async_api)
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._handler.chrome_path", lambda: "/mock/chrome"
    )
    return context


def _stub_oauth(monkeypatch: pytest.MonkeyPatch, scopes: list[str]) -> None:
    async def run_oauth_flow(
        _page: object, _keys_path: Path, requested_scopes: str
    ) -> dict[str, object]:
        await asyncio.sleep(0)
        scopes.append(requested_scopes)
        return {"refresh_token": "fresh-token"}

    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._handler.run_oauth_flow", run_oauth_flow
    )


@pytest.mark.action("integration.google.profile.setup")
@pytest.mark.asyncio
async def test_setup_action_provisions_project_api_credentials_and_oauth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_runtime(tmp_path)
    page = _SetupPage(
        credential_contents=b'{"installed": {"client_id": "123.apps.googleusercontent.com"}}'
    )
    context = _install_playwright(monkeypatch, page)
    scopes: list[str] = []
    _stub_oauth(monkeypatch, scopes)
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._handler.has_display", lambda: True
    )

    result = await _handler()({"source_group": "assigned"})

    assert result["result"]["status"] == "ok"
    assert result["result"]["steps"] == [
        "GCP project 'pynchy-gdrive' ready",
        "API 'drive.googleapis.com' enabled",
        "OAuth credentials created",
        "OAuth tokens obtained",
    ]
    assert context.closed is True
    assert context.navigation_timeout == 60_000
    assert context.default_timeout == 15_000
    assert "https://www.googleapis.com/auth/drive.readonly" in scopes[0]
    assert "https://www.googleapis.com/auth/service.management" in scopes[0]
    assert ("click", "enable") in page.calls
    assert ("click", "desktop-app") in page.calls


@pytest.mark.action("integration.google.profile.setup")
@pytest.mark.asyncio
async def test_setup_action_accepts_manual_credential_download_after_selector_breakage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_runtime(tmp_path)
    page = _SetupPage(
        credential_contents=b'{"web": {"client_id": "123.apps.googleusercontent.com"}}',
        fail_automated_download=True,
    )
    _install_playwright(monkeypatch, page)
    _stub_oauth(monkeypatch, [])
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._handler.has_display", lambda: True
    )

    result = await _handler()({"source_group": "assigned"})

    assert result["result"]["status"] == "ok"
    assert ("click", "oauth-type") in page.calls


@pytest.mark.action("integration.google.profile.setup")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("page_options", "credential_contents", "error"),
    [
        pytest.param(
            {"manual_project": True, "modal_visible": True},
            b'{"installed": {"client_id": "123.apps.googleusercontent.com"}}',
            None,
            id="manual-project-completion",
        ),
        pytest.param(
            {
                "modal_visible": True,
                "project_confirmation": True,
                "requires_login": True,
                "save_steps": 1,
            },
            b'{"installed": {"client_id": "123.apps.googleusercontent.com"}}',
            None,
            id="console-confirmation",
        ),
        pytest.param(
            {"project_stalls": True},
            b'{"installed": {"client_id": "123.apps.googleusercontent.com"}}',
            None,
            id="project-poll-fallback",
        ),
        pytest.param(
            {
                "api_already_enabled": True,
                "consent_configured": True,
                "project_exists": True,
            },
            b'{"installed": {"client_id": "123.apps.googleusercontent.com"}}',
            None,
            id="existing-console-assets",
        ),
        pytest.param(
            {"download_fails": True, "fail_automated_download": True},
            b'{"installed": {"client_id": "123.apps.googleusercontent.com"}}',
            "Could not detect credential JSON download",
            id="missing-manual-download",
        ),
        pytest.param(
            {"download_saves": False},
            b'{"installed": {"client_id": "123.apps.googleusercontent.com"}}',
            "Credentials file not found",
            id="reported-download-file-missing",
        ),
        pytest.param(
            {},
            b"{}",
            "Invalid credentials JSON",
            id="invalid-downloaded-credentials",
        ),
        pytest.param(
            {"project_confirmation": True, "requires_login": True, "save_steps": 4},
            b'{"installed": {"client_id": "123.apps.googleusercontent.com"}}',
            None,
            id="consent-save-loop-exhaustion",
        ),
        pytest.param(
            {"consent_textbox_count": 0},
            b'{"installed": {"client_id": "123.apps.googleusercontent.com"}}',
            None,
            id="consent-without-textboxes",
        ),
        pytest.param(
            {"consent_textbox_count": 1},
            b'{"installed": {"client_id": "123.apps.googleusercontent.com"}}',
            None,
            id="consent-with-one-textbox",
        ),
        pytest.param(
            {"oauth_textbox_count": 0},
            b'{"installed": {"client_id": "123.apps.googleusercontent.com"}}',
            None,
            id="oauth-dialog-without-name-field",
        ),
        pytest.param(
            {"manual_project": True, "timeout_before_manual_check": True},
            b'{"installed": {"client_id": "123.apps.googleusercontent.com"}}',
            None,
            id="manual-step-already-expired",
        ),
    ],
)
async def test_setup_action_handles_console_recovery_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    page_options: dict[str, bool | int],
    credential_contents: bytes,
    error: str | None,
) -> None:
    _configure_runtime(tmp_path)
    page = _SetupPage(credential_contents=credential_contents, **page_options)
    _install_playwright(monkeypatch, page)
    _stub_oauth(monkeypatch, [])
    if page_options.get("timeout_before_manual_check"):
        times = iter((1000.0, 2000.0))
        monkeypatch.setattr(
            "pynchy.plugins.integrations.google_setup._console.time.time", times.__next__
        )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._handler.has_display", lambda: True
    )

    result = await _handler()({"source_group": "assigned"})

    if error is None:
        assert result["result"]["status"] == "ok"
    else:
        assert error in str(result["error"])
    if page_options.get("manual_project"):
        assert page.screenshoted is True


@pytest.mark.action("integration.google.profile.setup")
@pytest.mark.asyncio
async def test_setup_action_accepts_login_completion_url_with_google_in_query(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_runtime(tmp_path)
    page = _SetupPage(
        credential_contents=b'{"installed": {"client_id": "123.apps.googleusercontent.com"}}',
        requires_login=True,
        login_return_url="https://console.cloud.google.com/home?from=accounts.google.com",
    )
    _install_playwright(monkeypatch, page)
    _stub_oauth(monkeypatch, [])
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._handler.has_display", lambda: True
    )

    assert (await _handler()({"source_group": "assigned"}))["result"]["status"] == "ok"


@pytest.mark.action("integration.google.profile.setup")
@pytest.mark.asyncio
async def test_setup_action_recovers_from_stale_key_and_token_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_runtime(tmp_path)
    profile_dir = tmp_path / "chrome-profiles" / "personal"
    profile_dir.mkdir(parents=True)
    (profile_dir / "gcp-oauth.keys.json").write_text("{")
    (profile_dir / "credentials.json").write_text("{")
    page = _SetupPage(
        credential_contents=b'{"installed": {"client_id": "123.apps.googleusercontent.com"}}'
    )
    _install_playwright(monkeypatch, page)
    _stub_oauth(monkeypatch, [])
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._handler.has_display", lambda: True
    )

    result = await _handler()({"source_group": "assigned"})

    assert result["result"]["status"] == "ok"
    assert result["result"]["steps"][2] == "OAuth credentials already exist"


@pytest.mark.action("integration.google.profile.setup")
@pytest.mark.asyncio
async def test_setup_action_uses_default_scopes_without_a_workspace_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_runtime(tmp_path, assigned=False)
    page = _SetupPage(
        credential_contents=b'{"installed": {"client_id": "123.apps.googleusercontent.com"}}'
    )
    _install_playwright(monkeypatch, page)
    scopes: list[str] = []
    _stub_oauth(monkeypatch, scopes)
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._handler.has_display", lambda: True
    )

    result = await _handler()({})

    assert result["result"]["status"] == "ok"
    assert scopes == [
        (
            "https://www.googleapis.com/auth/drive.readonly "
            "https://www.googleapis.com/auth/service.management"
        )
    ]


@pytest.mark.action("integration.google.profile.setup")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "page_options", [{"fail_api_enable": True}, {"fail_consent_creation": True}]
)
async def test_setup_action_recovers_when_console_state_confirms_failed_ui_step(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    page_options: dict[str, bool],
) -> None:
    _configure_runtime(tmp_path)
    page = _SetupPage(
        credential_contents=b'{"installed": {"client_id": "123.apps.googleusercontent.com"}}',
        **page_options,
    )
    _install_playwright(monkeypatch, page)
    _stub_oauth(monkeypatch, [])
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._handler.has_display", lambda: True
    )

    result = await _handler()({"source_group": "assigned"})

    assert result["result"]["status"] == "ok"


@pytest.mark.action("integration.google.profile.setup")
@pytest.mark.asyncio
async def test_setup_action_cleans_virtual_display_created_during_setup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_runtime(tmp_path)
    monkeypatch.delenv("DISPLAY", raising=False)
    stopped: list[list[object]] = []

    def start_virtual_display() -> tuple[list[object], str]:
        monkeypatch.setenv("DISPLAY", ":99")
        return [object()], "http://novnc.local/google"

    def stop_procs(procs: list[object]) -> None:
        stopped.append(procs)

    async def run_interactive_setup_body(*_args: object) -> dict[str, object]:
        await asyncio.sleep(0)
        return {"result": {"status": "ok"}}

    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._handler.has_display", lambda: False
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._handler.start_virtual_display",
        start_virtual_display,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._handler.stop_procs",
        stop_procs,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._handler._run_interactive_setup_body",
        run_interactive_setup_body,
    )

    assert await _handler()({}) == {"result": {"status": "ok"}}
    assert len(stopped) == 1
    assert "DISPLAY" not in os.environ


@pytest.mark.action("integration.google.profile.setup")
@pytest.mark.asyncio
@pytest.mark.parametrize("stored_credentials", ["{", "{}"])
async def test_setup_action_recovers_from_unusable_stored_tokens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stored_credentials: str
) -> None:
    _configure_runtime(tmp_path)
    profile_dir = tmp_path / "chrome-profiles" / "personal"
    profile_dir.mkdir(parents=True)
    (profile_dir / "gcp-oauth.keys.json").write_text(
        '{"installed": {"client_id": "123456-client.apps.googleusercontent.com", '
        '"client_secret": "client-secret"}}'  # pragma: allowlist secret
    )
    (profile_dir / "credentials.json").write_text(stored_credentials)
    page = _SetupPage(
        credential_contents=b'{"installed": {"client_id": "123.apps.googleusercontent.com"}}'
    )
    _install_playwright(monkeypatch, page)
    _stub_oauth(monkeypatch, [])
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._handler.has_display", lambda: True
    )

    result = await _handler()({"source_group": "assigned"})

    assert result["result"]["status"] == "ok"
    assert result["result"]["steps"][2] == "OAuth credentials already exist"
