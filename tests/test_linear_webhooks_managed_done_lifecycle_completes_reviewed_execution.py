"""Behavioral coverage for authenticated Linear issue-conversation admission."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from conftest import configure_linear_accounts_for, make_settings
from linear_webhook_test_support import (
    DELIVERY_ID as _DELIVERY_ID,
)
from linear_webhook_test_support import (
    SIGNING_KEY as _SIGNING_KEY,
)
from linear_webhook_test_support import (
    LinearWebhookHarness as _WebhookDeps,
)
from linear_webhook_test_support import (
    payload as _payload,
)
from linear_webhook_test_support import (
    route_config as _config,
)
from linear_webhook_test_support import (
    signed_request as _signed_request,
)

from pynchy.config.api import LinearTool, PluginConfig, ProfileConfig, WorkspaceConfig
from pynchy.conversation.models import (
    ConversationClaimId,
    ConversationId,
    ConversationLifecycleFence,
    ExternalDeliveryId,
    ExternalDeliveryIdentity,
    ExternalProvider,
    ExternalRoute,
)
from pynchy.identifiers import (
    GroupFolder,
)
from pynchy.plugins.api import (
    WebhookLifecycleDelivery,
)
from pynchy.plugins.integrations.linear import LinearMcpPlugin
from pynchy.plugins.integrations.linear_boards import LinearWorkspaceBoard
from pynchy.plugins.integrations.linear_webhook_effects import (
    process_linear_webhook_event,
    process_linear_webhook_lifecycle,
)
from pynchy.plugins.integrations.linear_webhooks import (
    parse_linear_webhook,
)
from pynchy.work_items.api import (
    WorkItemExecutionStatus,
)
from pynchy.workspace.api import WorkspaceProfile
from tests.linear_webhooks_support import (
    _LeaseResult,
    _linear_client_context,
)

pytest_plugins = ("tests.linear_webhooks_support",)


async def test_managed_done_lifecycle_completes_reviewed_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    complete = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects.complete_reviewed_work_item",
        complete,
    )
    await process_linear_webhook_lifecycle(
        WebhookLifecycleDelivery(
            identity=ExternalDeliveryIdentity(
                provider=ExternalProvider("linear"),
                route=ExternalRoute("project"),
                delivery_id=ExternalDeliveryId(_DELIVERY_ID),
            ),
            conversation_id=ConversationId("conversation-1"),
            subject_id="issue-1",
            workspace=GroupFolder("project"),
            context={
                "linear_state_id": "state-done",
                "linear_managed_done_state_id": "state-done",
            },
        )
    )

    complete.assert_awaited_once_with(
        "project",
        "issue-1",
        _DELIVERY_ID,
        controller_workspace="project",
    )


async def test_managed_done_lifecycle_forwards_current_fence_to_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    complete = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects.complete_reviewed_work_item",
        complete,
    )
    identity = ExternalDeliveryIdentity(
        provider=ExternalProvider("linear"),
        route=ExternalRoute("project"),
        delivery_id=ExternalDeliveryId(_DELIVERY_ID),
    )
    fence = ConversationLifecycleFence(
        conversation_id=ConversationId("conversation-1"),
        identity=identity,
        claim_id=ConversationClaimId("claim-1"),
        control_state_revision="2026-07-27T00:00:01+00:00",
    )

    await process_linear_webhook_lifecycle(
        WebhookLifecycleDelivery(
            identity=identity,
            conversation_id=ConversationId("conversation-1"),
            subject_id="issue-1",
            workspace=GroupFolder("runtime"),
            context={
                "linear_state_id": "state-done",
                "linear_managed_done_state_id": "state-done",
                "linear_controller_workspace": "controller",
            },
            lifecycle_fence=fence,
        )
    )

    complete.assert_awaited_once_with(
        "runtime",
        "issue-1",
        _DELIVERY_ID,
        lifecycle_fence=fence,
        controller_workspace="controller",
    )


@pytest.mark.parametrize("terminal_state_id", ["state-duplicate", "state-custom-canceled"])
async def test_non_managed_terminal_lifecycle_does_not_complete_reviewed_execution(
    monkeypatch: pytest.MonkeyPatch,
    terminal_state_id: str,
) -> None:
    complete = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects.complete_reviewed_work_item",
        complete,
    )
    await process_linear_webhook_lifecycle(
        WebhookLifecycleDelivery(
            identity=ExternalDeliveryIdentity(
                provider=ExternalProvider("linear"),
                route=ExternalRoute("project"),
                delivery_id=ExternalDeliveryId(_DELIVERY_ID),
            ),
            conversation_id=ConversationId("conversation-1"),
            subject_id="issue-1",
            workspace=GroupFolder("project"),
            context={
                "linear_state_id": terminal_state_id,
                "linear_managed_done_state_id": "state-done",
            },
        )
    )

    complete.assert_not_awaited()


async def test_human_approved_issue_triggers_controller_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    raw_body, headers = _signed_request(
        _payload(
            now=now,
            event_type="Issue",
            action="update",
            data={
                "id": "issue-1",
                "identifier": "PYN-1",
                "title": "Authorized outcome",
                "state": {"id": "state-approved", "name": "Human Approved"},
            },
        )
    )
    event = parse_linear_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())
    assert event.conversation is not None
    event = replace(event, conversation=replace(event.conversation, workspace="project"))
    board = LinearWorkspaceBoard(
        team={"id": "team-1"},
        project={"id": "project-1"},
        states={
            "ready_for_planning": {"id": "state-ready"},
            "awaiting_plan_approval": {"id": "state-awaiting-plan"},
            "human_approved": {"id": "state-approved"},
            "in_progress": {"id": "state-progress"},
        },
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects.linear_client",
        lambda **_kwargs: _linear_client_context(),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects.workspace_issue",
        AsyncMock(return_value=({"state": {"id": "state-approved"}}, board)),
    )
    trigger_reconciliation = AsyncMock()
    configure_linear_accounts_for(
        make_settings(),
        start_work_item_reconciliation=trigger_reconciliation,
    )

    processed = await process_linear_webhook_event(event)

    assert processed.ignored_reason == "work_item_execution_owned_by_controller"
    assert processed.conversation is not None
    trigger_reconciliation.assert_awaited_once_with()


async def test_human_move_directly_to_in_progress_acquires_lease_in_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    raw_body, headers = _signed_request(
        _payload(
            now=now,
            event_type="Issue",
            action="update",
            data={
                "id": "issue-1",
                "identifier": "PYN-1",
                "title": "Authorized outcome",
                "state": {"id": "state-progress", "name": "In Progress"},
            },
            updated_from={
                "projectId": "old-project",
                "stateId": "state-awaiting-plan",
            },
        )
    )
    event = parse_linear_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())
    assert event.conversation is not None
    event = replace(event, conversation=replace(event.conversation, workspace="project"))
    board = LinearWorkspaceBoard(
        team={"id": "team-1"},
        project={"id": "project-1"},
        states={
            "ready_for_planning": {"id": "state-ready"},
            "awaiting_plan_approval": {"id": "state-awaiting-plan"},
            "human_approved": {"id": "state-approved"},
            "in_progress": {"id": "state-progress"},
        },
    )
    acquire_started = AsyncMock(
        return_value=_LeaseResult(status=WorkItemExecutionStatus.IN_PROGRESS)
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects.linear_client",
        lambda **_kwargs: _linear_client_context(),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects.workspace_issue",
        AsyncMock(return_value=({"state": {"id": "state-progress"}}, board)),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects.acquire_human_started_work_item_lease",
        acquire_started,
    )

    processed = await process_linear_webhook_event(event)

    request = acquire_started.await_args.args[1]
    assert request.workspace == "project"
    assert request.issue_id == "issue-1"
    assert request.initiated_by == (f"linear-webhook:{_DELIVERY_ID}:user:user-1")
    assert processed.ignored_reason == "work_item_execution_owned_by_controller"
    assert processed.conversation is not None


@pytest.mark.parametrize(
    ("actor_type", "updated_from"),
    [
        ("integration", {"stateId": "state-awaiting-plan"}),
        ("user", {"title": "Old title"}),
    ],
)
async def test_unproven_in_progress_update_is_suppressed_without_authorizing_work(
    monkeypatch: pytest.MonkeyPatch,
    actor_type: str,
    updated_from: dict[str, object],
) -> None:
    now = datetime.now(UTC)
    payload = _payload(
        now=now,
        event_type="Issue",
        action="update",
        data={
            "id": "issue-1",
            "identifier": "PYN-1",
            "title": "Unproven outcome",
            "state": {"id": "state-progress", "name": "In Progress"},
        },
        updated_from=updated_from,
    )
    payload["actor"] = {"id": "actor-1", "type": actor_type, "name": "Actor"}
    raw_body, headers = _signed_request(payload)
    event = parse_linear_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())
    assert event.conversation is not None
    event = replace(event, conversation=replace(event.conversation, workspace="project"))
    board = LinearWorkspaceBoard(
        team={"id": "team-1"},
        project={"id": "project-1"},
        states={
            "ready_for_planning": {"id": "state-ready"},
            "awaiting_plan_approval": {"id": "state-awaiting-plan"},
            "human_approved": {"id": "state-approved"},
            "in_progress": {"id": "state-progress"},
        },
    )
    acquire_started = AsyncMock()
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects.linear_client",
        lambda **_kwargs: _linear_client_context(),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects.workspace_issue",
        AsyncMock(return_value=({"state": {"id": "state-progress"}}, board)),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects.acquire_human_started_work_item_lease",
        acquire_started,
    )

    processed = await process_linear_webhook_event(event)

    acquire_started.assert_not_awaited()
    assert processed.ignored_reason == "work_item_execution_owned_by_controller"
    assert processed.conversation is not None


@pytest.mark.parametrize(
    ("state_id", "state_name"),
    [
        ("state-ready", "Ready for Planning"),
        ("state-awaiting-plan", "Awaiting Plan Approval"),
    ],
)
async def test_planning_issue_updates_do_not_race_the_temporal_controller(
    monkeypatch: pytest.MonkeyPatch,
    state_id: str,
    state_name: str,
) -> None:
    now = datetime.now(UTC)
    raw_body, headers = _signed_request(
        _payload(
            now=now,
            event_type="Issue",
            action="update",
            data={
                "id": "issue-1",
                "identifier": "PYN-1",
                "title": "Plan durable recovery",
                "state": {"id": state_id, "name": state_name},
            },
        )
    )
    event = parse_linear_webhook(raw_body, headers, _SIGNING_KEY, now, config=_config())
    assert event.conversation is not None
    event = replace(event, conversation=replace(event.conversation, workspace="project"))
    board = LinearWorkspaceBoard(
        team={"id": "team-1"},
        project={"id": "project-1"},
        states={
            "ready_for_planning": {"id": "state-ready"},
            "awaiting_plan_approval": {"id": "state-awaiting-plan"},
            "human_approved": {"id": "state-approved"},
            "in_progress": {"id": "state-progress"},
        },
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects.linear_client",
        lambda **_kwargs: _linear_client_context(),
    )
    monkeypatch.setattr(
        "pynchy.plugins.integrations.linear_webhook_effects.workspace_issue",
        AsyncMock(return_value=({"state": {"id": state_id}}, board)),
    )

    processed = await process_linear_webhook_event(event)

    assert processed.ignored_reason == "work_item_execution_owned_by_controller"
    assert processed.conversation is not None


def test_plugin_route_requires_a_linear_enabled_discord_root() -> None:
    settings = make_settings(
        plugins={
            "linear": PluginConfig(
                options={"webhook_routes": [{"name": "project", "workspace": "project"}]}
            )
        },
        profiles={"linear": ProfileConfig(tools=["linear"])},
        workspaces={"project": WorkspaceConfig(profiles=["linear"])},
        tools={"linear": LinearTool(type="linear", public_source=False)},
    )
    configure_linear_accounts_for(settings)
    with nullcontext():
        route = LinearMcpPlugin().pynchy_webhook_routes()[0]
        validate = route.validate_workspace
        assert validate is not None
        assert route.process_event is not None
        assert validate(_WebhookDeps().workspace) is None
        assert "Discord guild-channel" in validate(
            WorkspaceProfile(
                jid="slack:project",
                name="Project",
                folder="project",
                trigger="@Pynchy",
            )
        )
        assert "workspace root" in validate(
            WorkspaceProfile(
                jid="discord:channel:child",
                name="Child",
                folder="project__thread_child",
                trigger="@Pynchy",
            )
        )
        assert route.routes_conversations is True
        assert route.public_source is False
        assert route.prepare_event is not None


def test_plugin_route_preserves_public_linear_source_taint() -> None:
    settings = make_settings(
        plugins={
            "linear": PluginConfig(
                options={"webhook_routes": [{"name": "project", "workspace": "project"}]}
            )
        },
        profiles={"linear": ProfileConfig(tools=["linear"])},
        workspaces={"project": WorkspaceConfig(profiles=["linear"])},
        tools={"linear": LinearTool(type="linear", public_source=True)},
    )
    configure_linear_accounts_for(settings)
    with nullcontext():
        route = LinearMcpPlugin().pynchy_webhook_routes()[0]

    assert route.public_source is True


def test_project_routed_route_declares_semantic_candidates_before_provider_boot() -> None:
    settings = make_settings(
        plugins={"linear": PluginConfig(options={"webhook_routes": [{"name": "managed-boards"}]})},
        profiles={
            "category": ProfileConfig(tools=["linear"]),
            "fam": ProfileConfig(tools=["linear"], repo="crypdick/fam"),
            "pynchy-dev": ProfileConfig(
                tools=["linear"],
                repo="crypdick/pynchy",
                execution_mode="host",
                cwd="/srv/pynchy",
                is_admin=True,
            ),
        },
        workspaces={
            "relationships": WorkspaceConfig(
                profiles=["category"],
                scopes=[{"workspace": "fam", "profiles": ["fam"]}],
            ),
            "admin": WorkspaceConfig(
                profiles=["category"],
                scopes=[{"workspace": "pynchy-dev", "profiles": ["pynchy-dev"]}],
            ),
        },
        tools={"linear": LinearTool(type="linear", public_source=False)},
    )
    configure_linear_accounts_for(settings)
    with nullcontext():
        route = LinearMcpPlugin().pynchy_webhook_routes()[0]

    assert route.workspace is None
    assert {"fam", "pynchy-dev"} <= set(route.candidate_workspaces)
    assert route.allow_admin_workspaces is True


def test_each_route_uses_its_named_account_trust() -> None:
    settings = make_settings(
        plugins={
            "linear": PluginConfig(
                options={
                    "webhook_routes": [
                        {
                            "name": "public",
                            "workspace": "public-project",
                            "tool": "linear_public",
                        },
                        {
                            "name": "synapse",
                            "workspace": "synapse-project",
                            "tool": "linear_synapse",
                        },
                    ]
                }
            )
        },
        profiles={
            "public": ProfileConfig(tools=["linear_public"]),
            "synapse": ProfileConfig(tools=["linear_synapse"]),
        },
        workspaces={
            "public-project": WorkspaceConfig(profiles=["public"]),
            "synapse-project": WorkspaceConfig(profiles=["synapse"]),
        },
        tools={
            "linear_public": LinearTool(type="linear", public_source=True),
            "linear_synapse": LinearTool(type="linear", public_source=False),
        },
    )
    configure_linear_accounts_for(settings)
    routes = LinearMcpPlugin().pynchy_webhook_routes()

    assert [(route.name, route.public_source) for route in routes] == [
        ("public", True),
        ("synapse", False),
    ]
