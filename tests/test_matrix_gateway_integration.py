"""Tests for Pynchy's host-only, approval-gated Matrix communications gateway."""

from __future__ import annotations

import json
import subprocess  # noqa: S404 - tests construct CompletedProcess fixtures.
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

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
    """In-memory Matrix gateway substitute used at the host service boundary."""

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


@dataclass(frozen=True)
class StubResolvedWorkspace:
    tools: list[str]


@dataclass(frozen=True)
class StubSettings:
    tools: list[str]

    def resolved_workspace_config(self, workspace_name: str) -> StubResolvedWorkspace | None:
        return StubResolvedWorkspace(self.tools) if workspace_name == "all-my-chats" else None


def _handlers() -> dict[str, object]:
    return matrix_gateway.MatrixGatewayPlugin().pynchy_service_handler()["tools"]


class TestMatrixGatewayPlugin:
    def test_plugin_provides_native_ipc_handlers(self):
        plugin_config = matrix_gateway.MatrixGatewayPlugin().pynchy_service_handler()

        assert set(plugin_config["tools"]) == {
            "matrix_list_chats",
            "matrix_list_messages",
            "matrix_send_message",
        }
        assert plugin_config["read_tools"] == frozenset(
            {"matrix_list_chats", "matrix_list_messages"}
        )

    def test_plugin_is_registered(self):
        plugin = get_plugin_manager().get_plugin("builtin-matrix-gateway")

        assert isinstance(plugin, matrix_gateway.MatrixGatewayPlugin)


class TestMatrixGatewayOperations:
    @pytest.mark.action("chat.matrix.list")
    async def test_list_chats_forwards_to_the_host_only_gateway(self):
        stub = StubMatrixGatewayClient()
        handler = _handlers()["matrix_list_chats"]

        with (
            patch.object(matrix_gateway, "create_matrix_gateway_client", return_value=stub),
            patch.object(
                matrix_gateway,
                "get_settings",
                return_value=StubSettings(["matrix_list_chats"]),
            ),
        ):
            result = await handler({"source_group": "all-my-chats"})

        assert json.loads(result["result"]) == [
            {"name": "Friend", "room_id": "!friend:matrix.example.com"}
        ]
        assert stub.calls == [("list_chats", None)]

    @pytest.mark.action("chat.matrix.message.list")
    async def test_list_messages_requires_an_explicit_room(self):
        stub = StubMatrixGatewayClient()
        handler = _handlers()["matrix_list_messages"]

        with (
            patch.object(matrix_gateway, "create_matrix_gateway_client", return_value=stub),
            patch.object(
                matrix_gateway,
                "get_settings",
                return_value=StubSettings(["matrix_list_messages"]),
            ),
        ):
            result = await handler(
                {
                    "source_group": "all-my-chats",
                    "room_id": "!friend:matrix.example.com",
                    "limit": 3,
                }
            )

        assert json.loads(result["result"])[0]["body"] == "Hello"
        assert stub.calls == [("list_messages", ("!friend:matrix.example.com", 3))]

    @pytest.mark.action("chat.matrix.message.send")
    async def test_send_message_forwards_only_the_final_message_to_the_gateway(self):
        stub = StubMatrixGatewayClient()
        handler = _handlers()["matrix_send_message"]

        with (
            patch.object(matrix_gateway, "create_matrix_gateway_client", return_value=stub),
            patch.object(
                matrix_gateway,
                "get_settings",
                return_value=StubSettings(["matrix_send_message"]),
            ),
        ):
            result = await handler(
                {
                    "source_group": "all-my-chats",
                    "room_id": "!friend:matrix.example.com",
                    "body": "Sounds good.",
                }
            )

        assert json.loads(result["result"]) == {
            "event_id": "$sent",
            "room_id": "!friend:matrix.example.com",
        }
        assert stub.calls == [("send_message", ("!friend:matrix.example.com", "Sounds good."))]

    async def test_send_message_rejects_an_empty_body_before_it_reaches_the_gateway(self):
        stub = StubMatrixGatewayClient()
        handler = _handlers()["matrix_send_message"]

        with (
            patch.object(matrix_gateway, "create_matrix_gateway_client", return_value=stub),
            patch.object(
                matrix_gateway,
                "get_settings",
                return_value=StubSettings(["matrix_send_message"]),
            ),
        ):
            result = await handler(
                {
                    "source_group": "all-my-chats",
                    "room_id": "!friend:matrix.example.com",
                    "body": "  ",
                }
            )

        assert "Invalid Matrix gateway tool arguments" in result["error"]
        assert stub.calls == []

    async def test_gateway_access_is_denied_outside_an_enabled_workspace(self):
        stub = StubMatrixGatewayClient()
        handler = _handlers()["matrix_list_chats"]

        with (
            patch.object(matrix_gateway, "create_matrix_gateway_client", return_value=stub),
            patch.object(matrix_gateway, "get_settings", return_value=StubSettings([])),
        ):
            result = await handler({"source_group": "other-workspace"})

        assert result == {"error": "matrix_list_chats is not enabled for this workspace"}
        assert stub.calls == []


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
