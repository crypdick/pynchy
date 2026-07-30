"""Tests for host-to-runner agent lifecycle hook wiring."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pluggy

if TYPE_CHECKING:
    from pathlib import Path

from pynchy.plugins.api import (
    AgentHookSpec,
    PynchySpec,
    agent_hook_mounts,
    collect_agent_hook_specs,
    container_agent_hook_configs,
    host_agent_hook_configs,
)

hookimpl = pluggy.HookimplMarker("pynchy")


class _AgentHookPlugin:
    def __init__(self, *specs: AgentHookSpec) -> None:
        self._specs = specs

    @hookimpl
    def pynchy_agent_hook_specs(self) -> tuple[AgentHookSpec, ...]:
        return self._specs


def _plugin_manager(*specs: AgentHookSpec) -> pluggy.PluginManager:
    manager = pluggy.PluginManager("pynchy")
    manager.add_hookspecs(PynchySpec)
    manager.register(_AgentHookPlugin(*specs))
    return manager


def test_agent_hook_specs_become_host_and_container_runner_configs(tmp_path: Path) -> None:
    hook_module = tmp_path / "audit_hook.py"
    hook_module.write_text("async def before_tool_use(tool_name, tool_input):\n    return None\n")

    specs = collect_agent_hook_specs(
        _plugin_manager(AgentHookSpec(name="audit hook", module_path=hook_module))
    )

    assert specs == (AgentHookSpec(name="audit hook", module_path=hook_module.resolve()),)
    assert host_agent_hook_configs(specs) == [
        {"name": "audit hook", "module_path": str(hook_module.resolve())}
    ]
    assert container_agent_hook_configs(specs) == [
        {
            "name": "audit hook",
            "module_path": "/workspace/plugin-hooks/000-audit-hook.py",
        }
    ]


def test_agent_hook_modules_are_mounted_read_only(tmp_path: Path) -> None:
    hook_module = tmp_path / "audit.py"
    hook_module.touch()
    specs = (AgentHookSpec(name="audit", module_path=hook_module),)

    [mount] = agent_hook_mounts(specs)

    assert mount.host_path == str(hook_module)
    assert mount.container_path == "/workspace/plugin-hooks/000-audit.py"
    assert mount.readonly is True


def test_missing_and_duplicate_agent_hook_specs_are_ignored(tmp_path: Path) -> None:
    first_module = tmp_path / "first.py"
    first_module.touch()
    duplicate_module = tmp_path / "duplicate.py"
    duplicate_module.touch()

    specs = collect_agent_hook_specs(
        _plugin_manager(
            AgentHookSpec(name="audit", module_path=first_module),
            AgentHookSpec(name="audit", module_path=duplicate_module),
            AgentHookSpec(name="missing", module_path=tmp_path / "missing.py"),
        )
    )

    assert specs == (AgentHookSpec(name="audit", module_path=first_module.resolve()),)


def test_malformed_agent_hook_contributions_are_ignored(tmp_path: Path) -> None:
    manager = _plugin_manager()
    manager.hook.pynchy_agent_hook_specs = lambda: [
        "not-a-tuple",
        (object(),),
        (AgentHookSpec(name="  ", module_path=tmp_path / "blank.py"),),
    ]

    assert collect_agent_hook_specs(manager) == ()
