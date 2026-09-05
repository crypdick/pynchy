"""Host-only wrapper around the private Matrix gateway binary."""

from __future__ import annotations

import json
import os
import subprocess  # noqa: S404 - command is a host-owned local gateway path.
from pathlib import Path  # noqa: TC003 - beartype resolves gateway path annotations.
from typing import Protocol, runtime_checkable
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from pynchy.process_environment import filtered_process_environment

DEFAULT_GATEWAY_COMMAND = "pynchy-matrix-gateway"
_MAX_LIST_LIMIT = 250


class MatrixGatewayError(RuntimeError):
    """A local Matrix gateway command did not complete safely."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MatrixMessage(_StrictModel):
    room_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    sender: str = Field(min_length=1)
    origin_server_ts: int = Field(ge=0)
    body: str


class MatrixPortalAssertion(_StrictModel):
    """Gateway-verified room identity and optional bridge state."""

    room_id: str = Field(min_length=1)
    owner_user_id: str = Field(min_length=1)
    joined: bool
    bridge: str | None = None
    active_portal: bool | None = None


class MatrixSyncEvent(_StrictModel):
    """Typed live event evidence returned by the host gateway."""

    room_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    sender: str = Field(min_length=1)
    origin_server_ts: int = Field(ge=0)
    event_type: str = Field(min_length=1)
    message_type: str | None = None
    body: str | None = None
    decrypted: bool
    live: bool
    relation_type: str | None = None
    redacted: bool = False


class MatrixSyncBatch(_StrictModel):
    """One provider sync page and its durable continuation token."""

    next_batch: str = Field(min_length=1)
    events: tuple[MatrixSyncEvent, ...] = ()
    rooms: tuple[MatrixPortalAssertion, ...] = ()


class MatrixSendResult(_StrictModel):
    room_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)


@runtime_checkable
class MatrixRouteGateway(Protocol):
    """Route-scoped gateway operations used after host destination binding."""

    def list_messages(self, *, room_id: str, limit: int) -> list[MatrixMessage]: ...

    def room_assertion(self, *, room_id: str) -> MatrixPortalAssertion: ...

    def send_message(self, *, room_id: str, body: str) -> MatrixSendResult: ...


@runtime_checkable
class MatrixConnectionGateway(Protocol):
    """Provider polling operations used by a Matrix connection runtime."""

    def sync(self, *, since: str | None, room_ids: tuple[str, ...]) -> MatrixSyncBatch: ...

    def room_assertion(self, *, room_id: str) -> MatrixPortalAssertion: ...


_MESSAGE_ADAPTER: TypeAdapter[MatrixMessage] = TypeAdapter(MatrixMessage)
_SEND_RESULT_ADAPTER: TypeAdapter[MatrixSendResult] = TypeAdapter(MatrixSendResult)
_SYNC_BATCH_ADAPTER: TypeAdapter[MatrixSyncBatch] = TypeAdapter(MatrixSyncBatch)
_PORTAL_ASSERTION_ADAPTER: TypeAdapter[MatrixPortalAssertion] = TypeAdapter(MatrixPortalAssertion)


def matrix_connection_state_dir(data_dir: Path, connection_name: str) -> Path:
    """Return an absolute, readable, traversal-safe store for one named identity."""
    state_key = f"connection-{quote(connection_name, safe='')}"
    return (data_dir / "matrix-gateway" / state_key).resolve()


class MatrixGatewayClient:
    """Call the gateway without ever exposing its Matrix session to an agent."""

    def __init__(self, command: str | None = None, *, state_dir: Path | None = None) -> None:
        self._command = command or os.environ.get("PYNCHY_MATRIX_GATEWAY", DEFAULT_GATEWAY_COMMAND)
        self._state_dir = state_dir

    def list_messages(self, *, room_id: str, limit: int) -> list[MatrixMessage]:
        if not 1 <= limit <= _MAX_LIST_LIMIT:
            raise MatrixGatewayError(f"message limit must be 1-{_MAX_LIST_LIMIT}")
        output = self._run(
            ["messages", "--room", room_id, "--limit", str(limit)],
            allow_empty=True,
        )
        lines = (line for line in output.splitlines() if line.strip())
        return [_MESSAGE_ADAPTER.validate_json(line) for line in lines]

    def sync(
        self,
        *,
        since: str | None,
        room_ids: tuple[str, ...],
    ) -> MatrixSyncBatch:
        """Fetch one live sync page restricted to configured room identities."""
        request = json.dumps({"since": since, "room_ids": room_ids}, sort_keys=True)
        return _SYNC_BATCH_ADAPTER.validate_json(
            self._run(["sync", "--request-stdin"], stdin=request)
        )

    def room_assertion(self, *, room_id: str) -> MatrixPortalAssertion:
        """Recheck joined identity and bridge assertions before an external send."""
        return _PORTAL_ASSERTION_ADAPTER.validate_json(
            self._run(["room-status", "--room", room_id])
        )

    def send_message(self, *, room_id: str, body: str) -> MatrixSendResult:
        if not body.strip():
            raise MatrixGatewayError("message body must not be empty")
        output = self._run(["send", "--room", room_id, "--body-stdin"], stdin=body)
        return _SEND_RESULT_ADAPTER.validate_json(output)

    def _run(
        self,
        arguments: list[str],
        *,
        stdin: str | None = None,
        allow_empty: bool = False,
    ) -> str:
        environment: dict[str, str] = {}
        if self._state_dir is not None:
            environment["PYNCHY_MATRIX_GATEWAY_DATA_DIR"] = str(self._state_dir)
        try:
            result = subprocess.run(  # noqa: S603 - arguments are fixed by typed host code.
                [self._command, *arguments],
                check=False,
                capture_output=True,
                input=stdin,
                env=filtered_process_environment(environment),
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
            if "MATRIX_GATEWAY_E2EE_KEYS_UNAVAILABLE" in result.stderr:
                raise MatrixGatewayError(
                    "Matrix room history is encrypted and the gateway does not have usable room "
                    "keys. Complete gateway device verification, then retry."
                )
            raise MatrixGatewayError("Matrix gateway command failed")
        if not allow_empty and not result.stdout.strip():
            raise MatrixGatewayError("Matrix gateway command returned no data")
        return result.stdout


def create_matrix_gateway_client(
    command: str | None = None,
    *,
    state_dir: Path | None = None,
) -> MatrixGatewayClient:
    """Create the production client from host-only environment configuration."""
    return MatrixGatewayClient(command, state_dir=state_dir)


def json_result(value: BaseModel | list[BaseModel]) -> str:
    """Serialize validated gateway output for an MCP text result."""
    if isinstance(value, list):
        serialized = [item.model_dump(mode="json") for item in value]
        return json.dumps(serialized, indent=2, sort_keys=True)
    return json.dumps(value.model_dump(mode="json"), indent=2, sort_keys=True)
