# Plugin Hook: Step 5 - Polish & Documentation

## Overview

Final polish: error handling, security review, documentation, and creating a reference example plugin.

## Scope

This step ensures the hook plugin system is production-ready with proper error handling, documentation, and a working example.

## Dependencies

- ✅ Step 1: HookPlugin base class
- ✅ Step 2: ContainerInput extended
- ✅ Step 3: Plugin sources mounted
- ✅ Step 4: Hooks loaded in agent runner

## Implementation

### 1. Enhanced Error Handling

**File:** `container/agent_runner/src/agent_runner/main.py`

Improve error handling in hook loading:

```python
def load_plugin_hooks(plugin_hooks: list[dict[str, str]]) -> dict[str, list]:
    """Load and merge hooks from plugin modules.

    Robust error handling ensures individual plugin failures don't crash
    the agent. All errors are logged but skipped.

    Args:
        plugin_hooks: List of {name, module_path} configs

    Returns:
        Dict mapping hook event names to lists of hook functions
    """
    import importlib
    import sys
    import traceback

    merged_hooks: dict[str, list] = {}

    for hook_config in plugin_hooks:
        plugin_name = hook_config.get("name", "unknown")
        module_path = hook_config.get("module_path")

        if not module_path:
            log(f"ERROR: Hook config missing module_path: {hook_config}")
            continue

        try:
            # Add plugin to PYTHONPATH
            plugin_dir = f"/workspace/plugins/{plugin_name}"
            if not Path(plugin_dir).exists():
                log(f"WARNING: Plugin directory not found: {plugin_dir}")
                continue

            if plugin_dir not in sys.path:
                sys.path.insert(0, plugin_dir)

            # Import the hook module
            log(f"Loading hook plugin: {plugin_name} from {module_path}")
            mod = importlib.import_module(module_path)

            # Validate module has create_hooks
            if not hasattr(mod, "create_hooks"):
                log(f"ERROR: {module_path} missing create_hooks() function")
                continue

            # Call create_hooks
            hooks = mod.create_hooks()
            if not isinstance(hooks, dict):
                log(f"ERROR: {module_path}.create_hooks() returned {type(hooks)}, expected dict")
                continue

            # Merge hooks by event name
            for event_name, hook_fns in hooks.items():
                if not isinstance(hook_fns, list):
                    hook_fns = [hook_fns]

                # Validate all items are callable
                for fn in hook_fns:
                    if not callable(fn):
                        log(f"WARNING: Non-callable hook in {event_name} from {plugin_name}: {fn}")
                        continue

                if event_name not in merged_hooks:
                    merged_hooks[event_name] = []

                merged_hooks[event_name].extend(hook_fns)

            log(f"✓ Loaded {plugin_name}: {sum(len(fns) for fns in hooks.values())} hook(s)")

        except ImportError as e:
            log(f"ERROR importing {plugin_name}: {e}")
            log(f"  Module path: {module_path}")
            log(f"  sys.path: {sys.path[:3]}...")  # Show first few entries
        except Exception as e:
            log(f"ERROR loading hook plugin {plugin_name}: {e}")
            log(f"  Traceback:\n{traceback.format_exc()}")

    if merged_hooks:
        log(f"Hook plugins loaded: {list(merged_hooks.keys())}")
    else:
        log("No hook plugins loaded")

    return merged_hooks
```

### 2. Create Example Hook Plugin

**File:** `docs/examples/hook-plugin/README.md`

```markdown
# Example: Hook Plugin

This example demonstrates creating a hook plugin that logs agent lifecycle events.

## Structure

```
pynchy-plugin-logger/
├── pyproject.toml
└── src/
    └── pynchy_plugin_logger/
        ├── __init__.py
        ├── plugin.py
        └── hooks.py
```

## Installation

```bash
uv pip install -e /path/to/pynchy-plugin-logger
```

## Files

See the plugin implementation in this directory.
```

**File:** `docs/examples/hook-plugin/pyproject.toml`

```toml
[project]
name = "pynchy-plugin-logger"
version = "0.1.0"
description = "Example hook plugin that logs agent lifecycle events"
dependencies = ["pynchy"]

[project.entry-points."pynchy.plugins"]
logger = "pynchy_plugin_logger:LoggerPlugin"
```

**File:** `docs/examples/hook-plugin/plugin.py`

```python
"""Example hook plugin implementation."""

from pynchy.plugin import HookPlugin


class LoggerPlugin(HookPlugin):
    """Logs agent lifecycle events to stderr."""

    name = "logger"
    version = "0.1.0"
    description = "Logs agent lifecycle events"

    def hook_module_path(self) -> str:
        return "pynchy_plugin_logger.hooks"
```

**File:** `docs/examples/hook-plugin/hooks.py`

```python
"""Hook functions for the logger plugin."""

import sys
from datetime import datetime


def create_hooks():
    """Return hook functions for agent lifecycle events."""

    def on_pre_compact(context):
        """Called before conversation compaction."""
        ts = datetime.now().isoformat()
        msg_count = len(context.messages) if hasattr(context, "messages") else "?"
        print(f"[{ts}] PreCompact: {msg_count} messages", file=sys.stderr, flush=True)

    def on_stop(context):
        """Called when agent is stopping."""
        ts = datetime.now().isoformat()
        print(f"[{ts}] Stop: Agent shutting down", file=sys.stderr, flush=True)

    return {
        "PreCompact": [on_pre_compact],
        "Stop": [on_stop],
    }
```

### 3. Documentation Updates

**File:** `docs/plugins.md` (or add section to existing docs)

```markdown
# Hook Plugins

Hook plugins enable you to execute custom code at key points in the agent lifecycle.

## Available Hook Events

Check the Claude Agent SDK documentation for the complete list of hook events. Common ones include:

- `PreCompact` - Before conversation history is compacted
- `PostCompact` - After conversation history is compacted
- `Stop` - When the agent is stopping

## Creating a Hook Plugin

1. Create a plugin class extending `HookPlugin`
2. Implement `hook_module_path()` to return your hook module
3. Create a `hooks.py` module with a `create_hooks()` function
4. Register via entry points in `pyproject.toml`

See `docs/examples/hook-plugin/` for a complete example.

## Hook Function Signature

Hook functions receive a `HookContext` object with information about the agent's state:

```python
def my_hook(context: HookContext) -> None:
    # Access context.messages, context.session_id, etc.
    pass
```

## Error Handling

If a hook raises an exception, it's logged but doesn't crash the agent. Other hooks still execute.

## Security Considerations

Hook plugins run inside the container with the same permissions as the agent. They can:
- Read/write files in mounted directories
- Access environment variables
- Execute arbitrary code

Only install hook plugins from trusted sources.
```

### 4. Security Review

Document security considerations:

**File:** `docs/SECURITY.md` (add section)

```markdown
## Hook Plugins

Hook plugins execute arbitrary code inside the agent container. Security considerations:

- **Sandboxing**: Hooks run in the container, not on the host
- **Filesystem**: Hooks can only access mounted directories
- **Trust**: Only install plugins from trusted sources
- **Review**: Read plugin source code before installing
- **Isolation**: Each group has its own container instance

Hooks are read-only mounted, but they execute with full agent permissions inside the container.
```

## Tests

**File:** `tests/test_plugin_hook_errors.py`

```python
"""Tests for hook plugin error handling."""

import pytest

from agent_runner.main import load_plugin_hooks


def test_missing_module_path():
    """Test handling of hook config missing module_path."""
    hooks = load_plugin_hooks([{"name": "broken"}])
    assert hooks == {}


def test_nonexistent_plugin_directory():
    """Test handling of missing plugin directory."""
    hooks = load_plugin_hooks([
        {"name": "nonexistent", "module_path": "foo.hooks"}
    ])
    assert hooks == {}


def test_module_without_create_hooks():
    """Test handling of module missing create_hooks()."""
    # Would need to create a test module without create_hooks
    pass


def test_create_hooks_returns_non_dict():
    """Test handling of create_hooks returning wrong type."""
    # Would need to create a test module that returns wrong type
    pass


def test_non_callable_hook():
    """Test handling of non-callable hook in list."""
    # Would need to create a test module with invalid hooks
    pass
```

## Success Criteria

- [ ] Enhanced error handling with detailed logging
- [ ] Example hook plugin created and documented
- [ ] Hook plugin documentation written
- [ ] Security considerations documented
- [ ] All edge cases tested
- [ ] Example plugin can be installed and works

## Final Verification

1. Install example plugin: `uv pip install -e docs/examples/hook-plugin/`
2. Run agent and verify hooks fire
3. Check logs for hook execution messages
4. Uninstall plugin and verify hooks no longer execute
5. Test with broken plugin to verify error handling

## Next Steps

After this is complete, the hook plugin system is production-ready! Consider:
- Creating more example plugins (metrics, monitoring, etc.)
- Contributing plugins to the ecosystem
- Documentation updates as the SDK evolves
