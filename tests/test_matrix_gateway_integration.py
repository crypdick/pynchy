"""Tests for the host-only, approval-gated Matrix communications gateway."""

from __future__ import annotations

import json
import subprocess  # noqa: S404 - tests construct CompletedProcess fixtures.
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from pynchy.plugins import get_plugin_manager
from pynchy.plugins.integrations import matrix_gateway
from pynchy.plugins.integrations.matrix_gateway_client import (
    MatrixChat,
    MatrixGatewayClient,
    MatrixGatewayError,
    MatrixMessage,
    MatrixSendResult,
)


@dataclass
class StubMatrixGatewayClient:
    """In-memory Matrix gateway substitute used at the MCP boundary."""

    calls: list[tuple[str, object]] = field(default_factory=list)

    def list_chats(self) -> list[MatrixChat]:
        self.calls.append(("list_chats", None))
        return [MatrixChat(room_id="!friend:matrix.example.com", name="Friend")]

    def list_messages(self, *, room_id: str, limit: int) -> list[MatrixMessage]:
        self.calls.append(("list_messages", (room_id, limit)))
        return [
            MatrixMessage(
                room_id=room_id,
                event_id="$message",
                sender="@friend:matrix.example.com",
                origin_server_ts=1,
                body="Hello",
            )
        ]

    def send_message(self, *, room_id: str, body: str) -> MatrixSendResult:
        self.calls.append(("send_message", (room_id, body)))
        return MatrixSendResult(room_id=room_id, event_id="$sent")


async def _start_mcp_client(stub: StubMatrixGatewayClient) -> TestClient:
    client = TestClient(TestServer(matrix_gateway.build_app(lambda: stub)))
    await client.start_server()
    return client


class TestMatrixGatewayMcpPlugin:
    def test_plugin_provides_a_host_only_approval_gated_mcp_server(self):
        spec = matrix_gateway.MatrixGatewayMcpPlugin().pynchy_mcp_server_spec()

        assert spec["name"] == "matrix-gateway"
        assert spec["port"] == 8476
        assert spec["trust"] == {
            "public_source": True,
            "secret_data": True,
            "public_sink": True,
            "dangerous_writes": True,
        }

    def test_plugin_is_registered(self):
        plugin = get_plugin_manager().get_plugin("builtin-matrix-gateway")

        assert isinstance(plugin, matrix_gateway.MatrixGatewayMcpPlugin)


class TestMatrixGatewayOperations:
    @pytest.mark.action("chat.matrix.list")
    async def test_list_chats_forwards_to_the_host_only_gateway(self):
        stub = StubMatrixGatewayClient()

        result = await matrix_gateway._call_tool(
            {"name": "matrix_list_chats", "arguments": {}},
            lambda: stub,
        )

        assert json.loads(result["content"][0]["text"]) == [
            {"name": "Friend", "room_id": "!friend:matrix.example.com"}
        ]
        assert stub.calls == [("list_chats", None)]

    @pytest.mark.action("chat.matrix.message.list")
    async def test_list_messages_requires_an_explicit_room(self):
        stub = StubMatrixGatewayClient()

        result = await matrix_gateway._call_tool(
            {
                "name": "matrix_list_messages",
                "arguments": {"room_id": "!friend:matrix.example.com", "limit": 3},
            },
            lambda: stub,
        )

        assert json.loads(result["content"][0]["text"])[0]["body"] == "Hello"
        assert stub.calls == [("list_messages", ("!friend:matrix.example.com", 3))]

    @pytest.mark.action("chat.matrix.message.send")
    async def test_send_message_forwards_only_the_final_message_to_the_gateway(self):
        stub = StubMatrixGatewayClient()

        result = await matrix_gateway._call_tool(
            {
                "name": "matrix_send_message",
                "arguments": {"room_id": "!friend:matrix.example.com", "body": "Sounds good."},
            },
            lambda: stub,
        )

        assert json.loads(result["content"][0]["text"]) == {
            "event_id": "$sent",
            "room_id": "!friend:matrix.example.com",
        }
        assert stub.calls == [("send_message", ("!friend:matrix.example.com", "Sounds good."))]

    async def test_send_message_rejects_an_empty_body_before_it_reaches_the_gateway(self):
        with pytest.raises(MatrixGatewayError, match="Invalid Matrix gateway tool arguments"):
            await matrix_gateway._call_tool(
                {
                    "name": "matrix_send_message",
                    "arguments": {"room_id": "!friend:matrix.example.com", "body": "  "},
                },
                StubMatrixGatewayClient,
            )


class TestMatrixGatewayMcpServer:
    async def test_mcp_lists_the_read_and_approval_gated_send_tools(self):
        stub = StubMatrixGatewayClient()
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
        assert names == {"matrix_list_chats", "matrix_list_messages", "matrix_send_message"}

    @pytest.mark.action("chat.matrix.message.send")
    async def test_mcp_sends_as_the_gateway_owner_after_the_approval_boundary(self):
        stub = StubMatrixGatewayClient()
        client = await _start_mcp_client(stub)
        try:
            response = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "matrix_send_message",
                        "arguments": {
                            "room_id": "!friend:matrix.example.com",
                            "body": "Approved reply",
                        },
                    },
                },
            )
            payload = await response.json()
        finally:
            await client.close()

        assert json.loads(payload["result"]["content"][0]["text"])["event_id"] == "$sent"
        assert stub.calls == [("send_message", ("!friend:matrix.example.com", "Approved reply"))]


class TestMatrixGatewayClient:
    def test_send_uses_stdin_and_never_places_the_body_in_command_arguments(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"room_id":"!friend:matrix.example.com","event_id":"$sent"}',
            stderr="",
        )
        client = MatrixGatewayClient(command="gateway")

        with patch(
            "pynchy.plugins.integrations.matrix_gateway_client.subprocess.run",
            return_value=completed,
        ) as run:
            result = client.send_message(room_id="!friend:matrix.example.com", body="Private reply")

        assert result.event_id == "$sent"
        command = run.call_args.args[0]
        assert command == [
            "gateway",
            "send",
            "--room",
            "!friend:matrix.example.com",
            "--body-stdin",
        ]
        assert run.call_args.kwargs["input"] == "Private reply"

    def test_failed_gateway_command_does_not_return_stderr_to_the_agent(self):
        completed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="private")
        client = MatrixGatewayClient(command="gateway")

        with (
            patch(
                "pynchy.plugins.integrations.matrix_gateway_client.subprocess.run",
                return_value=completed,
            ),
            pytest.raises(MatrixGatewayError, match="Matrix gateway command failed"),
        ):
            client.list_chats()
