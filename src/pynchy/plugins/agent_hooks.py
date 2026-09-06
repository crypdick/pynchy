"""Host-side preparation for trusted agent lifecycle hook plugins."""

from __future__ import annotations

from pathlib import Path

import pluggy

from pynchy.agent_protocol.api import VolumeMount
from pynchy.host.paths import PYNCHY_PLUGIN_HOOK_ROOT
from pynchy.logger import logger
from pynchy.plugins.contracts import AgentHookSpec

_CONTAINER_HOOK_ROOT = Path(PYNCHY_PLUGIN_HOOK_ROOT)


def collect_agent_hook_specs(
    plugin_manager: pluggy.PluginManager | None,
) -> tuple[AgentHookSpec, ...]:
    """Collect existing, uniquely named hook modules from plugins."""
    if plugin_manager is None:
        return ()

    collected: list[AgentHookSpec] = []
    names: set[str] = set()
    for contribution in plugin_manager.hook.pynchy_agent_hook_specs():
        if not isinstance(contribution, tuple):
            logger.warning(
                "Ignoring invalid agent hook contribution",
                result_type=type(contribution).__name__,
            )
            continue
        for spec in contribution:
            if not isinstance(spec, AgentHookSpec):
                logger.warning("Ignoring invalid agent hook spec", spec_type=type(spec).__name__)
                continue
            name = spec.name.strip()
            if not name:
                logger.warning("Ignoring unnamed agent hook spec")
                continue
            module_path = spec.module_path.expanduser().resolve()
            if not module_path.is_file():
                logger.warning(
                    "Ignoring missing agent hook module",
                    hook=name,
                    path=str(module_path),
                )
                continue
            if name in names:
                logger.warning("Ignoring duplicate agent hook name", hook=name)
                continue
            names.add(name)
            collected.append(AgentHookSpec(name=name, module_path=module_path))
    return tuple(collected)


def host_agent_hook_configs(specs: tuple[AgentHookSpec, ...]) -> list[dict[str, str]]:
    """Build runner wire configs that point at host hook modules."""
    return [{"name": spec.name, "module_path": str(spec.module_path)} for spec in specs]


def container_agent_hook_configs(specs: tuple[AgentHookSpec, ...]) -> list[dict[str, str]]:
    """Build runner wire configs that point at mounted container modules."""
    return [
        {"name": spec.name, "module_path": str(_container_hook_path(index, spec))}
        for index, spec in enumerate(specs)
    ]


def agent_hook_mounts(specs: tuple[AgentHookSpec, ...]) -> list[VolumeMount]:
    """Mount each hook module read-only at its runner-visible path."""
    return [
        VolumeMount(
            host_path=str(spec.module_path),
            container_path=str(_container_hook_path(index, spec)),
            readonly=True,
        )
        for index, spec in enumerate(specs)
    ]


def _container_hook_path(index: int, spec: AgentHookSpec) -> Path:
    safe_name = "".join(character if character.isalnum() else "-" for character in spec.name)
    safe_name = safe_name.strip("-") or "hook"
    return _CONTAINER_HOOK_ROOT / f"{index:03d}-{safe_name}.py"
