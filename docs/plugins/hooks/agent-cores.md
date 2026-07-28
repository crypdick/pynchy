# Agent Core and Lifecycle Hooks

## `pynchy_agent_core_info`

Provide an alternative LLM agent framework. Pynchy collects every core and
selects one with `[agent].default_core` or `AGENT__DEFAULT_CORE`.

```python
from pathlib import Path

from pynchy.plugins.contracts import AgentCoreSpec


@hookimpl
def pynchy_agent_core_info(self) -> AgentCoreSpec:
    return AgentCoreSpec(
        name="ollama",
        module="pynchy_plugin_ollama.core",
        class_name="OllamaAgentCore",
        packages=("ollama>=0.1.0",),
        host_source_path=Path(__file__).parent,
    )
```

| Field | Type | Description |
|-------|------|-------------|
| `name` | `str` | Unique core identifier. |
| `module` | `str` | Module importable inside the container. |
| `class_name` | `str` | Core class to instantiate. |
| `packages` | `tuple[str, ...]` | Packages to install in the container. |
| `host_source_path` | `Path \| None` | Host source mounted at `/workspace/plugins/{name}/`. |

For selecting built-in cores, see [Agent cores](../../usage/agent-cores.md).

## `pynchy_agent_hook_specs`

Provide trusted Python modules that extend the runner lifecycle:

```python
from pathlib import Path

from pynchy.plugins.contracts import AgentHookSpec


@hookimpl
def pynchy_agent_hook_specs(self) -> tuple[AgentHookSpec, ...]:
    return (
        AgentHookSpec(
            name="company-policy",
            module_path=Path(__file__).parent / "agent_hooks.py",
        ),
    )
```

The module can export functions named after runner lifecycle events, including
`before_tool_use`, `before_query`, `after_query`, `before_compact`,
`after_compact`, `session_start`, `session_end`, and `error`. A
`before_tool_use(tool_name, tool_input)` handler can return a `HookDecision` to
allow or deny the operation.

Pynchy resolves each module on the host. Container sessions receive a
read-only file mount, while direct-host sessions use the resolved host path.
All built-in cores load the same declarations. Built-in security checks run
before plugin-provided `before_tool_use` handlers, so an extension cannot
bypass the owned gate.

Agent hook modules execute as trusted code in the agent runner. Install them
only from trusted plugin authors.

## `pynchy_before_context_reset`

Settle one plugin-owned concern before Pynchy clears a workspace's provider
session, chat history, and security taint.
Use this hook when a plugin owns durable execution, leases, or provider state
that a context reset must cancel or transition first.

```python
from pynchy.workspace.api import WorkspaceProfile


@hookimpl
async def pynchy_before_context_reset(self, group: WorkspaceProfile) -> None:
    await cancel_plugin_execution(group.folder)
```

Pynchy awaits every implementation before destructive cleanup. Keep each hook
focused on one plugin concern. If an implementation raises or returns a
non-awaitable result, the reset stops before Pynchy clears the session.
