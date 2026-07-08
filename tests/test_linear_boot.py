"""Tests for Linear startup board provisioning."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from pynchy.plugins.integrations.linear_boards import LinearWorkspaceBoard
from pynchy.plugins.integrations.linear_boot import (
    create_linear_workspace_todo,
    reconcile_linear_workspace_boards,
)
from pynchy.types import WorkspaceProfile


def _workspace(folder: str, name: str) -> WorkspaceProfile:
    return WorkspaceProfile(
        jid=f"slack:{folder}",
        folder=folder,
        name=name,
        trigger="@Pynchy",
    )


async def test_reconcile_linear_workspace_boards_skips_when_api_key_missing(monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)

    result = await reconcile_linear_workspace_boards([_workspace("alpha", "Alpha")])

    assert result == {}


async def test_reconcile_linear_workspace_boards_uses_env_defaults(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    monkeypatch.setenv("LINEAR_TEAM_KEY", "SYN")
    fake_client = MagicMock()
    fake_board = LinearWorkspaceBoard(
        team={"id": "team-1"},
        project={"id": "project-1"},
        states={},
    )
    reconcile = AsyncMock(return_value={"alpha": fake_board})

    with (
        patch("pynchy.plugins.integrations.linear_boot.LinearClient", return_value=fake_client),
        patch("pynchy.plugins.integrations.linear_boot.reconcile_workspace_boards", reconcile),
    ):
        result = await reconcile_linear_workspace_boards([_workspace("alpha", "Alpha")])

    assert result == {"alpha": reconcile.return_value["alpha"]}
    reconcile.assert_awaited_once()
    _, args, kwargs = reconcile.mock_calls[0]
    assert args[0] is fake_client
    assert [workspace.folder for workspace in args[1]] == ["alpha"]
    assert kwargs["team_key"] == "SYN"


async def test_create_linear_workspace_todo_skips_when_api_key_missing(monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)

    result = await create_linear_workspace_todo(_workspace("alpha", "Alpha"), "Review docs")

    assert result is None


async def test_create_linear_workspace_todo_uses_env_defaults(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    monkeypatch.delenv("LINEAR_TEAM_KEY", raising=False)
    fake_client = MagicMock()
    create_todo = AsyncMock(return_value={"identifier": "SYN-1"})

    with (
        patch("pynchy.plugins.integrations.linear_boot.LinearClient", return_value=fake_client),
        patch("pynchy.plugins.integrations.linear_boot.create_workspace_todo", create_todo),
    ):
        result = await create_linear_workspace_todo(_workspace("alpha", "Alpha"), "Review docs")

    assert result == {"identifier": "SYN-1"}
    create_todo.assert_awaited_once()
    _, args, kwargs = create_todo.mock_calls[0]
    assert args[0] is fake_client
    assert args[1].folder == "alpha"
    assert args[2] == "Review docs"
    assert kwargs["team_key"] is None
