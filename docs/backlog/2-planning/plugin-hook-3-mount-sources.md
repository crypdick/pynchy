# Plugin Hook: Step 3 - Mount Plugin Sources

## Overview

Collect hook plugin configurations and mount their source directories into the container so the agent runner can import them.

## Scope

This step handles the host-side preparation: discovering hook plugins, extracting their configs, and mounting their source code. The agent runner doesn't load them yet.

## Dependencies

- ✅ Step 1: HookPlugin base class
- ✅ Step 2: ContainerInput extended

## Implementation

### 1. Collect Hook Configs in container_runner.py

**File:** `src/pynchy/container_runner.py`

Add a helper function to collect hook configurations:

```python
def _collect_hook_configs(registry: Any) -> list[dict[str, str]]:
    """Collect hook plugin configurations.

    Args:
        registry: PluginRegistry containing discovered plugins

    Returns:
        List of hook configs with name and module_path
    """
    if not registry or not registry.hooks:
        return []

    hook_configs = []
    for plugin in registry.hooks:
        try:
            hook_configs.append({
                "name": plugin.name,
                "module_path": plugin.hook_module_path(),
            })
            logger.debug(
                "Collected hook plugin",
                name=plugin.name,
                module_path=plugin.hook_module_path(),
            )
        except Exception as e:
            logger.warning(
                "Failed to collect hook plugin config",
                name=plugin.name,
                error=str(e),
            )
    return hook_configs
```

### 2. Add Hook Plugin Mounts

**File:** `src/pynchy/container_runner.py`

Update `run_container_agent()` to mount hook plugin sources:

```python
async def run_container_agent(
    input_data: ContainerInput,
    *,
    session_dir: Path | None = None,
    registry: Any = None,
    # ... other params
) -> AsyncIterator[ContainerOutput]:
    """Run agent in container with streaming output."""

    # ... existing setup ...

    # Collect hook configs
    hook_configs = _collect_hook_configs(registry) if registry else []
    if hook_configs:
        input_data.plugin_hooks = hook_configs

    # ... existing mounts setup ...

    # Add hook plugin source mounts
    if registry and registry.hooks:
        for plugin in registry.hooks:
            try:
                source_path = plugin.plugin_source_path()
                if source_path and source_path.exists():
                    mounts.append(
                        VolumeMount(
                            host_path=str(source_path),
                            container_path=f"/workspace/plugins/{plugin.name}",
                            readonly=True,
                        )
                    )
                    logger.debug(
                        "Mounted hook plugin source",
                        name=plugin.name,
                        host_path=str(source_path),
                    )
            except Exception as e:
                logger.warning(
                    "Failed to mount hook plugin source",
                    name=plugin.name,
                    error=str(e),
                )

    # ... rest of function ...
```

### 3. Verify Mount Paths

The mounted plugins will be available at:
```
/workspace/plugins/
├── agent-logger/      # Hook plugin 1
├── agent-monitor/     # Hook plugin 2
└── ...
```

And can be imported via PYTHONPATH manipulation in the agent runner (next step).

## Tests

**File:** `tests/test_plugin_hook_mounting.py`

```python
"""Tests for hook plugin source mounting."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pynchy.plugin import HookPlugin, PluginRegistry
from pynchy.container_runner import _collect_hook_configs
from pynchy.types import ContainerInput


class MockHookPlugin(HookPlugin):
    """Mock hook plugin for testing."""

    name = "test-hook"
    version = "0.1.0"
    description = "Test hook"

    def hook_module_path(self) -> str:
        return "test_plugin.hooks"

    def plugin_source_path(self) -> Path:
        return Path("/tmp/test-plugin")


def test_collect_hook_configs_empty():
    """Test collecting hook configs with no plugins."""
    registry = PluginRegistry()
    configs = _collect_hook_configs(registry)
    assert configs == []


def test_collect_hook_configs_single():
    """Test collecting hook configs with one plugin."""
    registry = PluginRegistry()
    plugin = MockHookPlugin()
    registry.hooks.append(plugin)

    configs = _collect_hook_configs(registry)
    assert len(configs) == 1
    assert configs[0]["name"] == "test-hook"
    assert configs[0]["module_path"] == "test_plugin.hooks"


def test_collect_hook_configs_multiple():
    """Test collecting hook configs with multiple plugins."""
    registry = PluginRegistry()

    plugin1 = MockHookPlugin()
    plugin2 = type("Plugin2", (MockHookPlugin,), {
        "name": "test-hook-2",
        "hook_module_path": lambda self: "plugin2.hooks",
    })()

    registry.hooks.extend([plugin1, plugin2])

    configs = _collect_hook_configs(registry)
    assert len(configs) == 2
    assert configs[0]["name"] == "test-hook"
    assert configs[1]["name"] == "test-hook-2"


def test_collect_hook_configs_with_error():
    """Test collecting hook configs when a plugin raises an error."""

    class BrokenPlugin(HookPlugin):
        name = "broken"
        categories = ["hook"]

        def hook_module_path(self) -> str:
            raise RuntimeError("Broken plugin")

    registry = PluginRegistry()
    registry.hooks.append(BrokenPlugin())

    # Should not crash, just skip the broken plugin
    configs = _collect_hook_configs(registry)
    assert configs == []
```

## Success Criteria

- [ ] `_collect_hook_configs()` extracts hook plugin info
- [ ] Hook plugin sources are mounted to `/workspace/plugins/{name}/`
- [ ] Mounts use readonly=True for security
- [ ] Errors in individual plugins don't crash the system
- [ ] Tests pass
- [ ] Agent runner doesn't import hooks yet (comes in Step 4)

## Next Steps

After this is complete:
- Step 4: Import and register hooks in agent runner
- Step 5: Error handling and testing
