"""Contract tests for typed capabilities and the host-action catalog."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, replace

import pluggy
import pytest

from pynchy.actions import ACTION_SPECS, ActionId, ActionSpec, ActionSurface, ActionTransport
from pynchy.host.container_manager.security.gate import (
    SecurityGate,
    evaluate_host_action_policy,
)
from pynchy.plugins.api import (
    ApprovalContract,
    ApprovalTrigger,
    AuditContract,
    CapabilityCatalogError,
    CapabilityDescriptor,
    CapabilityId,
    CapabilityKind,
    HostActionAccess,
    HostActionDescriptor,
    HostActionRegistration,
    HostToolName,
    IdempotencyContract,
    IdempotencyMode,
    PynchySpec,
    get_host_action_catalog,
    validate_host_action_descriptors,
)
from pynchy.plugins.integrations.caldav import CalDAVMcpServerPlugin
from pynchy.plugins.integrations.matrix_gateway import MATRIX_HOST_ACTIONS
from pynchy.plugins.memory.sqlite_memory import SqliteMemoryPlugin
from pynchy.workspace.api import (
    CapabilityDecision,
    CapabilityRule,
    ServiceTrustConfig,
    WorkspaceSecurity,
)

hookimpl = pluggy.HookimplMarker("pynchy")


async def _handler(_data: dict) -> dict[str, object]:
    return await asyncio.sleep(0, result={"result": "ok"})


def _descriptor(
    *,
    capability_id: str = "chat.matrix.route.read",
    tool_name: str = "matrix_route_read",
    action_id: str = "chat.matrix.route.read",
    access: HostActionAccess = HostActionAccess.READ,
) -> HostActionDescriptor:
    return HostActionDescriptor(
        capability=CapabilityDescriptor(
            id=CapabilityId(capability_id),
            kind=CapabilityKind.HOST_ACTION,
            owner="tests",
            summary="Exercise one host action.",
            action_ids=(ActionId(action_id),),
        ),
        tool_name=HostToolName(tool_name),
        handler=_handler,
        access=access,
        approval=ApprovalContract(),
        idempotency=IdempotencyContract(
            IdempotencyMode.NOT_REQUIRED
            if access is HostActionAccess.READ
            else IdempotencyMode.IPC_REQUEST_ID
        ),
        audit=AuditContract(),
    )


def _plugin_manager(plugin: object) -> pluggy.PluginManager:
    manager = pluggy.PluginManager("pynchy")
    manager.add_hookspecs(PynchySpec)
    manager.register(plugin)
    return manager


def test_matrix_descriptors_are_immutable_and_match_action_specs():
    descriptor = MATRIX_HOST_ACTIONS.actions[0]

    assert validate_host_action_descriptors(MATRIX_HOST_ACTIONS.actions, ACTION_SPECS) == ()
    with pytest.raises(FrozenInstanceError):
        descriptor.tool_name = HostToolName("replacement")  # type: ignore[misc]


def test_validation_rejects_duplicate_ids_and_tools():
    descriptor = _descriptor()

    errors = validate_host_action_descriptors((descriptor, descriptor), ACTION_SPECS)

    assert "duplicate capability id: chat.matrix.route.read" in errors
    assert "duplicate host tool name: matrix_route_read" in errors


def test_validation_rejects_unknown_or_mismatched_action_specs():
    unknown = _descriptor(action_id="chat.unknown.list")
    mismatched = _descriptor(action_id="chat.matrix.route.send")

    errors = validate_host_action_descriptors((unknown, mismatched), ACTION_SPECS)

    assert "chat.matrix.route.read: unknown ActionSpec chat.unknown.list" in errors
    assert (
        "chat.matrix.route.read: ActionSpec chat.matrix.route.send does not expose tool "
        "matrix_route_read"
    ) in errors


def test_write_actions_require_idempotency_and_terminal_audit():
    write = _descriptor(
        capability_id="chat.matrix.route.send",
        tool_name="matrix_route_send",
        action_id="chat.matrix.route.send",
        access=HostActionAccess.WRITE,
    )
    invalid = replace(
        write,
        idempotency=IdempotencyContract(IdempotencyMode.NOT_REQUIRED),
        audit=AuditContract(terminal_outcomes=False),
    )

    errors = validate_host_action_descriptors((invalid,), ACTION_SPECS)

    assert "chat.matrix.route.send: write action requires idempotency" in errors
    assert "chat.matrix.route.send: write action requires terminal audit outcomes" in errors


def test_mapping_service_registration_is_rejected():
    class MappingPlugin:
        @hookimpl
        def pynchy_service_handler(self) -> dict[str, object]:
            return {"tools": {"matrix_route_read": _handler}}

    with pytest.raises(
        CapabilityCatalogError,
        match="pynchy_service_handler must return HostActionRegistration",
    ):
        get_host_action_catalog(_plugin_manager(MappingPlugin()))


def test_caldav_registration_classifies_list_tools_as_read_only():
    catalog = get_host_action_catalog(_plugin_manager(CalDAVMcpServerPlugin()))

    assert catalog.action_for("list_calendars").access is HostActionAccess.READ
    assert catalog.action_for("list_calendar").access is HostActionAccess.READ
    assert catalog.action_for("create_event").access is HostActionAccess.WRITE
    assert catalog.action_for("delete_event").access is HostActionAccess.WRITE


def test_memory_registration_does_not_gate_reads_as_writes():
    catalog = get_host_action_catalog(_plugin_manager(SqliteMemoryPlugin()))

    assert catalog.action_for("recall_memories").access is HostActionAccess.READ
    assert catalog.action_for("list_memories").access is HostActionAccess.READ
    assert catalog.action_for("save_memory").access is HostActionAccess.WRITE
    assert catalog.action_for("forget_memory").access is HostActionAccess.WRITE


@pytest.mark.parametrize(
    ("tool_name", "payload"),
    [
        (
            "save_memory",
            {"key": "k", "content": "AKIAIOSFODNN7EXAMPLE"},  # pragma: allowlist secret
        ),
        ("recall_memories", {"query": "project"}),
        ("forget_memory", {"key": "k"}),
        ("list_memories", {}),
    ],
)
def test_memory_actions_do_not_require_automatic_human_approval(
    tool_name: str,
    payload: dict[str, object],
):
    catalog = get_host_action_catalog(_plugin_manager(SqliteMemoryPlugin()))
    gate = SecurityGate(WorkspaceSecurity())
    action = catalog.action_for(tool_name)

    assert action is not None
    assert action.approval.trigger is ApprovalTrigger.CAPABILITY_ONLY
    assert not evaluate_host_action_policy(action, gate, payload).needs_human


def test_memory_recall_uses_declared_private_local_trust() -> None:
    catalog = get_host_action_catalog(_plugin_manager(SqliteMemoryPlugin()))
    gate = SecurityGate(WorkspaceSecurity())
    recall = catalog.action_for("recall_memories")

    assert recall is not None
    decision = evaluate_host_action_policy(recall, gate, {"query": "project"})

    assert not decision.needs_cop
    assert not gate.policy.corruption_tainted
    assert gate.policy.secret_tainted


@pytest.mark.parametrize("decision", ["needs_human", "deny"])
def test_explicit_memory_capability_policy_remains_authoritative(
    decision: CapabilityDecision,
) -> None:
    catalog = get_host_action_catalog(_plugin_manager(SqliteMemoryPlugin()))
    save = catalog.action_for("save_memory")
    gate = SecurityGate(
        WorkspaceSecurity(
            capabilities={"memory.save": CapabilityRule(decision=decision)},
        )
    )

    assert save is not None
    policy = evaluate_host_action_policy(save, gate, {"key": "k", "content": "v"})

    assert policy.allowed is (decision != "deny")
    assert policy.needs_human is (decision == "needs_human")


def test_memory_contract_does_not_override_forbidden_service_policy() -> None:
    catalog = get_host_action_catalog(_plugin_manager(SqliteMemoryPlugin()))
    save = catalog.action_for("save_memory")
    gate = SecurityGate(
        WorkspaceSecurity(
            services={
                "sqlite-memory": ServiceTrustConfig(dangerous_writes="forbidden"),
            },
        )
    )

    assert save is not None
    policy = evaluate_host_action_policy(save, gate, {"key": "k", "content": "v"})

    assert not policy.allowed
    assert not policy.needs_human


def test_plugin_can_contribute_action_spec_and_typed_host_action():
    action_id = "weather.forecast.read"
    tool_name = "weather_get_forecast"
    action = _descriptor(
        capability_id=action_id,
        tool_name=tool_name,
        action_id=action_id,
    )
    spec = ActionSpec(
        id=ActionId(action_id),
        owner="weather-plugin",
        summary="Read a weather forecast.",
        surfaces=(ActionSurface(ActionTransport.AGENT_TOOL, tool_name),),
    )

    class WeatherPlugin:
        @hookimpl
        def pynchy_action_specs(self) -> tuple[ActionSpec, ...]:
            return (spec,)

        @hookimpl
        def pynchy_service_handler(self) -> HostActionRegistration:
            return HostActionRegistration(actions=(action,))

    catalog = get_host_action_catalog(_plugin_manager(WeatherPlugin()))

    assert catalog.action_for(tool_name) == action
    assert catalog.action_specs[-1] == spec


def test_plugin_action_spec_cannot_redefine_builtin_action_id():
    duplicate = next(spec for spec in ACTION_SPECS if str(spec.id) == "chat.matrix.route.read")

    class DuplicatePlugin:
        @hookimpl
        def pynchy_action_specs(self) -> tuple[ActionSpec, ...]:
            return (duplicate,)

    with pytest.raises(
        CapabilityCatalogError,
        match=r"duplicate action id: chat\.matrix\.route\.read",
    ):
        get_host_action_catalog(_plugin_manager(DuplicatePlugin()))
