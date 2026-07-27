"""Tests for Linear startup board provisioning."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from conftest import make_settings

from pynchy.config.models import LinearTool, ProfileConfig, WorkspaceConfig
from pynchy.host.orchestrator.workspace_config import dynamic_thread_folder
from pynchy.plugins.integrations.linear_boards import LinearWorkspaceBoard
from pynchy.plugins.integrations.linear_boot import (
    configured_linear_workspace_names,
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


async def test_reconcile_groups_workspaces_by_named_account_credentials(monkeypatch):
    monkeypatch.setenv("LINEAR_PUBLIC_KEY", "lin_public")
    monkeypatch.setenv("LINEAR_PUBLIC_TEAM", "PUB")
    monkeypatch.setenv("LINEAR_SYNAPSE_KEY", "lin_synapse")
    monkeypatch.setenv("LINEAR_SYNAPSE_TEAM", "SYN")
    fake_board = LinearWorkspaceBoard(
        team={"id": "team-1"},
        project={"id": "project-1"},
        states={},
    )

    def reconcile(_client, workspaces, *, team_key):
        assert team_key in {"PUB", "SYN"}
        return {workspace.folder: fake_board for workspace in workspaces}

    settings = make_settings(
        profiles={
            "public": ProfileConfig(tools=["linear_public"]),
            "synapse": ProfileConfig(tools=["linear_synapse"]),
        },
        workspaces={
            "public-board": WorkspaceConfig(profiles=["public"]),
            "synapse-board": WorkspaceConfig(profiles=["synapse"]),
        },
        tools={
            "linear_public": LinearTool(
                type="linear",
                required_env=["LINEAR_PUBLIC_KEY"],  # pragma: allowlist secret
                optional_env=["LINEAR_PUBLIC_TEAM"],
            ),
            "linear_synapse": LinearTool(
                type="linear",
                required_env=["LINEAR_SYNAPSE_KEY"],  # pragma: allowlist secret
                optional_env=["LINEAR_SYNAPSE_TEAM"],
            ),
        },
    )
    with (
        patch("pynchy.plugins.integrations.linear_boot.get_settings", return_value=settings),
        patch("pynchy.plugins.integrations.linear_boot.LinearClient") as client_class,
        patch(
            "pynchy.plugins.integrations.linear_boot.reconcile_workspace_boards",
            new=AsyncMock(side_effect=reconcile),
        ) as reconcile_boards,
    ):
        result = await reconcile_linear_workspace_boards(
            [
                _workspace("public-board", "Public"),
                _workspace("synapse-board", "Synapse"),
            ]
        )

    assert set(result) == {"public-board", "synapse-board"}
    assert [call.kwargs["api_key"] for call in client_class.call_args_list] == [
        "lin_public",
        "lin_synapse",
    ]
    assert reconcile_boards.await_count == 2


async def test_project_routes_only_admit_discord_thread_parents(monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    settings = make_settings(
        profiles={"linear": ProfileConfig(tools=["linear"])},
        workspaces={
            "general-voice": WorkspaceConfig(profiles=["linear"]),
            "project": WorkspaceConfig(profiles=["linear"]),
        },
        tools={"linear": LinearTool(type="linear")},
    )
    voice = _workspace("general-voice", "General voice")
    voice.jid = "discord:voice:general"
    project = _workspace("project", "Project")
    project.jid = "discord:channel:project"

    with patch("pynchy.plugins.integrations.linear_boot.get_settings", return_value=settings):
        await reconcile_linear_workspace_boards([voice, project])
        candidates = configured_linear_workspace_names("linear")

    assert candidates == ("project",)


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
    assert "status" not in kwargs


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
