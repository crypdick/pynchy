"""Contract tests for Pynchy's narrow, host-only Gog integration."""

from __future__ import annotations

import json
import subprocess  # noqa: S404 - tests construct CompletedProcess fixtures.
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from pynchy.capabilities import CapabilityProbeContext, ProbeStatus
from pynchy.config.settings import validate_settings_mapping
from pynchy.plugins import get_plugin_manager
from pynchy.plugins.integrations import gog

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class StubResolvedWorkspace:
    tools: list[str]


@dataclass(frozen=True)
class StubSettings:
    tools: list[str]

    def resolved_workspace_config(self, workspace_name: str) -> StubResolvedWorkspace | None:
        return StubResolvedWorkspace(self.tools) if workspace_name == "workspace" else None


@dataclass
class StubGogClient:
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    def gmail_search(self, **arguments: object) -> str:
        return self._result("gmail_search", arguments, {"messages": [{"id": "message-1"}]})

    def gmail_get(self, **arguments: object) -> str:
        return self._result("gmail_get", arguments, {"id": "message-1", "body": "Hello"})

    def gmail_create_draft(self, **arguments: object) -> str:
        return self._result("gmail_create_draft", arguments, {"draftId": "draft-1"})

    def gmail_send_draft(self, **arguments: object) -> str:
        return self._result("gmail_send_draft", arguments, {"id": "message-2"})

    def gmail_send(self, **arguments: object) -> str:
        return self._result("gmail_send", arguments, {"id": "message-3"})

    def contacts_search(self, **arguments: object) -> str:
        return self._result("contacts_search", arguments, {"contacts": [{"name": "Ada"}]})

    def docs_read(self, **arguments: object) -> str:
        return self._result("docs_read", arguments, {"text": "Notes"})

    def docs_export(self, **arguments: object) -> str:
        return self._result("docs_export", arguments, {"text": "# Notes"})

    def sheets_get(self, **arguments: object) -> str:
        return self._result("sheets_get", arguments, {"values": [["Ada"]]})

    def sheets_update(self, **arguments: object) -> str:
        return self._result("sheets_update", arguments, {"updatedRange": "Sheet1!A1"})

    def setup_start(self) -> str:
        return self._result(
            "setup_start", {}, {"authorization_url": "https://accounts.example/auth"}
        )

    def setup_complete(self, **arguments: object) -> str:
        return self._result("setup_complete", arguments, {"account": "you@example.com"})

    def _result(self, name: str, arguments: dict[str, object], result: object) -> str:
        self.calls.append((name, arguments))
        return json.dumps(result)


def _handler(tool_name: str):
    action = gog.GOG_HOST_ACTIONS.action_for(tool_name)
    assert action is not None
    return action.handler


def _enabled_settings() -> StubSettings:
    return StubSettings(["gog"])


def test_gog_config_resolves_paths_with_runtime_type_checking() -> None:
    """Gog setup reaches these methods through beartype in the host process."""
    settings = validate_settings_mapping({})
    config = gog.GogConfig(home="data/gog", oauth_client_path="data/gog-client.json")

    assert config.resolved_home(settings) == settings.project_root / "data/gog"
    assert config.resolved_oauth_client_path(settings) == (
        settings.project_root / "data/gog-client.json"
    )


class TestGogPlugin:
    def test_plugin_is_registered_with_its_host_actions(self) -> None:
        plugin = get_plugin_manager().get_plugin("builtin-gog")

        assert isinstance(plugin, gog.GogWorkspacePlugin)
        assert {str(action.tool_name) for action in gog.GOG_HOST_ACTIONS.actions} == {
            "gog_setup_start",
            "gog_setup_complete",
            "gog_gmail_search",
            "gog_gmail_get",
            "gog_gmail_create_draft",
            "gog_gmail_send_draft",
            "gog_gmail_send",
            "gog_contacts_search",
            "gog_docs_read",
            "gog_docs_export",
            "gog_sheets_get",
            "gog_sheets_update",
        }

    @pytest.mark.asyncio
    async def test_probe_reports_a_missing_gog_binary_without_contacting_google(self) -> None:
        action = gog.GOG_HOST_ACTIONS.action_for("gog_gmail_search")
        assert action is not None
        probe = action.capability.probe
        assert probe is not None

        with (
            patch(
                "pynchy.plugins.integrations.gog._plugin.gog_config",
                return_value=gog.GogConfig(account="you@example.com"),
            ),
            patch(
                "pynchy.plugins.integrations.gog._plugin.gog_executable_exists",
                return_value=False,
            ),
        ):
            result = await probe(CapabilityProbeContext("workspace"))

        assert result.status is ProbeStatus.UNAVAILABLE
        assert "install gogcli" in result.reason.lower()


def test_gog_subprocess_does_not_inherit_unrelated_host_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UNRELATED_HOST_SECRET", "must-not-leak")
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="{}", stderr="")
    client = gog.GogClient(
        config=gog.GogConfig(account="you@example.com"),
        home=tmp_path,
        oauth_client_path=None,
    )

    with patch(
        "pynchy.plugins.integrations.gog._client.subprocess.run",
        return_value=completed,
    ) as run:
        client.gmail_search(query="from:friend@example.com", limit=1)

    environment = run.call_args.kwargs["env"]
    assert environment["GOG_HOME"] == str(tmp_path)
    assert "UNRELATED_HOST_SECRET" not in environment


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments", "called_method"),
    [
        pytest.param(
            "gog_gmail_search",
            {"query": "from:friend@example.com", "limit": 5},
            "gmail_search",
            marks=pytest.mark.action("mail.gog.message.search"),
        ),
        pytest.param(
            "gog_gmail_get",
            {"message_id": "message-1"},
            "gmail_get",
            marks=pytest.mark.action("mail.gog.message.read"),
        ),
        pytest.param(
            "gog_contacts_search",
            {"query": "Ada", "limit": 5},
            "contacts_search",
            marks=pytest.mark.action("contacts.gog.contact.search"),
        ),
        pytest.param(
            "gog_docs_read",
            {"document_id": "doc-1", "tab": "tab-1"},
            "docs_read",
            marks=pytest.mark.action("docs.gog.document.read"),
        ),
        pytest.param(
            "gog_docs_export",
            {"document_id": "doc-1", "format": "md"},
            "docs_export",
            marks=pytest.mark.action("docs.gog.document.export"),
        ),
        pytest.param(
            "gog_sheets_get",
            {"spreadsheet_id": "sheet-1", "range": "Sheet1!A1"},
            "sheets_get",
            marks=pytest.mark.action("sheets.gog.range.read"),
        ),
    ],
)
async def test_read_actions_are_fenced_after_host_only_gog_call(
    tool_name: str,
    arguments: dict[str, object],
    called_method: str,
) -> None:
    client = StubGogClient()
    handler = _handler(tool_name)

    with (
        patch("pynchy.plugins.integrations.gog._handlers.create_gog_client", return_value=client),
        patch(
            "pynchy.plugins.integrations.gog._plugin.get_settings",
            return_value=_enabled_settings(),
        ),
    ):
        result = await handler({"source_group": "workspace", **arguments})

    assert "<<<EXTERNAL_UNTRUSTED_CONTENT" in result["result"]
    assert client.calls[0][0] == called_method


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments", "called_method"),
    [
        pytest.param(
            "gog_gmail_create_draft",
            {"to": ["friend@example.com"], "subject": "Hello", "body": "Body"},
            "gmail_create_draft",
            marks=pytest.mark.action("mail.gog.draft.create"),
        ),
        pytest.param(
            "gog_gmail_send_draft",
            {"draft_id": "draft-1"},
            "gmail_send_draft",
            marks=pytest.mark.action("mail.gog.draft.send"),
        ),
        pytest.param(
            "gog_gmail_send",
            {"to": ["friend@example.com"], "subject": "Hello", "body": "Body"},
            "gmail_send",
            marks=pytest.mark.action("mail.gog.message.send"),
        ),
        pytest.param(
            "gog_sheets_update",
            {"spreadsheet_id": "sheet-1", "range": "Sheet1!A1", "values": [["Ada"]]},
            "sheets_update",
            marks=pytest.mark.action("sheets.gog.range.write"),
        ),
    ],
)
async def test_write_actions_use_reviewed_host_operation_and_action_intent(
    tool_name: str,
    arguments: dict[str, object],
    called_method: str,
) -> None:
    client = StubGogClient()
    action = gog.GOG_HOST_ACTIONS.action_for(tool_name)
    assert action is not None
    assert action.action_intent is not None

    with (
        patch("pynchy.plugins.integrations.gog._handlers.create_gog_client", return_value=client),
        patch(
            "pynchy.plugins.integrations.gog._plugin.get_settings",
            return_value=_enabled_settings(),
        ),
    ):
        result = await action.handler({"source_group": "workspace", **arguments})

    draft = action.action_intent.draft_from_request(arguments)
    receipt = action.action_intent.receipt_from_response(result)
    assert client.calls[0][0] == called_method
    assert draft.summary
    assert receipt.provider_request_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments", "called_method"),
    [
        pytest.param(
            "gog_setup_start",
            {},
            "setup_start",
            marks=pytest.mark.action("integration.gog.auth.start"),
        ),
        pytest.param(
            "gog_setup_complete",
            {"redirect_url": "https://localhost/callback?code=example"},
            "setup_complete",
            marks=pytest.mark.action("integration.gog.auth.complete"),
        ),
    ],
)
async def test_oauth_actions_stay_in_the_host_handler(
    tool_name: str,
    arguments: dict[str, object],
    called_method: str,
) -> None:
    client = StubGogClient()
    handler = _handler(tool_name)

    with (
        patch("pynchy.plugins.integrations.gog._handlers.create_gog_client", return_value=client),
        patch(
            "pynchy.plugins.integrations.gog._plugin.get_settings",
            return_value=_enabled_settings(),
        ),
    ):
        result = await handler({"source_group": "workspace", **arguments})

    assert "error" not in result
    assert client.calls[0][0] == called_method


@pytest.mark.asyncio
async def test_gog_action_is_denied_when_the_workspace_does_not_select_gog() -> None:
    client = StubGogClient()
    handler = _handler("gog_gmail_search")

    with (
        patch("pynchy.plugins.integrations.gog._handlers.create_gog_client", return_value=client),
        patch(
            "pynchy.plugins.integrations.gog._plugin.get_settings",
            return_value=StubSettings([]),
        ),
    ):
        result = await handler({"source_group": "workspace", "query": "from:friend@example.com"})

    assert result == {"error": "gog_gmail_search is not enabled for this workspace"}
    assert client.calls == []


@pytest.mark.action("mail.gog.message.send", "sheets.gog.range.write")
def test_gog_builds_fixed_write_commands_and_passes_content_on_standard_input(
    tmp_path: Path,
) -> None:
    client = gog.GogClient(
        config=gog.GogConfig(account="you@example.com"),
        home=tmp_path / "gog-home",
        oauth_client_path=None,
    )
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, '{"id":"provider-id"}', "")

    with patch("pynchy.plugins.integrations.gog._client.subprocess.run", side_effect=run):
        client.gmail_send(
            to=["friend@example.com"],
            cc=[],
            bcc=[],
            subject="Hello",
            body="Sensitive body",
        )
        client.sheets_update(
            spreadsheet_id="sheet-1",
            range_name="Sheet1!A1",
            values=[["Ada"]],
            input_mode="RAW",
        )

    mail_command, mail_kwargs = calls[0]
    sheet_command, sheet_kwargs = calls[1]
    assert "Sensitive body" not in mail_command
    assert mail_kwargs["input"] == "Sensitive body"
    assert "--readonly" not in mail_command
    assert "--enable-commands-exact" in mail_command
    assert mail_command[mail_command.index("--enable-commands-exact") + 1] == "gmail.send"
    assert sheet_kwargs["input"] == '[["Ada"]]'
    assert sheet_command[sheet_command.index("--enable-commands-exact") + 1] == "sheets.update"


@pytest.mark.action("integration.gog.auth.start", "integration.gog.auth.complete")
def test_oauth_commands_have_fixed_services_and_read_only_drive_scope(tmp_path: Path) -> None:
    credentials = tmp_path / "client.json"
    credentials.write_text("{}", encoding="utf-8")
    client = gog.GogClient(
        config=gog.GogConfig(account="you@example.com"),
        home=tmp_path / "gog-home",
        oauth_client_path=credentials,
    )
    calls: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        output = (
            '{"status":"stored"}' if len(calls) == 1 else '{"authorization_url":"https://auth"}'
        )
        return subprocess.CompletedProcess(command, 0, output, "")

    with patch("pynchy.plugins.integrations.gog._client.subprocess.run", side_effect=run):
        client.setup_start()
        client.setup_complete(redirect_url="https://localhost/callback?code=example")

    auth_commands = [command for command in calls if "auth" in command and "add" in command]
    assert len(auth_commands) == 2
    for command in auth_commands:
        assert command[command.index("--services") + 1] == "gmail,contacts,docs,sheets,drive"
        assert command[command.index("--drive-scope") + 1] == "readonly"
