"""Host-action plugin discovery and mapping-registration parsing."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

import pluggy  # noqa: TC002, RUF100 - plugin-manager annotations are used at runtime.

from pynchy.actions import ACTION_SPECS, ActionId, ActionSpec, validate_action_specs
from pynchy.capabilities import (
    ApprovalContract,
    AuditContract,
    CapabilityCatalogError,
    CapabilityDescriptor,
    CapabilityId,
    CapabilityKind,
    CapabilityRequirement,
    CapabilityRequirementKind,
    DescriptorOrigin,
    HostActionAccess,
    HostActionDescriptor,
    HostActionHandler,
    HostActionRegistration,
    HostToolName,
    IdempotencyContract,
    IdempotencyMode,
    action_specs_for_host_tool,
    validate_host_action_descriptors,
)
from pynchy.plugins.registry import get_plugin_manager

__all__ = [
    "HostActionCatalog",
    "clear_host_action_catalog_cache",
    "get_effective_action_specs",
    "get_host_action_catalog",
    "initialize_host_action_catalog",
]


@dataclass(frozen=True)
class HostActionCatalog:
    """Validated immutable catalogue used by dispatch and capability status."""

    actions: tuple[HostActionDescriptor, ...]
    action_specs: tuple[ActionSpec, ...] = ACTION_SPECS

    def action_for(self, tool_name: str) -> HostActionDescriptor | None:
        return next((action for action in self.actions if action.tool_name == tool_name), None)

    @property
    def handlers(self) -> dict[str, HostActionHandler]:
        """Return handlers keyed by host tool name."""
        return {str(action.tool_name): action.handler for action in self.actions}


@dataclass
class _CatalogState:
    catalog: HostActionCatalog | None = None


_state = _CatalogState()


def get_host_action_catalog(
    pm: pluggy.PluginManager | None = None,
    *,
    action_specs: tuple[ActionSpec, ...] | None = None,
) -> HostActionCatalog:
    """Collect, parse, validate, and cache the effective host-action catalog."""
    if pm is None and action_specs is None and _state.catalog is not None:
        return _state.catalog
    plugin_manager = pm or get_plugin_manager()
    effective_action_specs = (
        action_specs if action_specs is not None else get_effective_action_specs(plugin_manager)
    )
    computer_use_backends = tuple(plugin_manager.hook.pynchy_computer_use_backend())
    actions: list[HostActionDescriptor] = []
    for contribution in plugin_manager.hook.pynchy_service_handler(
        computer_use_backends=computer_use_backends
    ):
        if isinstance(contribution, HostActionRegistration):
            actions.extend(contribution.actions)
            continue
        actions.extend(_legacy_registration(contribution, effective_action_specs))

    errors = validate_host_action_descriptors(actions, effective_action_specs)
    if errors:
        raise CapabilityCatalogError("; ".join(errors))
    catalog = HostActionCatalog(
        actions=tuple(sorted(actions, key=lambda action: action.tool_name)),
        action_specs=effective_action_specs,
    )
    if pm is None and action_specs is None:
        _state.catalog = catalog
    return catalog


def clear_host_action_catalog_cache() -> None:
    """Clear the process cache after plugin/config changes and between tests."""
    _state.catalog = None


def initialize_host_action_catalog(pm: pluggy.PluginManager) -> HostActionCatalog:
    """Validate plugin declarations during startup and install the effective catalog."""
    catalog = get_host_action_catalog(pm)
    _state.catalog = catalog
    return catalog


def get_effective_action_specs(pm: pluggy.PluginManager) -> tuple[ActionSpec, ...]:
    """Compose built-in and plugin-owned semantic action specifications."""
    specs = list(ACTION_SPECS)
    for contribution in pm.hook.pynchy_action_specs():
        if not isinstance(contribution, list | tuple):
            raise CapabilityCatalogError("pynchy_action_specs must return a list or tuple")
        for spec in contribution:
            if not isinstance(spec, ActionSpec):
                raise CapabilityCatalogError(
                    "pynchy_action_specs entries must be ActionSpec instances"
                )
            specs.append(spec)
    errors = validate_action_specs(specs)
    if errors:
        raise CapabilityCatalogError("; ".join(errors))
    return tuple(specs)


def _legacy_registration(
    contribution: object,
    action_specs: tuple[ActionSpec, ...],
) -> tuple[HostActionDescriptor, ...]:
    if not isinstance(contribution, Mapping):
        raise CapabilityCatalogError(
            "pynchy_service_handler must return HostActionRegistration or a mapping"
        )
    raw_tools = contribution.get("tools")
    if not isinstance(raw_tools, Mapping):
        raise CapabilityCatalogError("legacy service-handler registration requires a tools mapping")
    raw_read_tools = contribution.get("read_tools", ())
    if not isinstance(raw_read_tools, list | tuple | set | frozenset):
        raise CapabilityCatalogError("legacy read_tools must be a list, tuple, set, or frozenset")
    read_tools = {name for name in raw_read_tools if isinstance(name, str)}

    actions: list[HostActionDescriptor] = []
    for raw_name, handler in raw_tools.items():
        if not isinstance(raw_name, str) or not raw_name:
            raise CapabilityCatalogError("legacy host tool names must be non-empty strings")
        if not callable(handler):
            raise CapabilityCatalogError(
                f"legacy host action {raw_name} has a non-callable handler"
            )
        matching_specs = action_specs_for_host_tool(raw_name, action_specs)
        if not matching_specs:
            raise CapabilityCatalogError(
                f"legacy host action {raw_name} has no matching semantic ActionSpec"
            )
        actions.append(
            _adapt_legacy_action(
                raw_name,
                handler,
                matching_specs,
                is_read=raw_name in read_tools,
            )
        )
    return tuple(actions)


def _adapt_legacy_action(
    tool_name: str,
    handler: object,
    specs: tuple[ActionSpec, ...],
    *,
    is_read: bool,
) -> HostActionDescriptor:
    owners = tuple(dict.fromkeys(spec.owner for spec in specs))
    owner = owners[0] if len(owners) == 1 else "+".join(owners)
    action_ids = tuple(ActionId(str(spec.id)) for spec in specs)
    capability_id = (
        CapabilityId(str(action_ids[0]))
        if len(action_ids) == 1
        else CapabilityId(f"host.{tool_name.replace('_', '.')}")
    )
    summary = specs[0].summary if len(specs) == 1 else f"Run the {tool_name} host action."
    access = HostActionAccess.READ if is_read else HostActionAccess.WRITE
    return HostActionDescriptor(
        capability=CapabilityDescriptor(
            id=capability_id,
            kind=CapabilityKind.HOST_ACTION,
            owner=owner,
            summary=summary,
            action_ids=action_ids,
            requirements=(
                CapabilityRequirement(
                    kind=CapabilityRequirementKind.WORKSPACE_TOOL,
                    name=tool_name,
                    description=f"Enable the {tool_name} tool for this workspace.",
                ),
            ),
            origin=DescriptorOrigin.LEGACY_ADAPTER,
        ),
        tool_name=HostToolName(tool_name),
        handler=cast("HostActionHandler", handler),
        access=access,
        approval=ApprovalContract(),
        idempotency=IdempotencyContract(
            IdempotencyMode.NOT_REQUIRED if is_read else IdempotencyMode.IPC_REQUEST_ID
        ),
        audit=AuditContract(),
    )
