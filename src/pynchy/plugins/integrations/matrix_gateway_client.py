"""Host-only wrapper around the private Matrix gateway binary."""

from __future__ import annotations

import json
import os
import subprocess  # noqa: S404 - command is a host-owned local gateway path.

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

DEFAULT_GATEWAY_COMMAND = "pynchy-matrix-gateway"
_MAX_LIST_LIMIT = 250


class MatrixGatewayError(RuntimeError):
    """A local Matrix gateway command did not complete safely."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MatrixChat(_StrictModel):
    room_id: str = Field(min_length=1)
    name: str = Field(min_length=1)


class MatrixMessage(_StrictModel):
    room_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    sender: str = Field(min_length=1)
    origin_server_ts: int = Field(ge=0)
    body: str


class MatrixSendResult(_StrictModel):
    room_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)


_CHAT_LIST_ADAPTER: TypeAdapter[list[MatrixChat]] = TypeAdapter(list[MatrixChat])
_MESSAGE_ADAPTER: TypeAdapter[MatrixMessage] = TypeAdapter(MatrixMessage)
_SEND_RESULT_ADAPTER: TypeAdapter[MatrixSendResult] = TypeAdapter(MatrixSendResult)


class MatrixGatewayClient:
    """Call the gateway without ever exposing its Matrix session to an agent."""

    def __init__(self, command: str | None = None) -> None:
        self._command = command or os.environ.get("PYNCHY_MATRIX_GATEWAY", DEFAULT_GATEWAY_COMMAND)

    def list_chats(self) -> list[MatrixChat]:
        return _CHAT_LIST_ADAPTER.validate_json(self._run(["chats"]))

    def list_messages(self, *, room_id: str, limit: int) -> list[MatrixMessage]:
        if not 1 <= limit <= _MAX_LIST_LIMIT:
            raise MatrixGatewayError(f"message limit must be 1-{_MAX_LIST_LIMIT}")
        output = self._run(["messages", "--room", room_id, "--limit", str(limit)])
        lines = (line for line in output.splitlines() if line.strip())
        return [_MESSAGE_ADAPTER.validate_json(line) for line in lines]

    def send_message(self, *, room_id: str, body: str) -> MatrixSendResult:
        if not body.strip():
            raise MatrixGatewayError("message body must not be empty")
        output = self._run(["send", "--room", room_id, "--body-stdin"], stdin=body)
        return _SEND_RESULT_ADAPTER.validate_json(output)

    def _run(self, arguments: list[str], *, stdin: str | None = None) -> str:
        try:
            result = subprocess.run(  # noqa: S603 - arguments are fixed by typed host code.
                [self._command, *arguments],
                check=False,
                capture_output=True,
                input=stdin,
                text=True,
                timeout=60,
            )
        except FileNotFoundError as exc:
            raise MatrixGatewayError(
                "Matrix gateway binary is unavailable; configure PYNCHY_MATRIX_GATEWAY"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise MatrixGatewayError("Matrix gateway command timed out") from exc
        if result.returncode:
            raise MatrixGatewayError("Matrix gateway command failed")
        if not result.stdout.strip():
            raise MatrixGatewayError("Matrix gateway command returned no data")
        return result.stdout


def create_matrix_gateway_client() -> MatrixGatewayClient:
    """Create the production client from host-only environment configuration."""
    return MatrixGatewayClient()


def json_result(value: BaseModel | list[BaseModel]) -> str:
    """Serialize validated gateway output for an MCP text result."""
    if isinstance(value, list):
        serialized = [item.model_dump(mode="json") for item in value]
        return json.dumps(serialized, indent=2, sort_keys=True)
    return json.dumps(value.model_dump(mode="json"), indent=2, sort_keys=True)
