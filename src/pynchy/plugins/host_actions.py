"""Host-action plugin discovery and typed registration validation."""

from __future__ import annotations

from dataclasses import dataclass

import pluggy

from pynchy.actions.api import ACTION_SPECS, ActionSpec, validate_action_specs
from pynchy.plugins.capabilities import (
    CapabilityCatalogError,
    HostActionDescriptor,
    HostActionRegistration,
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


_catalog: HostActionCatalog | None = None


def get_host_action_catalog(
    pm: pluggy.PluginManager | None = None,
    *,
    action_specs: tuple[ActionSpec, ...] | None = None,
) -> HostActionCatalog:
    """Collect, validate, and cache the effective host-action catalog."""
    global _catalog  # noqa: PLW0603 - process-wide singleton.
    if pm is None and action_specs is None and _catalog is not None:
        return _catalog
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
        raise CapabilityCatalogError("pynchy_service_handler must return HostActionRegistration")

    errors = validate_host_action_descriptors(actions, effective_action_specs)
    if errors:
        raise CapabilityCatalogError("; ".join(errors))
    catalog = HostActionCatalog(
        actions=tuple(sorted(actions, key=lambda action: action.tool_name)),
        action_specs=effective_action_specs,
    )
    if pm is None and action_specs is None:
        _catalog = catalog
    return catalog


def clear_host_action_catalog_cache() -> None:
    """Clear the process cache after plugin/config changes and between tests."""
    global _catalog  # noqa: PLW0603 - process-wide singleton.
    _catalog = None


def initialize_host_action_catalog(pm: pluggy.PluginManager) -> HostActionCatalog:
    """Validate plugin declarations during startup and install the effective catalog."""
    global _catalog  # noqa: PLW0603 - process-wide singleton.
    catalog = get_host_action_catalog(pm)
    _catalog = catalog
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
