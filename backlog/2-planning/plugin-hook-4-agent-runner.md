# Plugin Hook: Step 4 - Load Hooks in Agent Runner

## Overview

Import hook plugin modules dynamically inside the agent container and register their hooks with the Claude Agent SDK.

## Scope

This step handles the container-side loading: reading hook configs from input, manipulating PYTHONPATH, importing modules, and merging hooks into ClaudeAgentOptions.

## Dependencies

- ✅ Step 1: HookPlugin base class
- ✅ Step 2: ContainerInput extended
- ✅ Step 3: Plugin sources mounted

## Implementation

### 1. Add Hook Loading Function

**File:** `container/agent_runner/src/agent_runner/main.py`

Add helper function to load hooks:

```python
def load_plugin_hooks(plugin_hooks: list[dict[str, str]]) -> dict[str, list]:
    """Load and merge hooks from plugin modules.

    Args:
        plugin_hooks: List of {name, module_path} configs

    Returns:
        Dict mapping hook event names to lists of hook functions
    """
    import importlib
    import sys

    merged_hooks: dict[str, list] = {}

    for hook_config in plugin_hooks:
        plugin_name = hook_config["name"]
        module_path = hook_config["module_path"]

        try:
            # Add plugin to PYTHONPATH so it can be imported
            plugin_dir = f"/workspace/plugins/{plugin_name}"
            if plugin_dir not in sys.path:
                sys.path.insert(0, plugin_dir)
                log(f"Added {plugin_dir} to PYTHONPATH")

            # Import the hook module
            log(f"Loading hook plugin: {plugin_name} from {module_path}")
            mod = importlib.import_module(module_path)

            # Get hooks from the module
            if not hasattr(mod, "create_hooks"):
                log(f"WARNING: {module_path} missing create_hooks() function")
                continue

            hooks = mod.create_hooks()
            if not isinstance(hooks, dict):
                log(f"WARNING: {module_path}.create_hooks() must return dict")
                continue

            # Merge hooks by event name
            for event_name, hook_fns in hooks.items():
                if not isinstance(hook_fns, list):
                    hook_fns = [hook_fns]

                if event_name not in merged_hooks:
                    merged_hooks[event_name] = []

                merged_hooks[event_name].extend(hook_fns)
                log(f"Registered {len(hook_fns)} hook(s) for {event_name} from {plugin_name}")

        except Exception as e:
            log(f"ERROR loading hook plugin {plugin_name}: {e}")
            # Continue with other plugins - don't crash

    return merged_hooks
```

### 2. Integrate Hook Loading into Agent Setup

**File:** `container/agent_runner/src/agent_runner/main.py`

Update the main agent setup to load and apply hooks:

```python
async def main() -> None:
    """Main entry point for the agent runner."""

    # ... existing setup ...

    # Read container input
    container_input = ContainerInput(input_data)

    # ... existing agent setup ...

    # Load plugin hooks
    plugin_hooks_config = container_input.plugin_hooks
    hook_functions = {}
    if plugin_hooks_config:
        log(f"Loading {len(plugin_hooks_config)} hook plugin(s)")
        hook_functions = load_plugin_hooks(plugin_hooks_config)

    # Configure agent options
    options = ClaudeAgentOptions(
        session_id=container_input.session_id,
        session_dir=Path(f"/workspace/.claude/sessions/{session_id}"),
        working_directory=Path(f"/workspace/group"),
        hooks=hook_functions,  # Apply loaded hooks
        # ... other options ...
    )

    # ... rest of agent execution ...
```

### 3. Hook Event Names

Common Claude Agent SDK hook events:
- `PreCompact` - Before conversation compaction
- `PostCompact` - After conversation compaction
- `Stop` - When agent is stopping
- `Error` - When an error occurs

Check Claude Agent SDK docs for the complete list.

## Tests

**File:** `tests/test_plugin_hook_integration.py`

```python
"""Integration tests for hook plugin loading."""

import tempfile
from pathlib import Path

import pytest

from pynchy.plugin import HookPlugin, PluginRegistry
from pynchy.types import ContainerInput
from pynchy.container_runner import run_container_agent


class TestHookPlugin(HookPlugin):
    """Test hook plugin that logs to a file."""

    name = "test-hook-logger"
    version = "0.1.0"
    description = "Test hook that logs events"

    def hook_module_path(self) -> str:
        return "test_hook_logger.hooks"


@pytest.fixture
def test_hook_plugin_source(tmp_path):
    """Create a test hook plugin package."""
    plugin_dir = tmp_path / "test_hook_logger"
    plugin_dir.mkdir()

    # Create __init__.py
    (plugin_dir / "__init__.py").write_text("")

    # Create hooks.py
    hooks_code = '''
import json
from pathlib import Path

def create_hooks():
    """Return hook functions."""
    log_file = Path("/tmp/hook_test.log")

    def on_pre_compact(context):
        with open(log_file, "a") as f:
            f.write("PreCompact fired\\n")

    def on_stop(context):
        with open(log_file, "a") as f:
            f.write("Stop fired\\n")

    return {
        "PreCompact": [on_pre_compact],
        "Stop": [on_stop],
    }
'''
    (plugin_dir / "hooks.py").write_text(hooks_code)

    return plugin_dir


@pytest.mark.asyncio
async def test_hook_plugin_loads(test_hook_plugin_source):
    """Test that hook plugins are loaded and executed."""
    # This would require running the actual container
    # For now, test the hook loading function in isolation
    from agent_runner.main import load_plugin_hooks

    plugin_hooks = [
        {"name": "test-hook-logger", "module_path": "test_hook_logger.hooks"}
    ]

    # Mock the plugin directory
    import sys
    sys.path.insert(0, str(test_hook_plugin_source.parent))

    try:
        hooks = load_plugin_hooks(plugin_hooks)
        assert "PreCompact" in hooks
        assert "Stop" in hooks
        assert len(hooks["PreCompact"]) == 1
        assert len(hooks["Stop"]) == 1
        assert callable(hooks["PreCompact"][0])
    finally:
        sys.path.remove(str(test_hook_plugin_source.parent))


def test_hook_plugin_error_handling():
    """Test that broken hook plugins don't crash the system."""
    from agent_runner.main import load_plugin_hooks

    # Plugin with broken module path
    plugin_hooks = [
        {"name": "broken", "module_path": "nonexistent.module"}
    ]

    # Should not raise, just log error and return empty dict
    hooks = load_plugin_hooks(plugin_hooks)
    assert hooks == {}
```

## Success Criteria

- [ ] `load_plugin_hooks()` function created in agent runner
- [ ] PYTHONPATH manipulation works correctly
- [ ] Hooks are imported and merged by event name
- [ ] Hooks are passed to ClaudeAgentOptions
- [ ] Error in one plugin doesn't crash the system
- [ ] Tests pass
- [ ] Hooks actually fire during agent execution

## Next Steps

After this is complete:
- Step 5: Error handling, security review, and edge cases
- Create example hook plugin for documentation
