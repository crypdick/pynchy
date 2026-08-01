"""Public database schema checks."""

from __future__ import annotations

import aiosqlite
import pytest

from pynchy.state.schema import create_schema


@pytest.mark.asyncio
async def test_schema_rejects_existing_foreign_key_violations() -> None:
    async with aiosqlite.connect(":memory:") as database:
        await create_schema(database)
        await database.execute("PRAGMA foreign_keys = OFF")
        await database.execute(
            "INSERT INTO outbound_ledger (chat_jid, content, timestamp, source) "
            "VALUES (?, ?, ?, ?)",
            ("missing-chat", "content", "now", "test"),
        )
        await database.execute(
            "INSERT INTO outbound_deliveries (ledger_id, channel_name, operation) VALUES (?, ?, ?)",
            (999, "slack", "post"),
        )
        await database.commit()

        with pytest.raises(RuntimeError, match="foreign-key violation remains"):
            await create_schema(database)
