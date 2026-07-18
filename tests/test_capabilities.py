"""Contract tests for typed capabilities and the host-action catalog."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, replace

import pluggy
import pytest

from pynchy.actions import ACTION_SPECS, ActionId, ActionSpec, ActionSurface, ActionTransport
from pynchy.capabilities import (
    ApprovalContract,
    AuditContract,
    CapabilityCatalogError,
    CapabilityDescriptor,
    CapabilityId,
    CapabilityKind,
    DescriptorOrigin,
    HostActionAccess,
    HostActionDescriptor,
    HostActionRegistration,
    HostToolName,
    IdempotencyContract,
    IdempotencyMode,
    validate_host_action_descriptors,
)
from pynchy.plugins.hookspecs import PynchySpec
from pynchy.plugins.host_actions import get_host_action_catalog
from pynchy.plugins.integrations.matrix_gateway import MATRIX_HOST_ACTIONS

hookimpl = pluggy.HookimplMarker("pynchy")


async def _handler(_data: dict) -> dict[str, object]:
    return await asyncio.sleep(0, result={"result": "ok"})


def _descriptor(
    *,
    capability_id: str = "chat.matrix.list",
    tool_name: str = "matrix_list_chats",
    action_id: str = "chat.matrix.list",
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

    assert "duplicate capability id: chat.matrix.list" in errors
    assert "duplicate host tool name: matrix_list_chats" in errors


def test_validation_rejects_unknown_or_mismatched_action_specs():
    unknown = _descriptor(action_id="chat.unknown.list")
    mismatched = _descriptor(action_id="chat.matrix.message.list")

    errors = validate_host_action_descriptors((unknown, mismatched), ACTION_SPECS)

    assert "chat.matrix.list: unknown ActionSpec chat.unknown.list" in errors
    assert (
        "chat.matrix.list: ActionSpec chat.matrix.message.list does not expose tool "
        "matrix_list_chats"
    ) in errors


def test_write_actions_require_idempotency_and_terminal_audit():
    write = _descriptor(
        capability_id="chat.matrix.message.send",
        tool_name="matrix_send_message",
        action_id="chat.matrix.message.send",
        access=HostActionAccess.WRITE,
    )
    invalid = replace(
        write,
        idempotency=IdempotencyContract(IdempotencyMode.NOT_REQUIRED),
        audit=AuditContract(terminal_outcomes=False),
    )

    errors = validate_host_action_descriptors((invalid,), ACTION_SPECS)

    assert "chat.matrix.message.send: write action requires idempotency" in errors
    assert "chat.matrix.message.send: write action requires terminal audit outcomes" in errors


def test_legacy_registration_is_parsed_through_action_specs():
    class LegacyPlugin:
        @hookimpl
        def pynchy_service_handler(self) -> dict[str, object]:
            return {
                "tools": {"matrix_list_chats": _handler},
                "read_tools": ("matrix_list_chats",),
            }

    catalog = get_host_action_catalog(_plugin_manager(LegacyPlugin()))
    action = catalog.action_for("matrix_list_chats")

    assert action is not None
    assert action.capability.origin is DescriptorOrigin.LEGACY_ADAPTER
    assert action.capability.action_ids == (ActionId("chat.matrix.list"),)
    assert action.access is HostActionAccess.READ


def test_legacy_registration_without_semantic_action_fails_closed():
    class UnknownPlugin:
        @hookimpl
        def pynchy_service_handler(self) -> dict[str, object]:
            return {"tools": {"unregistered_host_tool": _handler}}

    with pytest.raises(
        CapabilityCatalogError,
        match="unregistered_host_tool has no matching semantic ActionSpec",
    ):
        get_host_action_catalog(_plugin_manager(UnknownPlugin()))


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
    duplicate = next(spec for spec in ACTION_SPECS if str(spec.id) == "chat.matrix.list")

    class DuplicatePlugin:
        @hookimpl
        def pynchy_action_specs(self) -> tuple[ActionSpec, ...]:
            return (duplicate,)

    with pytest.raises(
        CapabilityCatalogError,
        match=r"duplicate action id: chat\.matrix\.list",
    ):
        get_host_action_catalog(_plugin_manager(DuplicatePlugin()))
