"""Tests for the built-in read-only Proton Mail MCP integration."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, call, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from pynchy.config.settings import validate_settings_mapping
from pynchy.host.container_manager.gateway import collect_plugin_mcp_servers
from pynchy.host.container_manager.mcp.resolution import merged_mcp_servers
from pynchy.plugins import get_plugin_manager
from pynchy.plugins.integrations import proton_mail


async def _start_mcp_client() -> TestClient:
    client = TestClient(TestServer(proton_mail.build_app()))
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
    async def test_list_mail_uses_requested_filters(self):
        result = {"messages": [{"uid": 12, "seen": False}]}
        run_command = AsyncMock(return_value=json.dumps(result))

        with patch(
            "pynchy.plugins.integrations.proton_mail._run_proton_command",
            new=run_command,
        ):
            assert (
                await proton_mail._list_mail(
                    {"mailbox": "Archive", "limit": 2, "offset": 3, "unread": True}
                )
                == result
            )

        run_command.assert_awaited_once_with(
            "mail",
            "list",
            "--mailbox",
            "Archive",
            "--limit",
            "2",
            "--offset",
            "3",
            "--unread",
            "--json",
        )

    async def test_read_mail_restores_an_unread_message(self):
        listing = {"messages": [{"uid": 34, "seen": False}]}
        message = {"uid": 34, "body": "Event details"}
        run_command = AsyncMock(
            side_effect=[json.dumps(listing), json.dumps(message), "flag restored"]
        )

        with patch(
            "pynchy.plugins.integrations.proton_mail._run_proton_command",
            new=run_command,
        ):
            assert await proton_mail._read_mail({"uid": "34", "headers": True}) == message

        assert run_command.await_args_list == [
            call("mail", "list", "--mailbox", "INBOX", "--limit", "500", "--json"),
            call("mail", "read", "--mailbox", "INBOX", "uid:34", "--json", "--headers"),
            call("mail", "flag", "--mailbox", "INBOX", "uid:34", "--unread"),
        ]

    async def test_read_mail_rejects_a_uid_missing_from_the_state_scan(self):
        run_command = AsyncMock(return_value=json.dumps({"messages": []}))

        with (
            patch(
                "pynchy.plugins.integrations.proton_mail._run_proton_command",
                new=run_command,
            ),
            pytest.raises(proton_mail.ProtonMailError, match="not found"),
        ):
            await proton_mail._read_mail({"uid": "34"})

        assert run_command.await_count == 1


class TestProtonMailMcpServer:
    async def test_mcp_lists_only_read_only_tools(self):
        client = await _start_mcp_client()
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
