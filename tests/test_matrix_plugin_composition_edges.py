"""Behavior tests for the host-owned Matrix composition contract."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import pytest

from pynchy.config.api import validate_settings_mapping
from pynchy.conversation.api import (
    ConversationClaimId,
    ConversationId,
    ConversationSubject,
    ConversationSubjectKey,
    ConversationSubjectNamespace,
    ExternalDeliveryId,
    ExternalDeliveryIdentity,
    ExternalProvider,
    ExternalRoute,
)
from pynchy.conversation.models import (
    Conversation,
    ConversationDelivery,
    ConversationDeliveryStatus,
)
from pynchy.host.orchestrator import plugin_configuration
from pynchy.identifiers import ChatJid, GroupFolder
from pynchy.plugins.api import ConnectionRuntimeContext
from pynchy.plugins.integrations import matrix_gateway
from pynchy.plugins.integrations.matrix_gateway_client import MatrixPortalAssertion
from pynchy.workspace.api import WorkspaceProfile


def _settings(*, workspace: str = "support"):
    return validate_settings_mapping(
        {
            "workspaces": {"support": {}},
            "connections": {
                "personal-chats": {
                    "type": "matrix",
                    "expected_user_id": "@me:matrix.example.com",
                    "chat": {
                        "family": {
                            "room_id": "!family:matrix.example.com",
                            "title": "Family",
                        }
                    },
                }
            },
            "routes": {
                "family": {
                    "source": "connection.matrix.personal-chats.chat.family",
                    "workspace": workspace,
                }
            },
        }
    )


def _context(workspaces: dict[str, WorkspaceProfile]) -> ConnectionRuntimeContext:
    return ConnectionRuntimeContext(
        channels=list,
        workspaces=lambda: workspaces,
        register_workspace=AsyncMock(),
        unregister_workspace=AsyncMock(),
        bind_session=AsyncMock(),
        ingest_message=AsyncMock(),
    )


def _delivery() -> ConversationDelivery:
    identity = ExternalDeliveryIdentity(
        provider=ExternalProvider("matrix"),
        route=ExternalRoute("personal-chats:family"),
        delivery_id=ExternalDeliveryId("event-1"),
    )
    return ConversationDelivery(
        sequence=1,
        identity=identity,
        conversation_id=ConversationId("conversation-1"),
        status=ConversationDeliveryStatus.PENDING,
        received_at="2026-07-29T00:00:00Z",
        payload={"body": "hello"},
    )


def test_matrix_plugin_hook_requires_configured_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "pynchy.plugins.integrations.matrix_gateway._runtime",
        None,
    )

    with pytest.raises(RuntimeError, match="runtime has not been configured"):
        matrix_gateway.MatrixGatewayPlugin().pynchy_connection_runtime()


@pytest.mark.asyncio
async def test_matrix_composition_installs_state_callbacks_and_route_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured: dict[str, object] = {}
    monkeypatch.setattr(
        plugin_configuration,
        "configure_matrix_gateway_runtime",
        lambda runtime: configured.setdefault("runtime", runtime),
    )
    settings = _settings()

    plugin_configuration.configure_matrix_gateway_plugin(settings)

    runtime = configured["runtime"]
    assert [route.name for route in runtime.routes] == ["family"]
    assert [connection.name for connection in runtime.connections] == ["personal-chats"]

    monkeypatch.setattr(
        plugin_configuration,
        "get_conversation_control_binding",
        AsyncMock(return_value=None),
    )
    assert await runtime.get_control_thread_jid(ConversationId("conversation-1")) is None

    operations = runtime.connection_operations
    monkeypatch.setattr(
        plugin_configuration,
        "claim_next_conversation_delivery",
        AsyncMock(return_value=None),
    )
    assert (
        await operations.claim_delivery(
            ConversationId("conversation-1"), ConversationClaimId("claim-1")
        )
        is None
    )

    delivery = _delivery()
    monkeypatch.setattr(
        plugin_configuration,
        "claim_next_conversation_delivery",
        AsyncMock(return_value=delivery),
    )
    claimed = await operations.claim_delivery(
        ConversationId("conversation-1"), ConversationClaimId("claim-1")
    )
    assert claimed is not None
    assert claimed.delivery_id == "event-1"
    assert claimed.payload == {"body": "hello"}

    admit = AsyncMock()
    monkeypatch.setattr(plugin_configuration, "admit_conversation_delivery", admit)
    subject = ConversationSubject(
        ConversationSubjectNamespace("matrix:room"), ConversationSubjectKey("room-1")
    )
    await operations.admit_delivery(
        delivery.identity, subject, GroupFolder("support"), {"body": "hello"}
    )
    admit.assert_awaited_once()

    monkeypatch.setattr(plugin_configuration, "get_conversation", AsyncMock(return_value=None))
    assert await operations.conversation_exists(ConversationId("missing")) is False


@pytest.mark.asyncio
async def test_matrix_route_control_rejects_unknown_then_ensures_registered_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured: dict[str, object] = {}
    monkeypatch.setattr(
        plugin_configuration,
        "configure_matrix_gateway_runtime",
        lambda runtime: configured.setdefault("runtime", runtime),
    )
    plugin_configuration.configure_matrix_gateway_plugin(_settings())
    operations = configured["runtime"].connection_operations
    route = configured["runtime"].routes[0]
    assertion = MatrixPortalAssertion(
        room_id="!family:matrix.example.com",
        owner_user_id="@me:matrix.example.com",
        joined=True,
    )

    with pytest.raises(ValueError, match="not registered"):
        await operations.ensure_route_control(_context({}), route, assertion)

    parent = WorkspaceProfile(
        jid="discord:channel:support",
        name="Support",
        folder="support",
        trigger="!",
    )
    conversation = Conversation(
        id=ConversationId("conversation-1"),
        workspace=GroupFolder("support"),
        subject=ConversationSubject(
            ConversationSubjectNamespace("matrix:room"), ConversationSubjectKey("room-1")
        ),
        session_id=None,
        created_at="",
        updated_at="",
    )
    monkeypatch.setattr(
        plugin_configuration,
        "resolve_conversation",
        AsyncMock(return_value=conversation),
    )
    monkeypatch.setattr(plugin_configuration, "register_runtime_workspace_policy", Mock())
    ensured = Mock()
    ensured.control.binding.thread_jid = ChatJid("discord:thread:control")
    monkeypatch.setattr(
        plugin_configuration,
        "ensure_conversation_workspace",
        AsyncMock(return_value=ensured),
    )

    result = await operations.ensure_route_control(_context({"support": parent}), route, assertion)

    assert result.control_thread_jid == ChatJid("discord:thread:control")


def test_matrix_composition_rejects_routes_for_unknown_workspaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()

    def no_workspace(_settings: object, _workspace: str) -> None:
        return None

    monkeypatch.setattr(type(settings), "resolved_workspace_config", no_workspace)
    monkeypatch.setattr(plugin_configuration, "configure_matrix_gateway_runtime", Mock())

    with pytest.raises(ValueError, match="unknown workspace"):
        plugin_configuration.configure_matrix_gateway_plugin(settings)
