"""Public Linear todo pagination failures."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.plugins.integrations.linear_boards import (
    LinearBoardError,
    LinearWorkspaceBoard,
    list_workspace_todos,
)


@dataclass
class _Workspace:
    folder: str = "health"
    name: str = "Health"
    jid: str = "discord:channel:health"


class _Client:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response

    async def query(self, _query: str, **_variables: object) -> dict[str, Any]:
        return self.response


_BOARD = LinearWorkspaceBoard(
    team={"id": "team-1"},
    project={"id": "project-1"},
    states={},
)


@pytest.mark.asyncio
async def test_list_workspace_todos_rejects_missing_page_info():
    client = _Client({"project": {"issues": {"nodes": []}}})

    with (
        patch(
            "pynchy.plugins.integrations.linear_boards.require_workspace_board",
            new=AsyncMock(return_value=_BOARD),
        ),
        pytest.raises(LinearBoardError, match="did not include pageInfo"),
    ):
        await list_workspace_todos(client, _Workspace(), team_key=None)


@pytest.mark.asyncio
async def test_list_workspace_todos_rejects_missing_next_page_cursor():
    client = _Client(
        {
            "project": {
                "issues": {
                    "nodes": [],
                    "pageInfo": {"hasNextPage": True, "endCursor": None},
                }
            }
        }
    )

    with (
        patch(
            "pynchy.plugins.integrations.linear_boards.require_workspace_board",
            new=AsyncMock(return_value=_BOARD),
        ),
        pytest.raises(LinearBoardError, match="did not include a pagination cursor"),
    ):
        await list_workspace_todos(client, _Workspace(), team_key=None)
