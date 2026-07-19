"""Tests for Linear startup board provisioning."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from conftest import make_settings

from pynchy.config.models import LinearTool, ProfileConfig, WorkspaceConfig
from pynchy.host.orchestrator.workspace_config import dynamic_thread_folder
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
        patch(
            "pynchy.plugins.integrations.linear_boot.get_settings",
            return_value=make_settings(
                profiles={"linear": ProfileConfig(tools=["linear"])},
                workspaces={"alpha": WorkspaceConfig(profiles=["linear"])},
                tools={"linear": LinearTool(type="linear")},
            ),
        ),
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
        patch(
            "pynchy.plugins.integrations.linear_boot.get_settings",
            return_value=make_settings(
                profiles={"linear": ProfileConfig(tools=["linear"])},
                workspaces={"alpha": WorkspaceConfig(profiles=["linear"])},
                tools={"linear": LinearTool(type="linear")},
            ),
        ),
        patch("pynchy.plugins.integrations.linear_boot.LinearClient", return_value=fake_client),
        patch("pynchy.plugins.integrations.linear_boot.create_workspace_todo", create_todo),
    ):
        result = await create_linear_workspace_todo(_workspace("alpha", "Alpha"), "Review docs")

    assert result == {"identifier": "SYN-1"}
    create_todo.assert_awaited_once()
    _, args, kwargs = create_todo.mock_calls[0]
    assert args[0] is fake_client
    assert args[1].folder == "alpha"
    assert args[2].title == "Review docs"
    assert kwargs["team_key"] is None
    assert kwargs["status"] == "ready_for_planning"


async def test_create_linear_workspace_todo_requires_linear_tool_selection(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    create_todo = AsyncMock(return_value={"identifier": "SYN-1"})

    with (
        patch(
            "pynchy.plugins.integrations.linear_boot.get_settings",
            return_value=make_settings(
                profiles={"plain": ProfileConfig(tools=[])},
                workspaces={"alpha": WorkspaceConfig(profiles=["plain"])},
                tools={"linear": LinearTool(type="linear")},
            ),
        ),
        patch("pynchy.plugins.integrations.linear_boot.create_workspace_todo", create_todo),
    ):
        result = await create_linear_workspace_todo(_workspace("alpha", "Alpha"), "Review docs")

    assert result is None
    create_todo.assert_not_called()


async def test_create_linear_workspace_todo_uses_parent_board_for_dynamic_thread(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    fake_client = MagicMock()
    create_todo = AsyncMock(return_value={"identifier": "SYN-1"})
    thread_workspace = _workspace(
        dynamic_thread_folder("admin", "discord:channel:thread"),
        "Admin/thread-1",
    )
    thread_workspace.jid = "discord:channel:thread"

    with (
        patch(
            "pynchy.plugins.integrations.linear_boot.get_settings",
            return_value=make_settings(
                profiles={"linear": ProfileConfig(tools=["linear"])},
                workspaces={"admin": WorkspaceConfig(profiles=["linear"])},
                tools={"linear": LinearTool(type="linear")},
            ),
        ),
        patch("pynchy.plugins.integrations.linear_boot.LinearClient", return_value=fake_client),
        patch("pynchy.plugins.integrations.linear_boot.create_workspace_todo", create_todo),
    ):
        result = await create_linear_workspace_todo(thread_workspace, "Review docs")

    assert result == {"identifier": "SYN-1"}
    _, args, _kwargs = create_todo.mock_calls[0]
    assert args[1].folder == "admin"
    assert args[1].name == "Admin"
    assert args[1].jid == "discord:channel:thread"
