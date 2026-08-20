"""Tests for the built-in Proton Mail Bridge MCP integration."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from pynchy.config.api import validate_settings_mapping
from pynchy.host.container_manager.gateway import collect_plugin_mcp_servers
from pynchy.host.container_manager.mcp.resolution import merged_mcp_servers
from pynchy.plugins import get_plugin_manager
from pynchy.plugins.integrations import proton_mail
from pynchy.plugins.integrations.proton_bridge import (
    ProtonMailbox,
    ProtonMailboxList,
    ProtonMailDelivery,
    ProtonMailList,
    ProtonMessage,
    ProtonMessageEnvelope,
)


@dataclass
class StubProtonMailClient:
    """In-memory direct-IMAP substitute used at the MCP boundary."""

    calls: list[tuple[str, object]] = field(default_factory=list)

    def list_mailboxes(self) -> ProtonMailboxList:
        self.calls.append(("list_mailboxes", None))
        return ProtonMailboxList(mailboxes=[ProtonMailbox(name="INBOX", mailbox="INBOX")])

    def list_mail(
        self,
        *,
        mailbox: str,
        limit: int,
        offset: int,
        unread: bool,
    ) -> ProtonMailList:
        self.calls.append(("list_mail", (mailbox, limit, offset, unread)))
        return ProtonMailList(
            messages=[
                ProtonMessageEnvelope(
                    message_id="<event@example.com>",
                    sender="Events <events@example.com>",
                    subject="Event details",
                    date="Tue, 15 Jul 2026 12:00:00 +0000",
                    seen=False,
                )
            ]
        )

    def read_mail(self, *, mailbox: str, message_id: str, include_headers: bool) -> ProtonMessage:
        self.calls.append(("read_mail", (mailbox, message_id, include_headers)))
        return ProtonMessage(message_id=message_id, body="Event details")

    def send_mail(
        self,
        *,
        recipients: list[str],
        subject: str,
        body: str,
    ) -> ProtonMailDelivery:
        self.calls.append(("send_mail", (recipients, subject, body)))
        return ProtonMailDelivery(message_id="<sent@example.com>")

    def delete_mail(self, *, mailbox: str, message_id: str) -> None:
        self.calls.append(("delete_mail", (mailbox, message_id)))

    def message_exists(self, *, mailbox: str, message_id: str) -> bool:
        self.calls.append(("message_exists", (mailbox, message_id)))
        return False


async def _start_mcp_client(stub: StubProtonMailClient) -> TestClient:
    client = TestClient(TestServer(proton_mail.build_app(lambda: stub)))
    await client.start_server()
    return client


class TestProtonMailMcpPlugin:
    def test_plugin_provides_script_mcp_server(self):
        spec = proton_mail.ProtonMailMcpPlugin().pynchy_mcp_server_spec()[0]

        assert spec.name == "proton-mail"
        assert spec.config.type == "script"
        assert spec.config.args == [
            "run",
            "python",
            "-m",
            "pynchy.plugins.integrations.proton_mail",
            "--port",
            "{port}",
        ]
        assert spec.trust is not None
        assert spec.trust.public_source is True
        assert spec.trust.secret_data is True
        assert spec.trust.public_sink is True
        assert spec.trust.dangerous_writes is True

    def test_plugin_is_registered(self):
        plugin = get_plugin_manager().get_plugin("builtin-proton-mail")

        assert isinstance(plugin, proton_mail.ProtonMailMcpPlugin)

    def test_explicit_tool_config_uses_the_plugin_server_spec(self):
        settings = validate_settings_mapping(
            {
                "profiles": {"events": {"tools": ["proton-mail"]}},
                "workspaces": {"events": {"profiles": ["events"]}},
                "tools": {
                    "proton-mail": {
                        "type": "mcp",
                        "public_source": True,
                        "secret_data": True,
                        "public_sink": True,
                        "dangerous_writes": True,
                        "mcp": {
                            "runtime": "script",
                            "command": "uv",
                            "args": [
                                "run",
                                "python",
                                "-m",
                                "pynchy.plugins.integrations.proton_mail",
                                "--port",
                                "{port}",
                            ],
                            "port": 8475,
                            "transport": "streamable_http",
                        },
                    }
                },
            }
        )
        servers, _trust_defaults = collect_plugin_mcp_servers(get_plugin_manager())

        resolved = merged_mcp_servers(settings, servers)

        assert resolved["proton-mail"].command == "uv"
        assert resolved["proton-mail"].port == 8475


class TestProtonMailMcpServer:
    async def test_mcp_lists_all_operational_mail_tools(self):
        stub = StubProtonMailClient()
        client = await _start_mcp_client(stub)
        try:
            response = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )

            payload = await response.json()
        finally:
            await client.close()

        names = {tool["name"] for tool in payload["result"]["tools"]}
        assert names == {
            "proton_list_mailboxes",
            "proton_list_mail",
            "proton_read_mail",
            "proton_send_mail",
            "proton_delete_mail",
        }

    @pytest.mark.action("mail.proton.message.read")
    async def test_mcp_reads_a_message_through_the_injected_direct_client(self):
        stub = StubProtonMailClient()
        client = await _start_mcp_client(stub)
        try:
            response = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "proton_read_mail",
                        "arguments": {"message_id": "<event@example.com>", "headers": True},
                    },
                },
            )

            payload = await response.json()
        finally:
            await client.close()

        text = payload["result"]["content"][0]["text"]
        assert json.loads(text)["message_id"] == "<event@example.com>"
        assert stub.calls == [("read_mail", ("INBOX", "<event@example.com>", True))]

    @pytest.mark.action("mail.proton.mailbox.list")
    async def test_mcp_lists_mailboxes_through_the_injected_direct_client(self):
        stub = StubProtonMailClient()
        client = await _start_mcp_client(stub)
        try:
            response = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "proton_list_mailboxes", "arguments": {}},
                },
            )
            payload = await response.json()
        finally:
            await client.close()

        text = payload["result"]["content"][0]["text"]
        assert json.loads(text) == {"mailboxes": [{"mailbox": "INBOX", "name": "INBOX"}]}
        assert stub.calls == [("list_mailboxes", None)]

    @pytest.mark.action("mail.proton.mailbox.list")
    async def test_mcp_ignores_protocol_metadata_on_tool_calls(self):
        stub = StubProtonMailClient()
        client = await _start_mcp_client(stub)
        try:
            response = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "proton_list_mailboxes",
                        "arguments": {},
                        "_meta": {"callId": "exec-123", "progressToken": 1},
                    },
                },
            )
            payload = await response.json()
        finally:
            await client.close()

        assert "isError" not in payload["result"]
        assert stub.calls == [("list_mailboxes", None)]

    @pytest.mark.action("mail.proton.message.list")
    async def test_mcp_lists_messages_through_the_injected_direct_client(self):
        stub = StubProtonMailClient()
        client = await _start_mcp_client(stub)
        try:
            response = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "proton_list_mail",
                        "arguments": {
                            "mailbox": "Archive",
                            "limit": 2,
                            "offset": 3,
                            "unread": True,
                        },
                    },
                },
            )
            payload = await response.json()
        finally:
            await client.close()

        text = payload["result"]["content"][0]["text"]
        assert json.loads(text)["messages"][0]["message_id"] == "<event@example.com>"
        assert stub.calls == [("list_mail", ("Archive", 2, 3, True))]

    @pytest.mark.parametrize(
        ("name", "arguments"),
        [
            ("proton_read_mail", {"uid": "34"}),
            ("proton_list_mail", {"mailbox": "Archive\nArchive"}),
            (
                "proton_read_mail",
                {"message_id": "<event@example.com>", "mailbox": "Archive\nArchive"},
            ),
            (
                "proton_send_mail",
                {
                    "to": ["recipient@example.com\r\nBcc: attacker@example.com"],
                    "subject": "Canary",
                    "body": "Safe test body",
                },
            ),
            (
                "proton_send_mail",
                {
                    "to": ["recipient@example.com"],
                    "subject": "Canary\nInjected",
                    "body": "Safe test body",
                },
            ),
            (
                "proton_send_mail",
                {
                    "to": ["Display Name <recipient@example.com>"],
                    "subject": "Canary",
                    "body": "Safe test body",
                },
            ),
            (
                "proton_send_mail",
                {
                    "to": ["recipient@exa\u00a0mple.com"],
                    "subject": "Canary",
                    "body": "Safe test body",
                },
            ),
            ("proton_delete_mail", {"message_id": "<sent@example.com>", "mailbox": "\n"}),
        ],
    )
    async def test_mcp_rejects_invalid_tool_arguments(
        self,
        name: str,
        arguments: dict[str, object],
    ) -> None:
        client = await _start_mcp_client(StubProtonMailClient())
        try:
            response = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
            )
            payload = await response.json()
        finally:
            await client.close()

        assert payload["result"]["isError"] is True
        assert "Invalid Proton Mail tool arguments" in payload["result"]["content"][0]["text"]

    async def test_mcp_serves_protocol_control_requests_and_rejects_invalid_json_rpc(self):
        client = await _start_mcp_client(StubProtonMailClient())
        try:
            initialize = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            )
            notifications = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            unknown = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": "unknown", "method": "tools/unknown"},
            )
            invalid = await client.post(
                "/mcp",
                json={"jsonrpc": "1.0", "id": 2, "method": "initialize"},
            )
            initialize_payload = await initialize.json()
            unknown_payload = await unknown.json()
            invalid_payload = await invalid.json()
        finally:
            await client.close()

        assert initialize_payload["result"]["serverInfo"]["name"] == "pynchy-proton-mail"
        assert notifications.status == 202
        assert unknown_payload["error"]["code"] == -32601
        assert invalid_payload["error"]["code"] == -32600

    def test_main_passes_cli_port_to_aiohttp(self):
        with patch("pynchy.plugins.integrations.proton_mail.web.run_app") as run_app:
            proton_mail.main(["--port", "9999"])

        assert run_app.call_args.kwargs["host"] == proton_mail.LOCAL_MCP_BIND_HOST
        assert run_app.call_args.kwargs["port"] == 9999

    @pytest.mark.action("mail.proton.message.send")
    async def test_mcp_sends_mail_through_the_injected_bridge_client(self):
        stub = StubProtonMailClient()
        client = await _start_mcp_client(stub)
        try:
            response = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "proton_send_mail",
                        "arguments": {
                            "to": ["recipient@example.com"],
                            "subject": "Canary",
                            "body": "Safe test body",
                        },
                    },
                },
            )
            payload = await response.json()
        finally:
            await client.close()

        assert json.loads(payload["result"]["content"][0]["text"]) == {
            "message_id": "<sent@example.com>"
        }
        assert stub.calls == [
            ("send_mail", (["recipient@example.com"], "Canary", "Safe test body"))
        ]

    @pytest.mark.action("mail.proton.message.delete")
    async def test_mcp_deletes_mail_through_the_injected_bridge_client(self):
        stub = StubProtonMailClient()
        client = await _start_mcp_client(stub)
        try:
            response = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "proton_delete_mail",
                        "arguments": {"mailbox": "INBOX", "message_id": "<sent@example.com>"},
                    },
                },
            )
            payload = await response.json()
        finally:
            await client.close()

        assert json.loads(payload["result"]["content"][0]["text"]) == {
            "message_id": "<sent@example.com>"
        }
        assert stub.calls == [("delete_mail", ("INBOX", "<sent@example.com>"))]
