"""Defensive state-shape and claim-race contracts for in-flight turns."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from pynchy.agent_protocol.api import CheckpointControlState, InFlightTurn, InFlightWorkKind
from pynchy.state import (
    complete_in_flight_turn,
    get_in_flight_turn,
    get_in_flight_turn_for_chat,
    get_oldest_resumable_turn_for_group,
    init_test_database,
)

pytest_plugins = ("tests.state_support",)


def _turn() -> InFlightTurn:
    return InFlightTurn(
        turn_id="turn-1",
        chat_jid="group@g.us",
        group_folder="group",
        work_kind=InFlightWorkKind.INTERACTIVE,
        input_messages=[],
        input_start_cursor="",
        input_end_cursor="",
        started_at="2026-07-29T00:00:00+00:00",
        control_state=CheckpointControlState.ACTIVE,
    )


class _Cursor:
    def __init__(self, *, row: dict[str, str] | None = None, rowcount: int = 1) -> None:
        self._row = row
        self.rowcount = rowcount

    async def fetchone(self) -> dict[str, str] | None:
        return self._row


class _Database:
    def __init__(self, *cursors: _Cursor) -> None:
        self._cursors = list(cursors)

    async def execute(self, *_args: object) -> _Cursor:
        return self._cursors.pop(0)


class _AtomicWrite:
    def __init__(self, database: _Database) -> None:
        self.database = database

    async def __aenter__(self) -> _Database:
        return self.database

    async def __aexit__(self, *_args: object) -> bool:
        return False


async def test_empty_work_kind_filters_return_no_turn() -> None:
    await init_test_database()

    assert await get_in_flight_turn_for_chat("group@g.us", set()) is None
    assert await get_oldest_resumable_turn_for_group("group", set()) is None


async def test_malformed_persisted_input_messages_are_rejected() -> None:
    row = {
        "turn_id": "turn-1",
        "chat_jid": "group@g.us",
        "group_folder": "group",
        "work_kind": "interactive",
        "input_messages": "{}",
        "input_start_cursor": "",
        "input_end_cursor": "",
        "started_at": "2026-07-29T00:00:00+00:00",
        "task_id": None,
        "session_id": None,
        "output_sent": 0,
        "interrupted_at": None,
        "deploy_id": None,
        "claimed_at": None,
        "conversation_claim_id": None,
        "input_source": "user",
        "control_state": "active",
    }
    database = _Database(_Cursor(row=row))

    with (
        patch("pynchy.state.in_flight_turns._get_db", return_value=database),
        pytest.raises(TypeError, match="must decode to a list"),
    ):
        await get_in_flight_turn("turn-1")


async def test_completion_fails_when_claim_update_loses_its_row() -> None:
    claimed = {
        "provider": "linear",
        "route": "project",
        "delivery_id": "delivery-1",
        "conversation_id": "conversation-1",
    }
    database = _Database(
        _Cursor(),
        _Cursor(row=claimed),
        _Cursor(rowcount=0),
    )

    with (
        patch("pynchy.state.in_flight_turns.atomic_write", return_value=_AtomicWrite(database)),
        patch(
            "pynchy.state.in_flight_turns._get_in_flight_turn_in_transaction",
            new=AsyncMock(return_value=_turn()),
        ),
        pytest.raises(ValueError, match="claim disappeared before turn completion"),
    ):
        await complete_in_flight_turn("turn-1", conversation_claim_id="claim-1")
