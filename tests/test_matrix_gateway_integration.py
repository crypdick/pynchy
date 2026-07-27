"""Hermetic tests for first-party routed Matrix conversations."""

from __future__ import annotations

import json
import subprocess  # noqa: S404 - tests construct completed local process fixtures.
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import make_settings

from pynchy.capabilities import ApprovalTrigger, HostActionAccess
from pynchy.config.models import (
    MatrixConnectionConfig,
    MatrixEndpointConfig,
)
from pynchy.config.settings import validate_settings_mapping
from pynchy.conversation.models import ConversationId
from pynchy.plugins import get_plugin_manager
from pynchy.plugins.integrations import matrix_gateway
from pynchy.plugins.integrations.matrix_gateway_client import (
    MatrixGatewayClient,
    MatrixGatewayError,
    MatrixMessage,
    MatrixPortalAssertion,
    MatrixSendResult,
    matrix_connection_state_dir,
)
from pynchy.plugins.integrations.matrix_route_registry import (
    ActiveMatrixRoute,
    bind_active_matrix_route,
    clear_active_matrix_routes,
)
from pynchy.plugins.integrations.matrix_route_resolution import (
    ResolvedMatrixRoute,
    resolve_matrix_routes,
)
from pynchy.types import ChatJid

_ROOM = "!family:matrix.example.com"
_CONTROL = "discord:thread:family"
_FOLDER = "support-conversation-conv_family"


@dataclass
class StubMatrixGatewayClient:
    """Typed in-memory gateway substitute at the local process boundary."""

    portal: MatrixPortalAssertion
    calls: list[tuple[str, object]] = field(default_factory=list)

    def list_messages(self, *, room_id: str, limit: int) -> list[MatrixMessage]:
        self.calls.append(("list_messages", (room_id, limit)))
        return [
            MatrixMessage(
                room_id=room_id,
                event_id="$message",
                sender="@friend:matrix.example.com",
                origin_server_ts=1,
                body="Hello",
            )
        ]

    def room_assertion(self, *, room_id: str) -> MatrixPortalAssertion:
        self.calls.append(("room_assertion", room_id))
        return self.portal

    def send_message(self, *, room_id: str, body: str) -> MatrixSendResult:
        self.calls.append(("send_message", (room_id, body)))
        return MatrixSendResult(room_id=room_id, event_id="$sent")


def _route(*, outbound: str = "approval_required") -> ResolvedMatrixRoute:
    connection = MatrixConnectionConfig(
        expected_user_id="@me:matrix.example.com",
        chat={"family": MatrixEndpointConfig(room_id=_ROOM, title="Family")},
    )
    return ResolvedMatrixRoute(
        name="family",
        connection_name="personal-chats",
        connection=connection,
        endpoint_name="family",
        endpoint=connection.chat["family"],
        control_title="Family",
        workspace="support",
        activation="on_event",
        outbound=outbound,  # type: ignore[arg-type]
        tools=("matrix_route_read", "matrix_route_send"),
        capabilities={},
    )


def _portal(*, room_id: str = _ROOM) -> MatrixPortalAssertion:
    return MatrixPortalAssertion(
        room_id=room_id,
        owner_user_id="@me:matrix.example.com",
        joined=True,
    )


def _bind_route(*, outbound: str = "approval_required") -> ActiveMatrixRoute:
    binding = ActiveMatrixRoute(
        workspace_folder=_FOLDER,
        conversation_id=ConversationId("conv_family"),
        control_thread_jid=ChatJid(_CONTROL),
        route=_route(outbound=outbound),
        portal=_portal(),
    )
    bind_active_matrix_route(binding)
    return binding


def _handlers():
    registration = matrix_gateway.MatrixGatewayPlugin().pynchy_service_handler()
    return {str(action.tool_name): action.handler for action in registration.actions}


def _canonical_config() -> dict[str, object]:
    return {
        "workspaces": {"support": {}},
        "connections": {
            "personal-chats": {
                "type": "matrix",
                "expected_user_id": "@me:matrix.example.com",
                "chat": {"family": {"room_id": _ROOM, "title": "Family"}},
            }
        },
        "routes": {
            "family": {
                "source": "connection.matrix.personal-chats.chat.family",
                "workspace": "support",
                "activation": "on_event",
            }
        },
    }


@pytest.fixture(autouse=True)
def _route_registry() -> None:
    clear_active_matrix_routes()


def test_canonical_connections_and_routes_config_resolves_matrix_runtime() -> None:
    settings = validate_settings_mapping(_canonical_config())

    with patch.object(matrix_gateway, "get_settings", return_value=settings):
        runtimes = matrix_gateway.MatrixGatewayPlugin().pynchy_connection_runtime()

    assert settings.connections["personal-chats"].type == "matrix"
    assert settings.routes["family"].workspace == "support"
    assert [runtime.name for runtime in runtimes] == ["connection.matrix.personal-chats"]


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("expected_user_id", "me", "full Matrix user ID"),
        ("room_id", "family-room", "immutable Matrix room ID"),
    ],
)
def test_matrix_connection_rejects_ambiguous_provider_ids(
    field: str,
    value: str,
    error: str,
) -> None:
    raw = _canonical_config()
    connection = raw["connections"]["personal-chats"]
    if field == "room_id":
        connection["chat"]["family"][field] = value
    else:
        connection[field] = value

    with pytest.raises(ValueError, match=error):
        validate_settings_mapping(raw)


@pytest.mark.parametrize(
    ("case", "error"),
    [
        ("unknown-connection", "unknown connection"),
        ("unknown-endpoint", "unknown endpoint"),
        ("disabled-endpoint", "disabled endpoint"),
        ("unknown-workspace", "unknown workspace"),
        ("platform-mismatch", "platform does not match"),
        ("duplicate-source", "cannot map to multiple"),
    ],
)
def test_route_references_fail_closed(case: str, error: str) -> None:
    raw = deepcopy(_canonical_config())
    route = raw["routes"]["family"]
    if case == "unknown-connection":
        route["source"] = "connection.matrix.missing.chat.family"
    elif case == "unknown-endpoint":
        route["source"] = "connection.matrix.personal-chats.chat.missing"
    elif case == "disabled-endpoint":
        raw["connections"]["personal-chats"]["chat"]["family"]["enabled"] = False
    elif case == "unknown-workspace":
        route["workspace"] = "missing"
    elif case == "platform-mismatch":
        route["source"] = "connection.discord.personal-chats.chat.family"
    else:
        raw["routes"]["family-copy"] = deepcopy(route)

    with pytest.raises((TypeError, ValueError), match=error):
        validate_settings_mapping(raw)


@pytest.mark.parametrize(
    ("case", "error"),
    [
        ("admin-on-event", "untrusted events to an admin workspace"),
        ("tool-expansion", "tools must be a restriction"),
        ("capability-weakening", "cannot weaken workspace denial"),
        ("wildcard-capability-weakening", "cannot weaken workspace denial"),
    ],
)
def test_route_policy_can_only_reduce_parent_workspace_privilege(
    case: str,
    error: str,
) -> None:
    raw = deepcopy(_canonical_config())
    raw["profiles"] = {"support": {}}
    raw["workspaces"]["support"] = {"profiles": ["support"]}
    if case == "admin-on-event":
        raw["profiles"]["support"]["is_admin"] = True
    elif case == "tool-expansion":
        raw["routes"]["family"]["tools"] = ["matrix_route_read"]
    elif case == "capability-weakening":
        capability = "chat.matrix.route.read"
        raw["profiles"]["support"]["capabilities"] = {capability: {"decision": "deny"}}
        raw["routes"]["family"]["capabilities"] = {capability: "needs_human"}
    else:
        capability = "chat.matrix.route.read"
        raw["profiles"]["support"]["capabilities"] = {"chat.matrix.*": {"decision": "deny"}}
        raw["routes"]["family"]["capabilities"] = {capability: "needs_human"}
    settings = validate_settings_mapping(raw)

    with pytest.raises(ValueError, match=error):
        resolve_matrix_routes(settings)


@pytest.mark.parametrize(
    ("default_outbound", "route_outbound"),
    [
        ("read_only", "approval_required"),
        ("approval_required", "read_only"),
    ],
)
def test_route_outbound_merge_keeps_the_most_restrictive_policy(
    default_outbound: str,
    route_outbound: str,
) -> None:
    raw = deepcopy(_canonical_config())
    raw["connections"]["personal-chats"]["route_defaults"] = {"outbound": default_outbound}
    raw["routes"]["family"]["outbound"] = route_outbound
    settings = validate_settings_mapping(raw)

    [resolved] = resolve_matrix_routes(settings)

    assert resolved.outbound == "read_only"


def test_unused_matrix_connection_does_not_create_a_false_ready_runtime() -> None:
    raw = _canonical_config()
    raw["routes"] = {}
    settings = validate_settings_mapping(raw)

    with patch.object(matrix_gateway, "get_settings", return_value=settings):
        runtimes = matrix_gateway.MatrixGatewayPlugin().pynchy_connection_runtime()

    assert runtimes == ()


def test_plugin_exposes_only_route_scoped_tools_and_write_is_mandatory_approval() -> None:
    registration = matrix_gateway.MatrixGatewayPlugin().pynchy_service_handler()

    assert {str(action.tool_name) for action in registration.actions} == {
        "matrix_route_read",
        "matrix_route_send",
    }
    send = registration.action_for("matrix_route_send")
    assert send is not None
    assert send.access is HostActionAccess.WRITE
    assert send.approval.trigger is ApprovalTrigger.ALWAYS
    assert isinstance(
        get_plugin_manager().get_plugin("builtin-matrix-gateway"),
        matrix_gateway.MatrixGatewayPlugin,
    )


@pytest.mark.action("chat.matrix.route.read")
async def test_route_read_ignores_caller_destination_and_uses_bound_room(tmp_path: Path) -> None:
    _bind_route()
    stub = StubMatrixGatewayClient(_portal())
    handler = _handlers()["matrix_route_read"]

    with (
        patch.object(matrix_gateway, "create_matrix_gateway_client", return_value=stub),
        patch.object(matrix_gateway, "get_settings", return_value=make_settings(data_dir=tmp_path)),
        patch.object(
            matrix_gateway,
            "get_conversation_control_binding",
            return_value=type("Binding", (), {"thread_jid": _CONTROL})(),
        ),
    ):
        result = await handler(
            {"source_group": _FOLDER, "room_id": "!attacker:example.com", "limit": 3}
        )

    assert json.loads(result["result"])[0]["body"] == "Hello"
    assert stub.calls == [
        ("room_assertion", _ROOM),
        ("list_messages", (_ROOM, 3)),
    ]
    assert _ROOM not in result["result"]


@pytest.mark.action("chat.matrix.route.read")
async def test_route_read_rechecks_live_portal_and_denies_stale_binding(tmp_path: Path) -> None:
    _bind_route()
    stub = StubMatrixGatewayClient(_portal(room_id="!replacement:matrix.example.com"))
    handler = _handlers()["matrix_route_read"]

    with (
        patch.object(matrix_gateway, "create_matrix_gateway_client", return_value=stub),
        patch.object(matrix_gateway, "get_settings", return_value=make_settings(data_dir=tmp_path)),
        patch.object(
            matrix_gateway,
            "get_conversation_control_binding",
            return_value=type("Binding", (), {"thread_jid": _CONTROL})(),
        ),
    ):
        result = await handler({"source_group": _FOLDER, "limit": 3})

    assert "denied" in result["error"].lower()
    assert stub.calls == [("room_assertion", _ROOM)]


@pytest.mark.action("chat.matrix.route.send")
async def test_route_send_rechecks_exact_portal_before_provider_write(tmp_path: Path) -> None:
    _bind_route()
    stub = StubMatrixGatewayClient(_portal(room_id="!replacement:matrix.example.com"))
    handler = _handlers()["matrix_route_send"]

    with (
        patch.object(matrix_gateway, "create_matrix_gateway_client", return_value=stub),
        patch.object(matrix_gateway, "get_settings", return_value=make_settings(data_dir=tmp_path)),
        patch.object(
            matrix_gateway,
            "get_conversation_control_binding",
            return_value=type("Binding", (), {"thread_jid": _CONTROL})(),
        ),
    ):
        result = await handler({"source_group": _FOLDER, "body": "Private reply"})

    assert "denied" in result["error"].lower()
    assert all(call[0] != "send_message" for call in stub.calls)


@pytest.mark.action("chat.matrix.route.send")
async def test_route_send_returns_only_agent_safe_event_receipt(tmp_path: Path) -> None:
    _bind_route()
    stub = StubMatrixGatewayClient(_portal())
    handler = _handlers()["matrix_route_send"]

    with (
        patch.object(matrix_gateway, "create_matrix_gateway_client", return_value=stub),
        patch.object(matrix_gateway, "get_settings", return_value=make_settings(data_dir=tmp_path)),
        patch.object(
            matrix_gateway,
            "get_conversation_control_binding",
            return_value=type("Binding", (), {"thread_jid": _CONTROL})(),
        ),
    ):
        result = await handler({"source_group": _FOLDER, "body": "Private reply"})

    assert json.loads(result["result"]) == {"event_id": "$sent"}
    assert _ROOM not in result["result"]
    assert stub.calls == [
        ("room_assertion", _ROOM),
        ("send_message", (_ROOM, "Private reply")),
    ]
    action = matrix_gateway.MATRIX_HOST_ACTIONS.action_for("matrix_route_send")
    assert action is not None
    assert action.action_intent is not None
    receipt = action.action_intent.receipt_from_response(result)
    assert receipt.provider_request_id == "$sent"
    assert receipt.receipt == {"event_id": "$sent"}


def test_read_only_route_rejects_draft_before_approval() -> None:
    _bind_route(outbound="read_only")
    action = matrix_gateway.MATRIX_HOST_ACTIONS.action_for("matrix_route_send")
    assert action is not None
    assert action.action_intent is not None

    with pytest.raises(ValueError, match="read-only"):
        action.action_intent.draft_from_request({"source_group": _FOLDER, "body": "hello"})


def test_approval_draft_binds_route_conversation_portal_thread_and_body() -> None:
    active = _bind_route()
    action = matrix_gateway.MATRIX_HOST_ACTIONS.action_for("matrix_route_send")
    assert action is not None
    assert action.action_intent is not None

    draft = action.action_intent.draft_from_request({"source_group": _FOLDER, "body": "hello"})

    assert draft.recipient == "matrix-route:family"
    assert draft.payload == {
        "connection": "personal-chats",
        "route": "family",
        "conversation_id": "conv_family",
        "approval_chat_jid": _CONTROL,
        "room_id": _ROOM,
        "portal": active.portal.model_dump(mode="json"),
        "body": "hello",
    }


def test_gateway_subprocess_uses_distinct_absolute_connection_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UNRELATED_HOST_SECRET", "must-not-leak")
    first_dir = matrix_connection_state_dir(tmp_path / "relative", "personal-chats")
    second_dir = matrix_connection_state_dir(tmp_path / "relative", "work-chats")
    completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps(
            {
                "room_id": _ROOM,
                "owner_user_id": "@me:matrix.example.com",
                "joined": True,
            }
        ),
        stderr="",
    )

    with patch(
        "pynchy.plugins.integrations.matrix_gateway_client.subprocess.run",
        side_effect=[completed, completed],
    ) as run:
        MatrixGatewayClient("gateway", state_dir=first_dir).room_assertion(room_id=_ROOM)
        MatrixGatewayClient("gateway", state_dir=second_dir).room_assertion(room_id=_ROOM)

    environments = [call.kwargs["env"] for call in run.call_args_list]
    assert Path(environments[0]["PYNCHY_MATRIX_GATEWAY_DATA_DIR"]).is_absolute()
    assert first_dir == tmp_path / "relative/matrix-gateway/connection-personal-chats"
    assert environments[0]["PYNCHY_MATRIX_GATEWAY_DATA_DIR"] == str(first_dir)
    assert environments[1]["PYNCHY_MATRIX_GATEWAY_DATA_DIR"] == str(second_dir)
    assert "UNRELATED_HOST_SECRET" not in environments[0]
    assert first_dir != second_dir


def test_connection_state_path_encodes_traversal_as_one_readable_component(
    tmp_path: Path,
) -> None:
    state_dir = matrix_connection_state_dir(tmp_path, "../family/personal chats")

    assert state_dir == (tmp_path / "matrix-gateway/connection-..%2Ffamily%2Fpersonal%20chats")


def test_gateway_sync_uses_stdin_and_safe_provider_errors() -> None:
    failed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="private")
    client = MatrixGatewayClient(command="gateway")

    with (
        patch(
            "pynchy.plugins.integrations.matrix_gateway_client.subprocess.run",
            return_value=failed,
        ),
        pytest.raises(MatrixGatewayError, match="Matrix gateway command failed"),
    ):
        client.sync(since="cursor", room_ids=(_ROOM,))


def test_empty_history_is_distinct_from_missing_encryption_keys() -> None:
    empty = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    encrypted = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout="",
        stderr="MATRIX_GATEWAY_E2EE_KEYS_UNAVAILABLE private details",
    )
    client = MatrixGatewayClient(command="gateway")

    with patch(
        "pynchy.plugins.integrations.matrix_gateway_client.subprocess.run",
        return_value=empty,
    ):
        assert client.list_messages(room_id=_ROOM, limit=50) == []
    with (
        patch(
            "pynchy.plugins.integrations.matrix_gateway_client.subprocess.run",
            return_value=encrypted,
        ),
        pytest.raises(MatrixGatewayError, match="does not have usable room keys"),
    ):
        client.list_messages(room_id=_ROOM, limit=50)
