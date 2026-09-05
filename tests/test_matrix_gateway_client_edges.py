"""Public contract tests for the host-only Matrix gateway client."""

from __future__ import annotations

import json
import subprocess  # noqa: S404 - tests construct completed local process fixtures.
from unittest.mock import patch

import pytest

from pynchy.plugins.integrations.matrix_gateway_client import (
    MatrixGatewayClient,
    MatrixGatewayError,
    MatrixMessage,
    json_result,
)

_ROOM = "!family:matrix.example.com"


def _completed(
    stdout: str,
    *,
    returncode: int = 0,
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["gateway"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_client_decodes_gateway_commands_and_preserves_request_boundary() -> None:
    sync = json.dumps({"next_batch": "cursor-2", "events": [], "rooms": []})
    sent = json.dumps({"room_id": _ROOM, "event_id": "$sent"})

    with patch(
        "pynchy.plugins.integrations.matrix_gateway_client.subprocess.run",
        side_effect=[_completed(sync), _completed(sent)],
    ) as run:
        client = MatrixGatewayClient("gateway")
        assert client.sync(since="cursor-1", room_ids=(_ROOM,)).next_batch == "cursor-2"
        assert client.send_message(room_id=_ROOM, body="hello").event_id == "$sent"

    assert run.call_args_list[0].kwargs["input"] == json.dumps(
        {"room_ids": (_ROOM,), "since": "cursor-1"}, sort_keys=True
    )
    assert run.call_args_list[1].kwargs["input"] == "hello"


@pytest.mark.parametrize("limit", [0, 251])
def test_client_rejects_message_limits_outside_gateway_contract(limit: int) -> None:
    with pytest.raises(MatrixGatewayError, match="message limit must be 1-250"):
        MatrixGatewayClient("gateway").list_messages(room_id=_ROOM, limit=limit)


def test_client_rejects_empty_outbound_messages() -> None:
    with pytest.raises(MatrixGatewayError, match="message body must not be empty"):
        MatrixGatewayClient("gateway").send_message(room_id=_ROOM, body=" \n\t")


def test_client_reports_missing_binary_and_timeout() -> None:
    client = MatrixGatewayClient("gateway")
    with (
        patch(
            "pynchy.plugins.integrations.matrix_gateway_client.subprocess.run",
            side_effect=FileNotFoundError,
        ),
        pytest.raises(MatrixGatewayError, match="binary is unavailable"),
    ):
        client.send_message(room_id=_ROOM, body="hello")

    with (
        patch(
            "pynchy.plugins.integrations.matrix_gateway_client.subprocess.run",
            side_effect=subprocess.TimeoutExpired("gateway", 60),
        ),
        pytest.raises(MatrixGatewayError, match="command timed out"),
    ):
        client.send_message(room_id=_ROOM, body="hello")


def test_client_requires_output_for_commands_that_return_structured_data() -> None:
    with (
        patch(
            "pynchy.plugins.integrations.matrix_gateway_client.subprocess.run",
            return_value=_completed("\n"),
        ),
        pytest.raises(MatrixGatewayError, match="returned no data"),
    ):
        MatrixGatewayClient("gateway").send_message(room_id=_ROOM, body="hello")


def test_json_result_serializes_one_model_or_a_model_list() -> None:
    message = MatrixMessage(
        room_id=_ROOM,
        event_id="$event",
        sender="@friend:matrix.example.com",
        origin_server_ts=1,
        body="hello",
    )
    assert json.loads(json_result(message))["event_id"] == "$event"
    assert json.loads(json_result([message])) == [message.model_dump(mode="json")]
