"""Small public edge contracts that should not be left to incidental coverage."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.discord import (
    DiscordChannelSettings,
    DiscordChatTarget,
    DiscordConnectionSettings,
    DiscordGuildSettings,
    discord_chat_ref_error,
)
from pynchy.host.container_manager.ipc.ledger import claim_request_for_execution
from pynchy.host.container_manager.ipc.protocol import IpcRequestEnvelope, make_ipc_request
from pynchy.plugins.channels.discord.api import DiscordChannel
from pynchy.plugins.integrations.linear_board_errors import LinearBoardError
from pynchy.plugins.integrations.linear_board_mutations import apply_workspace_todo_move
from pynchy.plugins.integrations.linear_board_payloads import (
    LinearBoardPayloadError,
    nodes,
    normalize_status,
    payload_entity,
)
from pynchy.plugins.integrations.linear_board_resources import (
    load_team_resources,
    reconcile_workflow_state_position,
)


def test_empty_discord_channel_name_is_rejected():
    channel = DiscordChannel(
        "discord",
        DiscordConnectionSettings(
            bot_token_env="",
            chat={
                "guild": DiscordGuildSettings(
                    channels={"channel": DiscordChannelSettings(name="!!!")}
                )
            },
        ),
        "token",
        lambda *_args: None,
        lambda *_args: None,
        audio_cache_dir=Path("audio"),
    )

    with pytest.raises(ValueError, match="cannot be empty"):
        channel.configured_channel_name(DiscordChatTarget("channel", "channel", "guild"))


@pytest.mark.parametrize(
    ("config", "chat", "message"),
    [
        (DiscordConnectionSettings(bot_token_env=""), "invalid", "must target"),
        (
            DiscordConnectionSettings(bot_token_env="", dm_policy="disabled"),
            "direct.user-1",
            "not allowed",
        ),
        (
            DiscordConnectionSettings(bot_token_env="", group_policy="disabled"),
            "guild.channels.channel",
            "disabled",
        ),
        (
            DiscordConnectionSettings(bot_token_env=""),
            "guild.channels.channel",
            "unknown Discord guild",
        ),
    ],
)
def test_discord_chat_reference_errors_are_explicit(config, chat, message):
    error = discord_chat_ref_error(config, chat)

    assert error is not None
    assert message in error


async def test_nonnumeric_discord_target_without_client_is_unresolved():
    channel = DiscordChannel(
        "discord",
        DiscordConnectionSettings(
            bot_token_env="",
            chat={"guild": DiscordGuildSettings(channels={"not-an-id": DiscordChannelSettings()})},
        ),
        "token",
        lambda *_args: None,
        lambda *_args: None,
        audio_cache_dir=Path("audio"),
    )

    assert await channel.resolve_chat_jid("guild.channels.not-an-id") is None


async def test_nonnumeric_discord_target_stays_unresolved_after_client_lookup():
    channel = DiscordChannel(
        "discord",
        DiscordConnectionSettings(
            bot_token_env="",
            chat={"guild": DiscordGuildSettings(channels={"not-an-id": DiscordChannelSettings()})},
        ),
        "token",
        lambda *_args: None,
        lambda *_args: None,
        audio_cache_dir=Path("audio"),
    )
    channel.client = object()
    channel.find_configured_channel = AsyncMock(return_value=None)  # type: ignore[method-assign]

    assert await channel.resolve_chat_jid("guild.channels.not-an-id") is None
    channel.find_configured_channel.assert_awaited_once()


def test_non_mutating_ipc_request_does_not_create_a_ledger_entry(tmp_path: Path):
    request = make_ipc_request(
        kind="task_status",
        request_id="refresh-1",
        source_group="admin",
        created_at="2026-07-29T00:00:00+00:00",
        payload={},
    )

    assert claim_request_for_execution(IpcRequestEnvelope.from_dict(request), tmp_path) is True
    assert not (tmp_path / "admin" / "request_ledger").exists()


async def test_linear_board_move_fails_when_provider_rejects_update():
    effect = MagicMock()
    effect.fail = AsyncMock()
    client = MagicMock()
    client.query = AsyncMock(
        return_value={"issueUpdate": {"success": False, "issue": {"id": "issue-1"}}}
    )

    @asynccontextmanager
    async def effect_scope(*_args, **_kwargs):
        yield effect

    with (
        patch(
            "pynchy.plugins.integrations.linear_board_mutations.linear_effects.linear_webhook_effect",
            effect_scope,
        ),
        patch(
            "pynchy.plugins.integrations.linear_board_mutations.linear_effects.confirm_issue_state_effect",
            new_callable=AsyncMock,
        ) as confirm,
        pytest.raises(LinearBoardPayloadError, match="did not complete issueUpdate"),
    ):
        await apply_workspace_todo_move(client, issue_id="issue-1", state_id="done")

    effect.fail.assert_awaited_once_with()
    confirm.assert_not_awaited()


def test_linear_board_payload_helpers_fail_closed_on_incomplete_payloads():
    with pytest.raises(LinearBoardPayloadError, match="Unknown todo status"):
        normalize_status("not-a-status")
    with pytest.raises(LinearBoardPayloadError, match="did not include projects"):
        nodes({}, "projects")
    with pytest.raises(LinearBoardPayloadError, match=r"did not include projects.nodes"):
        nodes({"projects": {}}, "projects")
    with pytest.raises(LinearBoardPayloadError, match="did not include project"):
        payload_entity({"projectCreate": {"success": True}}, "projectCreate", "project")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("team", "message"),
    [
        (None, "did not include team"),
        (
            {"projects": {"nodes": []}, "states": {"nodes": []}},
            "did not include pageInfo",
        ),
        (
            {"projects": {"nodes": [], "pageInfo": []}, "states": {"nodes": []}},
            "did not include pageInfo",
        ),
        (
            {"projects": {"nodes": [], "pageInfo": {}}, "states": {"nodes": []}},
            "missing boolean hasNextPage",
        ),
        (
            {
                "projects": {"nodes": [], "pageInfo": {"hasNextPage": True}},
                "states": {"nodes": []},
            },
            "missing endCursor",
        ),
    ],
)
async def test_linear_resource_loading_rejects_incomplete_pagination(
    team: object, message: str
) -> None:
    client = MagicMock()
    client.query = AsyncMock(return_value={"team": team})

    with pytest.raises(LinearBoardError, match=message):
        await load_team_resources(client, "team-1")


@pytest.mark.asyncio
async def test_linear_resource_loading_returns_projects_and_states_without_next_page() -> None:
    client = MagicMock()
    client.query = AsyncMock(
        return_value={
            "team": {
                "projects": {
                    "nodes": [{"id": "project-1", "name": "Project 1"}],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                },
                "states": {"nodes": [{"id": "state-1", "name": "Todo"}]},
            }
        }
    )

    result = await load_team_resources(client, "team-1")

    assert result == {
        "projects": [{"id": "project-1", "name": "Project 1"}],
        "states": [{"id": "state-1", "name": "Todo"}],
    }
    client.query.assert_awaited_once()


@pytest.mark.asyncio
async def test_linear_resource_loading_accumulates_all_project_pages() -> None:
    client = MagicMock()
    client.query = AsyncMock(
        side_effect=[
            {
                "team": {
                    "projects": {
                        "nodes": [{"id": "project-1"}],
                        "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                    },
                    "states": {"nodes": [{"id": "state-1"}]},
                }
            },
            {
                "team": {
                    "projects": {
                        "nodes": [{"id": "project-2"}],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                    "states": {"nodes": [{"id": "state-2"}]},
                }
            },
        ]
    )

    result = await load_team_resources(client, "team-1")

    assert result == {
        "projects": [{"id": "project-1"}, {"id": "project-2"}],
        "states": [{"id": "state-1"}],
    }
    assert client.query.await_args_list[1].kwargs["projects_after"] == "cursor-1"


@pytest.mark.asyncio
async def test_linear_workflow_state_reconciliation_keeps_an_already_positioned_state() -> None:
    client = MagicMock()
    client.query = AsyncMock()
    state = {"id": "state-1", "position": 1.0, "name": "Todo"}

    assert await reconcile_workflow_state_position(client, state, position=1.0) is state
    client.query.assert_not_awaited()


@pytest.mark.asyncio
async def test_linear_workflow_state_reconciliation_returns_provider_update() -> None:
    client = MagicMock()
    client.query = AsyncMock(
        return_value={
            "workflowStateUpdate": {
                "success": True,
                "workflowState": {"id": "state-1", "position": 2.0},
            }
        }
    )

    result = await reconcile_workflow_state_position(
        client, {"id": "state-1", "position": 1.0}, position=2.0
    )

    assert result == {"id": "state-1", "position": 2.0}
    client.query.assert_awaited_once()


@pytest.mark.asyncio
async def test_linear_workflow_state_reconciliation_requires_an_id() -> None:
    class NoQueryClient:
        async def query(self, _query: str, **_variables: object) -> dict[str, object]:
            raise AssertionError("provider query should not run")

    with pytest.raises(LinearBoardError, match="workflow state did not include an ID"):
        await reconcile_workflow_state_position(NoQueryClient(), {}, position=1.0)
