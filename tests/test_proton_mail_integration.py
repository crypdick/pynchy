"""Tests for the built-in, direct-IMAP Proton Mail MCP integration."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest
from aiohttp.test_utils import TestClient, TestServer

from pynchy.config.settings import validate_settings_mapping
from pynchy.host.container_manager.gateway import collect_plugin_mcp_servers
from pynchy.host.container_manager.mcp.resolution import merged_mcp_servers
from pynchy.plugins import get_plugin_manager
from pynchy.plugins.integrations import proton_mail
from pynchy.plugins.integrations.proton_bridge import (
    ProtonMailbox,
    ProtonMailboxList,
    ProtonMailError,
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


async def _start_mcp_client(stub: StubProtonMailClient) -> TestClient:
    client = TestClient(TestServer(proton_mail.build_app(lambda: stub)))
    await client.start_server()
    return client


class TestProtonMailMcpPlugin:
    def test_plugin_provides_script_mcp_server(self):
        spec = proton_mail.ProtonMailMcpPlugin().pynchy_mcp_server_spec()

        assert spec["name"] == "proton-mail"
        assert spec["type"] == "script"
        assert spec["args"] == [
            "run",
            "python",
            "-m",
            "pynchy.plugins.integrations.proton_mail",
            "--port",
            "{port}",
        ]
        assert spec["trust"] == {
            "public_source": False,
            "secret_data": True,
            "public_sink": False,
            "dangerous_writes": False,
        }

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
                        "public_source": False,
                        "secret_data": True,
                        "public_sink": False,
                        "dangerous_writes": False,
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


class TestProtonMailOperations:
    async def test_list_mail_forwards_typed_filters_to_the_direct_client(self):
        stub = StubProtonMailClient()

        result = await proton_mail._call_tool(
            {
                "name": "proton_list_mail",
                "arguments": {"mailbox": "Archive", "limit": 2, "offset": 3, "unread": True},
            },
            lambda: stub,
        )

        assert json.loads(result["content"][0]["text"])["messages"][0]["message_id"] == (
            "<event@example.com>"
        )
        assert stub.calls == [("list_mail", ("Archive", 2, 3, True))]

    async def test_read_mail_uses_message_id_not_a_persistent_imap_uid(self):
        stub = StubProtonMailClient()

        result = await proton_mail._call_tool(
            {
                "name": "proton_read_mail",
                "arguments": {"message_id": "<event@example.com>", "headers": True},
            },
            lambda: stub,
        )

        assert json.loads(result["content"][0]["text"])["body"] == "Event details"
        assert stub.calls == [("read_mail", ("INBOX", "<event@example.com>", True))]

    async def test_read_mail_rejects_the_removed_uid_argument(self):
        with pytest.raises(ProtonMailError, match="Invalid Proton Mail tool arguments"):
            await proton_mail._call_tool(
                {"name": "proton_read_mail", "arguments": {"uid": "34"}},
                StubProtonMailClient,
            )


class TestProtonMailMcpServer:
    async def test_mcp_lists_only_read_only_tools(self):
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
        }

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
                        "arguments": {"message_id": "<event@example.com>"},
                    },
                },
            )

            payload = await response.json()
        finally:
            await client.close()

        text = payload["result"]["content"][0]["text"]
        assert json.loads(text)["message_id"] == "<event@example.com>"
