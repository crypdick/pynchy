"""Failure and metadata contracts for consumed host-control messages."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from pynchy.plugins.api import NewMessage
from pynchy.state import (
    get_chat_history,
    get_messages_since,
    init_test_database,
    mark_message_as_host,
    store_message,
)

pytest_plugins = ("tests.state_support",)


class _Cursor:
    def __init__(self, *, row: dict[str, str] | None = None, rowcount: int = 0) -> None:
        self._row = row
        self.rowcount = rowcount

    async def fetchone(self) -> dict[str, str] | None:
        return self._row


class _Database:
    def __init__(self, *cursors: _Cursor, rows: list[dict[str, str]] | None = None) -> None:
        self._cursors = list(cursors)
        self._rows = rows or []

    async def execute(self, *_args: object) -> _Cursor | _Database:
        if self._cursors:
            return self._cursors.pop(0)
        return self

    async def fetchall(self) -> list[dict[str, str]]:
        return self._rows


class _AtomicWrite:
    def __init__(self, database: _Database) -> None:
        self.database = database

    async def __aenter__(self) -> _Database:
        return self.database

    async def __aexit__(self, *_args: object) -> bool:
        return False


def _message(message_id: str = "control-1") -> NewMessage:
    return NewMessage(
        id=message_id,
        chat_jid="group@g.us",
        sender="alice",
        sender_name="Alice",
        content="pause",
        timestamp="2026-07-29T00:00:00+00:00",
        is_from_me=False,
        metadata={"source": "slack"},
    )


async def test_mark_message_as_host_rejects_missing_message() -> None:
    await init_test_database()

    with pytest.raises(ValueError, match="disappeared"):
        await mark_message_as_host("missing", "group@g.us")


async def test_mark_message_as_host_rejects_malformed_metadata() -> None:
    await init_test_database()
    await store_message(_message())

    with (
        patch("pynchy.state.messages.json.loads", return_value=[]),
        pytest.raises(TypeError, match="invalid persisted shape"),
    ):
        await mark_message_as_host("control-1", "group@g.us")


async def test_mark_message_as_host_records_deferred_control() -> None:
    await init_test_database()
    await store_message(_message())

    await mark_message_as_host("control-1", "group@g.us", deferred_control=True)

    stored = await get_chat_history("group@g.us")
    assert stored[0].message_type == "host"
    assert stored[0].metadata == {"source": "slack", "deferred_host_control": True}


async def test_mark_message_as_host_fails_when_update_loses_its_row() -> None:
    database = _Database(
        _Cursor(row={"metadata": "{}"}),
        _Cursor(rowcount=0),
    )

    with (
        patch("pynchy.state.messages.atomic_write", return_value=_AtomicWrite(database)),
        pytest.raises(ValueError, match="disappeared"),
    ):
        await mark_message_as_host("control-1", "group@g.us")


async def test_legacy_message_without_sender_flag_defaults_to_unknown() -> None:
    database = _Database(
        rows=[
            {
                "id": "legacy-message",
                "chat_jid": "group@g.us",
                "sender": "alice",
                "sender_name": "Alice",
                "content": "hello",
                "timestamp": "2026-07-29T00:00:00+00:00",
                "message_type": "user",
                "metadata": "{}",
            }
        ]
    )

    with patch("pynchy.state.messages._get_db", return_value=database):
        messages = await get_messages_since("group@g.us", None)

    assert messages[0].is_from_me is None
