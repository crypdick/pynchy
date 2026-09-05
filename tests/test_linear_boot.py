"""Tests for Linear startup board provisioning."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import configure_linear_accounts_for, make_settings

from pynchy.config.api import LinearTool, ProfileConfig, WorkspaceConfig
from pynchy.conversation.api import dynamic_thread_folder
from pynchy.plugins.integrations.linear_boards import LinearWorkspaceBoard
from pynchy.plugins.integrations.linear_boot import (
    LinearIssueControl,
    configured_linear_workspace_names,
    create_linear_workspace_todo,
    reconcile_linear_workspace_boards,
    workspace_for_linear_project,
)
from pynchy.workspace.api import WorkspaceProfile


@pytest.fixture(autouse=True)
def _configure_linear_boot() -> None:
    configure_linear_accounts_for(make_settings())


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
    settings = make_settings(
        profiles={"linear": ProfileConfig(tools=["linear"])},
        workspaces={"alpha": WorkspaceConfig(profiles=["linear"])},
        tools={"linear": LinearTool(type="linear")},
    )
    configure_linear_accounts_for(settings)

    with (
        patch("pynchy.plugins.integrations.linear_boot.LinearClient", return_value=fake_client),
        patch("pynchy.plugins.integrations.linear_boot.reconcile_workspace_boards", reconcile),
    ):
        result = await reconcile_linear_workspace_boards([_workspace("alpha", "Alpha")])

    assert result == {"alpha": reconcile.return_value["alpha"]}
    assert workspace_for_linear_project("project-1") == "alpha"
    assert workspace_for_linear_project("missing-project") is None
    reconcile.assert_awaited_once()
    _, args, kwargs = reconcile.mock_calls[0]
    assert args[0] is fake_client
    assert [workspace.folder for workspace in args[1]] == ["alpha"]
    assert kwargs["team_key"] == "SYN"


async def test_reconcile_prefers_workspace_root_over_registered_thread(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    settings = make_settings(
        profiles={"linear": ProfileConfig(tools=["linear"])},
        workspaces={"health": WorkspaceConfig(profiles=["linear"])},
        tools={"linear": LinearTool(type="linear")},
    )
    configure_linear_accounts_for(settings)
    thread = _workspace(
        dynamic_thread_folder("health", "discord:channel:thread"),
        "Health/body-checkins",
    )
    thread.jid = "discord:channel:thread"
    root = _workspace("health", "Health")
    root.jid = "discord:channel:forum"
    reconcile = AsyncMock(return_value={})

    with patch(
        "pynchy.plugins.integrations.linear_boot.reconcile_workspace_boards",
        reconcile,
    ):
        await reconcile_linear_workspace_boards([thread, root])

    _, args, _ = reconcile.mock_calls[0]
    assert args[1][0].jid == root.jid


async def test_reconcile_deduplicates_dynamic_threads_with_one_parent(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    settings = make_settings(
        profiles={"linear": ProfileConfig(tools=["linear"])},
        workspaces={"health": WorkspaceConfig(profiles=["linear"])},
        tools={"linear": LinearTool(type="linear")},
    )
    configure_linear_accounts_for(settings)
    first = _workspace(
        dynamic_thread_folder("health", "discord:channel:first"),
        "Health/first",
    )
    second = _workspace(
        dynamic_thread_folder("health", "discord:channel:second"),
        "Health/second",
    )
    reconcile = AsyncMock(return_value={})

    with patch(
        "pynchy.plugins.integrations.linear_boot.reconcile_workspace_boards",
        reconcile,
    ):
        await reconcile_linear_workspace_boards([first, second])

    _, args, _ = reconcile.mock_calls[0]
    assert [workspace.jid for workspace in args[1]] == [first.jid]


def test_configured_workspace_names_skip_workspaces_without_linear_account():
    settings = make_settings(
        profiles={"linear": ProfileConfig(tools=["linear"])},
        workspaces={
            "alpha": WorkspaceConfig(profiles=["linear"]),
            "unconfigured": WorkspaceConfig(),
        },
        tools={"linear": LinearTool(type="linear")},
    )
    configure_linear_accounts_for(settings)

    assert configured_linear_workspace_names("linear") == ()


async def test_reconcile_materializes_active_issue_controls(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    settings = make_settings(
        profiles={"linear": ProfileConfig(tools=["linear"])},
        workspaces={"health": WorkspaceConfig(profiles=["linear"])},
        tools={"linear": LinearTool(type="linear")},
    )
    configure_linear_accounts_for(settings)
    root = _workspace("health", "Health")
    root.jid = "discord:channel:forum"
    ensure_control = AsyncMock()

    with (
        patch(
            "pynchy.plugins.integrations.linear_boot.reconcile_workspace_boards",
            AsyncMock(return_value={}),
        ),
        patch(
            "pynchy.plugins.integrations.linear_boot.list_workspace_todos",
            AsyncMock(
                return_value=[
                    {
                        "id": "issue-1",
                        "identifier": "SYN-1",
                        "title": "Restore sleep access",
                        "url": "https://linear.app/acme/issue/SYN-1",
                        "updatedAt": "2026-07-31T09:00:00Z",
                    }
                ]
            ),
        ),
    ):
        await reconcile_linear_workspace_boards(
            [root],
            ensure_issue_control=ensure_control,
        )

    ensure_control.assert_awaited_once_with(
        LinearIssueControl(
            issue_id="issue-1",
            workspace="health",
            parent_jid=root.jid,
            account_name="linear",
            title="[SYN-1] Restore sleep access",
            url="https://linear.app/acme/issue/SYN-1",
            updated_at="2026-07-31T09:00:00Z",
        )
    )


async def test_reconcile_skips_malformed_issue_and_keeps_valid_sibling(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    settings = make_settings(
        profiles={"linear": ProfileConfig(tools=["linear"])},
        workspaces={"health": WorkspaceConfig(profiles=["linear"])},
        tools={"linear": LinearTool(type="linear")},
    )
    configure_linear_accounts_for(settings)
    root = _workspace("health", "Health")
    ensure_control = AsyncMock()

    with (
        patch(
            "pynchy.plugins.integrations.linear_boot.reconcile_workspace_boards",
            AsyncMock(return_value={}),
        ),
        patch(
            "pynchy.plugins.integrations.linear_boot.list_workspace_todos",
            AsyncMock(
                return_value=[
                    {"identifier": "SYN-bad"},
                    {
                        "id": "issue-1",
                        "identifier": "SYN-1",
                        "title": "Keep this issue",
                        "url": "https://linear.app/acme/issue/SYN-1",
                        "updatedAt": "2026-07-31T09:00:00Z",
                    },
                ]
            ),
        ),
    ):
        await reconcile_linear_workspace_boards([root], ensure_issue_control=ensure_control)

    ensure_control.assert_awaited_once()
    assert ensure_control.await_args.args[0].issue_id == "issue-1"


async def test_reconcile_continues_after_issue_control_failure(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    settings = make_settings(
        profiles={"linear": ProfileConfig(tools=["linear"])},
        workspaces={"health": WorkspaceConfig(profiles=["linear"])},
        tools={"linear": LinearTool(type="linear")},
    )
    configure_linear_accounts_for(settings)
    root = _workspace("health", "Health")
    ensure_control = AsyncMock(side_effect=[RuntimeError("control unavailable"), None])
    issues = [
        {
            "id": f"issue-{index}",
            "identifier": f"SYN-{index}",
            "title": f"Issue {index}",
            "url": f"https://linear.app/acme/issue/SYN-{index}",
            "updatedAt": "2026-07-31T09:00:00Z",
        }
        for index in (1, 2)
    ]

    with (
        patch(
            "pynchy.plugins.integrations.linear_boot.reconcile_workspace_boards",
            AsyncMock(return_value={}),
        ),
        patch(
            "pynchy.plugins.integrations.linear_boot.list_workspace_todos",
            AsyncMock(return_value=issues),
        ),
    ):
        await reconcile_linear_workspace_boards([root], ensure_issue_control=ensure_control)

    assert ensure_control.await_count == 2


async def test_reconcile_continues_when_issue_discovery_fails(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    settings = make_settings(
        profiles={"linear": ProfileConfig(tools=["linear"])},
        workspaces={"health": WorkspaceConfig(profiles=["linear"])},
        tools={"linear": LinearTool(type="linear")},
    )
    configure_linear_accounts_for(settings)
    root = _workspace("health", "Health")
    ensure_control = AsyncMock()

    with (
        patch(
            "pynchy.plugins.integrations.linear_boot.reconcile_workspace_boards",
            AsyncMock(return_value={}),
        ),
        patch(
            "pynchy.plugins.integrations.linear_boot.list_workspace_todos",
            AsyncMock(side_effect=RuntimeError("Linear unavailable")),
        ),
    ):
        await reconcile_linear_workspace_boards([root], ensure_issue_control=ensure_control)

    ensure_control.assert_not_awaited()


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
    configure_linear_accounts_for(settings)
    with (
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


async def test_reconcile_ignores_one_account_failure(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    settings = make_settings(
        profiles={"linear": ProfileConfig(tools=["linear"])},
        workspaces={"alpha": WorkspaceConfig(profiles=["linear"])},
        tools={"linear": LinearTool(type="linear")},
    )
    configure_linear_accounts_for(settings)

    with patch(
        "pynchy.plugins.integrations.linear_boot.reconcile_workspace_boards",
        new=AsyncMock(side_effect=RuntimeError("provider unavailable")),
    ):
        assert await reconcile_linear_workspace_boards([_workspace("alpha", "Alpha")]) == {}


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

    configure_linear_accounts_for(settings)
    await reconcile_linear_workspace_boards([voice, project])
    candidates = configured_linear_workspace_names("linear")

    assert candidates == ("project",)


async def test_create_linear_workspace_todo_skips_when_api_key_missing(monkeypatch):
    settings = make_settings(
        profiles={"linear": ProfileConfig(tools=["linear"])},
        workspaces={"alpha": WorkspaceConfig(profiles=["linear"])},
        tools={"linear": LinearTool(type="linear")},
    )
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    configure_linear_accounts_for(settings)
    monkeypatch.delenv("LINEAR_API_KEY")

    result = await create_linear_workspace_todo(_workspace("alpha", "Alpha"), "Review docs")

    assert result is None


async def test_create_linear_workspace_todo_ignores_provider_failure(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    settings = make_settings(
        profiles={"linear": ProfileConfig(tools=["linear"])},
        workspaces={"alpha": WorkspaceConfig(profiles=["linear"])},
        tools={"linear": LinearTool(type="linear")},
    )
    configure_linear_accounts_for(settings)

    with patch(
        "pynchy.plugins.integrations.linear_boot.create_workspace_todo",
        new=AsyncMock(side_effect=RuntimeError("provider unavailable")),
    ):
        result = await create_linear_workspace_todo(_workspace("alpha", "Alpha"), "Review docs")

    assert result is None


async def test_create_linear_workspace_todo_uses_env_defaults(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    monkeypatch.delenv("LINEAR_TEAM_KEY", raising=False)
    fake_client = MagicMock()
    create_todo = AsyncMock(return_value={"identifier": "SYN-1"})
    settings = make_settings(
        profiles={"linear": ProfileConfig(tools=["linear"])},
        workspaces={"alpha": WorkspaceConfig(profiles=["linear"])},
        tools={"linear": LinearTool(type="linear")},
    )
    configure_linear_accounts_for(settings)

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
    assert args[2].title == "Review docs"
    assert kwargs["team_key"] is None
    assert "status" not in kwargs


async def test_create_linear_workspace_todo_requires_linear_tool_selection(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_test")
    create_todo = AsyncMock(return_value={"identifier": "SYN-1"})
    settings = make_settings(
        profiles={"plain": ProfileConfig(tools=[])},
        workspaces={"alpha": WorkspaceConfig(profiles=["plain"])},
        tools={"linear": LinearTool(type="linear")},
    )
    configure_linear_accounts_for(settings)

    with (
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
    settings = make_settings(
        profiles={"linear": ProfileConfig(tools=["linear"])},
        workspaces={"admin": WorkspaceConfig(profiles=["linear"])},
        tools={"linear": LinearTool(type="linear")},
    )
    configure_linear_accounts_for(settings)

    with (
        patch("pynchy.plugins.integrations.linear_boot.LinearClient", return_value=fake_client),
        patch("pynchy.plugins.integrations.linear_boot.create_workspace_todo", create_todo),
    ):
        result = await create_linear_workspace_todo(thread_workspace, "Review docs")

    assert result == {"identifier": "SYN-1"}
    _, args, _kwargs = create_todo.mock_calls[0]
    assert args[1].folder == "admin"
    assert args[1].name == "Admin"
    assert args[1].jid == "discord:channel:thread"
