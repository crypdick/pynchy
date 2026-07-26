"""Contract tests for state initialization."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pynchy.state import (
    close_test_database,
    get_all_chats,
    init_database,
)
from pynchy.state.connection import StateRuntimeConfig

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_init_database_uses_explicit_runtime_config(tmp_path: Path) -> None:
    database_path = tmp_path / "explicit" / "messages.db"
    close_test_database()

    try:
        await init_database(StateRuntimeConfig(database_path=database_path))

        assert await get_all_chats() == []
        assert database_path.is_file()
    finally:
        close_test_database()
