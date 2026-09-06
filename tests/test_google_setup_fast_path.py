"""Public Google setup fast-path and workspace-access behavior."""

from __future__ import annotations

import asyncio
import io
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from pynchy.plugins.integrations.google_setup import (
    GoogleSetupPlugin,
    GoogleSetupRuntime,
    configure_google_setup_runtime,
)

if TYPE_CHECKING:
    from pathlib import Path


class _Response:
    def __init__(self, contents: bytes) -> None:
        self._contents = contents

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._contents


def _configure_runtime(
    tmp_path: Path,
    *,
    workspace_tools: tuple[str, ...] | None = ("gdrive.personal",),
    workspace_is_admin: bool = False,
    mcp_tool_names: frozenset[str] = frozenset({"gdrive.personal"}),
) -> None:
    configure_google_setup_runtime(
        GoogleSetupRuntime(
            data_dir=tmp_path,
            chrome_profiles=frozenset({"personal"}),
            workspace_names=("assigned",),
            workspace_tools=lambda _workspace: workspace_tools,
            workspace_is_admin=lambda _workspace: workspace_is_admin,
            mcp_tool_names=mcp_tool_names,
        )
    )


def _handler() -> Any:
    action = (
        GoogleSetupPlugin(("personal",))
        .pynchy_service_handler()
        .action_for("setup_google_personal")
    )
    assert action is not None
    return action.handler


def _write_valid_credentials(tmp_path: Path) -> None:
    profile_dir = tmp_path / "chrome-profiles" / "personal"
    profile_dir.mkdir(parents=True)
    (profile_dir / "gcp-oauth.keys.json").write_text(
        '{"installed": {"client_id": "123456-client.apps.googleusercontent.com", '
        '"client_secret": "client-secret"}}'  # pragma: allowlist secret
    )
    (profile_dir / "credentials.json").write_text('{"refresh_token": "refresh-token"}')


@pytest.mark.asyncio
async def test_setup_action_requires_configured_runtime() -> None:
    with (
        patch("pynchy.plugins.integrations.google_setup._paths._runtime", None),
        pytest.raises(RuntimeError, match="runtime has not been configured"),
    ):
        await _handler()({"source_group": "assigned"})


def _successful_rest_response(request: urllib.request.Request) -> _Response:
    if request.full_url == "https://oauth2.googleapis.com/token":
        return _Response(b'{"access_token": "access-token"}')
    return _Response(b'{"name": "operations/enable-drive"}')


@pytest.mark.action("integration.google.profile.setup")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("workspace_is_admin", "source_group"),
    [
        pytest.param(False, "assigned", id="assigned-workspace"),
        pytest.param(True, "other-workspace", id="admin-workspace"),
    ],
)
async def test_setup_action_uses_valid_tokens_to_enable_apis_without_browser(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    workspace_is_admin: bool,
    source_group: str,
) -> None:
    _configure_runtime(tmp_path, workspace_is_admin=workspace_is_admin)
    _write_valid_credentials(tmp_path)
    requests: list[str] = []

    def urlopen_https_request(request: urllib.request.Request) -> _Response:
        requests.append(request.full_url)
        return _successful_rest_response(request)

    async def should_not_open_browser(*_args: object) -> dict[str, object]:
        await asyncio.sleep(0)
        raise AssertionError("valid credentials should take the REST fast path")

    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._rest_api.urlopen_https_request",
        urlopen_https_request,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._handler._run_interactive_setup",
        should_not_open_browser,
    )

    result = await _handler()({"source_group": source_group})

    assert result["result"]["status"] == "already_configured"
    assert result["result"]["steps"] == ["APIs verified/enabled via REST"]
    assert requests == [
        "https://oauth2.googleapis.com/token",
        "https://serviceusage.googleapis.com/v1/projects/123456/services/drive.googleapis.com:enable",
    ]


@pytest.mark.asyncio
async def test_setup_action_skips_service_usage_when_project_number_is_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_runtime(tmp_path)
    _write_valid_credentials(tmp_path)
    keys_file = tmp_path / "chrome-profiles" / "personal" / "gcp-oauth.keys.json"
    keys_file.write_text('{"installed": {"client_id": "", "client_secret": "client-secret"}}')
    requests: list[str] = []

    def urlopen_https_request(request: urllib.request.Request) -> _Response:
        requests.append(request.full_url)
        return _Response(b'{"access_token": "access-token"}')

    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._rest_api.urlopen_https_request",
        urlopen_https_request,
    )

    result = await _handler()({"source_group": "assigned"})

    assert result["result"]["status"] == "already_configured"
    assert result["result"]["steps"] == []
    assert requests == ["https://oauth2.googleapis.com/token"]


@pytest.mark.asyncio
@pytest.mark.parametrize("response_body", [b"SCOPE_INSUFFICIENT", b"unexpected failure"])
async def test_setup_action_reopens_interactive_setup_when_rest_enablement_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, response_body: bytes
) -> None:
    _configure_runtime(tmp_path)
    _write_valid_credentials(tmp_path)

    def urlopen_https_request(request: urllib.request.Request) -> _Response:
        if request.full_url == "https://oauth2.googleapis.com/token":
            return _Response(b'{"access_token": "access-token"}')
        raise urllib.error.HTTPError(
            request.full_url, 403, "forbidden", {}, io.BytesIO(response_body)
        )

    async def run_interactive_setup(*_args: object) -> dict[str, object]:
        await asyncio.sleep(0)
        return {"result": {"status": "interactive_setup_required"}}

    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._rest_api.urlopen_https_request",
        urlopen_https_request,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._handler._run_interactive_setup",
        run_interactive_setup,
    )

    assert await _handler()({"source_group": "assigned"}) == {
        "result": {"status": "interactive_setup_required"}
    }


@pytest.mark.asyncio
async def test_setup_action_reopens_interactive_setup_after_rest_transport_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_runtime(tmp_path)
    _write_valid_credentials(tmp_path)

    def urlopen_https_request(request: urllib.request.Request) -> _Response:
        if request.full_url == "https://oauth2.googleapis.com/token":
            return _Response(b'{"access_token": "access-token"}')
        raise urllib.error.URLError("offline")

    async def run_interactive_setup(*_args: object) -> dict[str, object]:
        await asyncio.sleep(0)
        return {"result": {"status": "interactive_setup_required"}}

    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._rest_api.urlopen_https_request",
        urlopen_https_request,
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._handler._run_interactive_setup",
        run_interactive_setup,
    )

    assert await _handler()({"source_group": "assigned"}) == {
        "result": {"status": "interactive_setup_required"}
    }


@pytest.mark.asyncio
async def test_interactive_setup_reads_project_id_from_existing_client_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure_runtime(tmp_path)
    profile_dir = tmp_path / "chrome-profiles" / "personal"
    profile_dir.mkdir(parents=True)
    (profile_dir / "gcp-oauth.keys.json").write_text(
        '{"installed": {"client_id": "123456-client.apps.googleusercontent.com", '
        '"client_secret": "client-secret", '  # pragma: allowlist secret
        '"project_id": "existing-project"}}'
    )
    project_ids: list[str] = []

    async def run_body(setup: Any, *_args: object) -> dict[str, object]:
        await asyncio.sleep(0)
        project_ids.append(setup.project_id)
        return {"result": {"status": "interactive_setup_required"}}

    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._handler.has_display", lambda: True
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.google_setup._handler._run_interactive_setup_body",
        run_body,
    )

    result = await _handler()({"source_group": "assigned"})

    assert result == {"result": {"status": "interactive_setup_required"}}
    assert project_ids == ["existing-project"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("workspace_tools", "mcp_tool_names"),
    [
        pytest.param(None, frozenset({"gdrive.personal"}), id="unknown-workspace"),
        pytest.param(
            ("foreign.personal", "gdrive"),
            frozenset({"gdrive"}),
            id="unknown-and-malformed-tool-names",
        ),
        pytest.param(
            ("foreign.other",),
            frozenset({"foreign.other"}),
            id="unconfigured-profile",
        ),
    ],
)
async def test_setup_action_denies_unresolved_workspace_tool_configuration(
    tmp_path: Path,
    workspace_tools: tuple[str, ...] | None,
    mcp_tool_names: frozenset[str],
) -> None:
    _configure_runtime(tmp_path, workspace_tools=workspace_tools, mcp_tool_names=mcp_tool_names)

    result = await _handler()({"source_group": "assigned"})

    assert result["error"] == (
        "Workspace 'assigned' does not have access to chrome profile 'personal'. "
        "Available profiles: none"
    )


def test_plugin_configure_registers_profile_specific_action() -> None:
    plugin = GoogleSetupPlugin()

    plugin.configure(("personal",))

    assert plugin.pynchy_service_handler().action_for("setup_google_personal") is not None
