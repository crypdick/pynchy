# Hook Plugins

## Overview

Enable plugins to hook into agent lifecycle events (PreCompact, Stop, etc.) provided by the Claude Agent SDK.

## Dependencies

- Plugin discovery system (plugin-discovery.md)

## Design

**Note:** This is the most complex plugin type and should be implemented last.

### HookPlugin Class

```python
class HookPlugin(PluginBase):
    """Base class for hook plugins."""

    categories = ["hook"]  # Fixed

    @abstractmethod
    def hook_module_path(self) -> str:
        """Return Python module path that provides hooks.

        The module must export a `create_hooks()` function that returns
        a dict of hook name -> list of hook functions.

        Example: "pynchy_plugin_foo.hooks"
        """
        ...
```

## The Hook Serialization Problem

Hooks are callables — they can't be serialized through JSON input to the container. We need a different approach:

### Solution: Import Hook Modules in Container

1. **Plugin provides module path** (not callable objects)
2. **Plugin source mounted** into container at `/workspace/plugins/{name}/`
3. **Agent runner imports module** dynamically:
   ```python
   import importlib
   mod = importlib.import_module(plugin_hook_module_path)
   hooks = mod.create_hooks()
   ```
4. **Merge hooks** into `ClaudeAgentOptions.hooks`

## Example: Logging Hook Plugin

**pyproject.toml:**
```toml
[project]
name = "pynchy-plugin-agent-logger"
dependencies = ["pynchy"]

[project.entry-points."pynchy.plugins"]
agent-logger = "pynchy_plugin_agent_logger:AgentLoggerPlugin"
```

**plugin.py:**
```python
from pynchy.plugin import HookPlugin

class AgentLoggerPlugin(HookPlugin):
    name = "agent-logger"
    version = "0.1.0"
    description = "Enhanced agent lifecycle logging"

    def hook_module_path(self) -> str:
        return "pynchy_plugin_agent_logger.hooks"
```

**hooks.py:**
```python
from claude_agent_sdk import PreCompactHook, StopHook

def create_hooks():
    """Return hooks dict for agent runner."""
    def on_pre_compact(context):
        print(f"PreCompact: {len(context.messages)} messages")

    def on_stop(context):
        print("Agent stopped")

    return {
        "PreCompact": [on_pre_compact],
        "Stop": [on_stop],
    }
```

## Container Integration

The agent runner needs to:

1. **Read hook configs** from `ContainerInput`:
   ```python
   plugin_hooks = input_data.get("plugin_hooks", [])
   # List of {name: str, module_path: str}
   ```

2. **Set PYTHONPATH** so plugin modules are importable:
   ```python
   sys.path.insert(0, f"/workspace/plugins/{hook_name}")
   ```

3. **Import and merge hooks:**
   ```python
   for hook_config in plugin_hooks:
       mod = importlib.import_module(hook_config["module_path"])
       hook_fns = mod.create_hooks()

       for event_name, fns in hook_fns.items():
           if event_name not in options.hooks:
               options.hooks[event_name] = []
           options.hooks[event_name].extend(fns)
   ```

## Implementation Steps

1. Define `HookPlugin` base class in `plugin/hook.py`
2. Extend `ContainerInput` in `types.py`:
   - Add `plugin_hooks: list[dict] | None = None`
3. Update `container_runner.py`:
   - Collect hook configs from plugins
   - Include in container input
4. Update `agent_runner/main.py`:
   - Read plugin_hooks from input
   - Import modules dynamically
   - Merge hooks into options
5. Tests: hook registration, invocation, multiple plugins

## Integration Points

- `container_runner.py:_input_to_dict()` — includes hook configs
- `agent_runner/main.py` — imports and registers hooks
- Claude Agent SDK — hook mechanism already exists

## Open Questions

- **Security**: Should we sandbox hook execution?
- **Error handling**: What if a hook raises an exception?
- **Hook ordering**: Can plugins control execution order?
- **Hook conflicts**: What if multiple plugins hook the same event?
- **Hot reloading**: Can hooks be added/removed without restart?
- **Async hooks**: Do we need async hook support?

## Complexity Warning

This is the trickiest plugin type because:
- Requires dynamic module importing
- PYTHONPATH manipulation
- Error handling for plugin code
- Security implications of arbitrary code execution

**Recommendation:** Implement this last, after other plugin types are stable and tested.

## Verification

1. Create test hook plugin that logs to a file
2. Install: `uv pip install -e /tmp/pynchy-plugin-test-hook`
3. Start agent, trigger hook event
4. Verify hook was called (check log file)
5. Test with multiple hook plugins
6. Verify error handling when hook fails
7. Uninstall and verify hooks no longer execute
