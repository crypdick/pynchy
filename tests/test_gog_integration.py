"""Contract tests for Pynchy's narrow, host-only Gog integration."""

from __future__ import annotations

import json
import subprocess  # noqa: S404 - tests construct CompletedProcess fixtures.
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from pynchy.plugins import get_plugin_manager
from pynchy.plugins.api import CapabilityProbeContext, ProbeStatus
from pynchy.plugins.integrations import gog


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


def _configure_gog_runtime(*, enabled: bool = True, config: gog.GogConfig | None = None) -> None:
    gog.configure_gog_runtime(
        gog.GogRuntime(
            config=config or gog.GogConfig(account="you@example.com"),
            home=Path.cwd() / "pynchy-gog-test",
            oauth_client_path=None,
            workspace_enables_gog=lambda workspace: enabled and workspace == "workspace",
        )
    )


@pytest.fixture(autouse=True)
def _configure_runtime() -> None:
    _configure_gog_runtime()


def test_gog_client_uses_resolved_runtime_paths(tmp_path: Path) -> None:
    """Gog actions receive host-resolved paths rather than settings access."""
    config = gog.GogConfig(home="data/gog", oauth_client_path="data/gog-client.json")
    home = tmp_path / "data/gog"
    oauth_client_path = tmp_path / "data/gog-client.json"
    gog.configure_gog_runtime(
        gog.GogRuntime(
            config=config,
            home=home,
            oauth_client_path=oauth_client_path,
            workspace_enables_gog=lambda _workspace: True,
        )
    )

    client = gog.create_gog_client()
    assert client.home == home
    assert client.oauth_client_path == oauth_client_path


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

        with patch(
            "pynchy.plugins.integrations.gog._plugin.gog_executable_exists",
            return_value=False,
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


def test_gog_client_builds_reviewed_read_and_draft_commands(tmp_path: Path) -> None:
    client = gog.GogClient(
        config=gog.GogConfig(account="you@example.com"),
        home=tmp_path / "gog-home",
        oauth_client_path=None,
    )
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "{}", "")

    with patch("pynchy.plugins.integrations.gog._client.subprocess.run", side_effect=run):
        assert client.gmail_get(message_id="message-1") == "{}"
        assert (
            client.gmail_create_draft(
                to=["to@example.com"],
                cc=["cc@example.com"],
                bcc=["bcc@example.com"],
                subject="Subject",
                body="Body",
            )
            == "{}"
        )
        assert client.contacts_search(query="Ada", limit=3) == "{}"
        assert client.docs_read(document_id="doc-1", tab="tab-1") == "{}"
        assert client.docs_export(document_id="doc-1", export_format="md") == "{}"
        assert client.sheets_get(spreadsheet_id="sheet-1", range_name="Sheet1!A1") == "{}"

    commands = [command for command, _kwargs in calls]
    assert commands[0][-5:] == ["gmail", "get", "--sanitize-content", "--", "message-1"]
    assert "--readonly" in commands[0]
    assert commands[1][-13:] == [
        "gmail",
        "drafts",
        "create",
        "--to",
        "to@example.com",
        "--subject",
        "Subject",
        "--body-file",
        "-",
        "--cc",
        "cc@example.com",
        "--bcc",
        "bcc@example.com",
    ]
    assert calls[1][1]["input"] == "Body"
    assert "--readonly" not in commands[1]
    assert commands[2][-6:] == ["contacts", "search", "--max", "3", "--", "Ada"]
    assert commands[3][-8:] == [
        "docs",
        "cat",
        "--max-bytes",
        "2000000",
        "--tab",
        "tab-1",
        "--",
        "doc-1",
    ]
    assert commands[4][-8:] == [
        "docs",
        "export",
        "--format",
        "md",
        "--out",
        "-",
        "--",
        "doc-1",
    ]
    assert commands[5][-5:] == ["sheets", "get", "--", "sheet-1", "Sheet1!A1"]
    readonly_commands = (commands[2], commands[3], commands[4], commands[5])
    assert all("--readonly" in command for command in readonly_commands)


def test_gog_client_builds_send_draft_command(tmp_path: Path) -> None:
    client = gog.GogClient(
        config=gog.GogConfig(account="you@example.com"),
        home=tmp_path,
        oauth_client_path=None,
    )
    completed = subprocess.CompletedProcess([], 0, "{}", "")

    with patch("pynchy.plugins.integrations.gog._client.subprocess.run", return_value=completed):
        assert client.gmail_send_draft(draft_id="draft-1") == "{}"


def test_gog_client_omits_optional_docs_tab(tmp_path: Path) -> None:
    client = gog.GogClient(
        config=gog.GogConfig(account="you@example.com"),
        home=tmp_path,
        oauth_client_path=None,
    )
    completed = subprocess.CompletedProcess([], 0, "{}", "")

    with patch(
        "pynchy.plugins.integrations.gog._client.subprocess.run", return_value=completed
    ) as run:
        assert client.docs_read(document_id="doc-1", tab=None) == "{}"

    assert "--tab" not in run.call_args.args[0]


@pytest.mark.parametrize(
    ("outcome", "message"),
    [
        pytest.param(FileNotFoundError(), "unavailable"),
        pytest.param(subprocess.TimeoutExpired(["gog"], timeout=1), "timed out"),
        pytest.param(subprocess.CompletedProcess([], 1, "", ""), "failed"),
        pytest.param(subprocess.CompletedProcess([], 0, " ", ""), "no data"),
    ],
)
def test_gog_client_returns_safe_errors_for_execution_failures(
    tmp_path: Path,
    outcome: Exception | subprocess.CompletedProcess[str],
    message: str,
) -> None:
    client = gog.GogClient(
        config=gog.GogConfig(account="you@example.com"),
        home=tmp_path,
        oauth_client_path=None,
    )

    def run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    with (
        patch("pynchy.plugins.integrations.gog._client.subprocess.run", side_effect=run),
        pytest.raises(gog.GogError, match=message),
    ):
        client.gmail_search(query="Ada", limit=1)


@pytest.mark.parametrize(
    ("output", "message"),
    [("not json", "invalid JSON"), ('"scalar"', "unsupported JSON")],
)
def test_gog_client_rejects_invalid_provider_json(
    tmp_path: Path,
    output: str,
    message: str,
) -> None:
    client = gog.GogClient(
        config=gog.GogConfig(account="you@example.com"),
        home=tmp_path,
        oauth_client_path=None,
    )
    result = subprocess.CompletedProcess([], 0, output, "")

    with (
        patch("pynchy.plugins.integrations.gog._client.subprocess.run", return_value=result),
        pytest.raises(gog.GogError, match=message),
    ):
        client.gmail_search(query="Ada", limit=1)


def test_gog_client_rejects_oversized_provider_output(tmp_path: Path) -> None:
    client = gog.GogClient(
        config=gog.GogConfig(account="you@example.com"),
        home=tmp_path,
        oauth_client_path=None,
    )
    result = subprocess.CompletedProcess([], 0, "x" * 2_000_001, "")

    with (
        patch("pynchy.plugins.integrations.gog._client.subprocess.run", return_value=result),
        pytest.raises(gog.GogError, match="safe output limit"),
    ):
        client.gmail_search(query="Ada", limit=1)


def test_gog_client_requires_account_and_available_oauth_credentials(tmp_path: Path) -> None:
    unconfigured = gog.GogClient(
        config=gog.GogConfig(),
        home=tmp_path,
        oauth_client_path=None,
    )
    missing_credentials = gog.GogClient(
        config=gog.GogConfig(account="you@example.com"),
        home=tmp_path,
        oauth_client_path=tmp_path / "missing.json",
    )

    with pytest.raises(gog.GogError, match="account"):
        unconfigured.gmail_search(query="Ada", limit=1)
    with pytest.raises(gog.GogError, match="oauth_client_path"):
        unconfigured.setup_start()
    with pytest.raises(gog.GogError, match="credentials"):
        missing_credentials.setup_start()


@pytest.mark.asyncio
async def test_gog_executable_probe_checks_explicit_paths(tmp_path: Path) -> None:
    executable = tmp_path / "gog"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)

    gog.configure_gog_runtime(
        gog.GogRuntime(
            config=gog.GogConfig(command=str(executable), account="you@example.com"),
            home=tmp_path,
            oauth_client_path=None,
            workspace_enables_gog=lambda _workspace: True,
        )
    )
    action = gog.GOG_HOST_ACTIONS.action_for("gog_gmail_search")
    assert action is not None
    assert action.capability.probe is not None

    result = await action.capability.probe(CapabilityProbeContext("workspace"))

    assert result.status is ProbeStatus.READY


@pytest.mark.asyncio
async def test_gog_executable_probe_uses_path_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "gog"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    gog.configure_gog_runtime(
        gog.GogRuntime(
            config=gog.GogConfig(command="gog", account="you@example.com"),
            home=tmp_path,
            oauth_client_path=None,
            workspace_enables_gog=lambda _workspace: True,
        )
    )
    action = gog.GOG_HOST_ACTIONS.action_for("gog_gmail_search")
    assert action is not None
    assert action.capability.probe is not None

    result = await action.capability.probe(CapabilityProbeContext("workspace"))

    assert result.status is ProbeStatus.READY


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
    ):
        result = await action.handler({"source_group": "workspace", **arguments})

    draft = action.action_intent.draft_from_request(arguments)
    receipt = action.action_intent.receipt_from_response(result)
    assert client.calls[0][0] == called_method
    assert draft.summary
    assert receipt.provider_request_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments", "message"),
    [
        (
            "gog_gmail_send",
            {"to": ["friend@example.com"], "subject": "Hello", "body": " "},
            "body must not be empty",
        ),
        (
            "gog_sheets_update",
            {"spreadsheet_id": "sheet-1", "range": "Sheet1!A1", "values": [[]]},
            "values must not contain empty rows",
        ),
        (
            "gog_setup_complete",
            {"redirect_url": "ftp://localhost/callback"},
            "HTTP(S) URL",
        ),
        (
            "gog_gmail_send",
            {"to": ["friend@example.com,other@example.com"], "subject": "Hello", "body": "Body"},
            "one email address",
        ),
    ],
)
async def test_gog_handlers_reject_invalid_public_payloads(
    tool_name: str,
    arguments: dict[str, object],
    message: str,
) -> None:
    result = await _handler(tool_name)({"source_group": "workspace", **arguments})

    assert "error" in result
    assert message in str(result["error"])


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
    ):
        result = await handler({"source_group": "workspace", **arguments})

    assert "error" not in result
    assert client.calls[0][0] == called_method


@pytest.mark.asyncio
async def test_gog_action_is_denied_when_the_workspace_does_not_select_gog() -> None:
    client = StubGogClient()
    handler = _handler("gog_gmail_search")

    _configure_gog_runtime(enabled=False)
    with (
        patch("pynchy.plugins.integrations.gog._handlers.create_gog_client", return_value=client),
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name",
    [
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
        "gog_setup_complete",
    ],
)
async def test_gog_handlers_return_validation_errors_for_incomplete_arguments(
    tool_name: str,
) -> None:
    result = await _handler(tool_name)({"source_group": "workspace"})

    assert "error" in result


@pytest.mark.asyncio
async def test_gog_setup_start_returns_a_safe_client_error() -> None:
    with patch(
        "pynchy.plugins.integrations.gog._handlers.create_gog_client",
        side_effect=gog.GogError("credentials unavailable"),
    ):
        result = await _handler("gog_setup_start")({"source_group": "workspace"})

    assert result == {"error": "Gog setup could not start: credentials unavailable"}
