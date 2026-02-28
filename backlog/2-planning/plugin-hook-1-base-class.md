# Plugin Hook: Step 1 - Base Class & Discovery

## Overview

Create the `HookPlugin` base class and integrate it into the plugin discovery system.

## Scope

This step establishes the plugin interface without any container integration. It's purely about defining the contract and making hook plugins discoverable.

## Dependencies

- ✅ Plugin discovery system (already implemented)

## Implementation

### 1. Create HookPlugin Base Class

**File:** `src/pynchy/plugins/hook.py`

```python
"""Hook plugin system for agent lifecycle events.

Enables plugins to hook into agent lifecycle events (PreCompact, Stop, etc.)
provided by the Claude Agent SDK.
"""

from __future__ import annotations

from abc import abstractmethod
from pathlib import Path

from pynchy.plugins.base import PluginBase


class HookPlugin(PluginBase):
    """Base class for hook plugins.

    Hook plugins provide agent lifecycle hooks by exposing Python modules
    that define hook functions. The plugin source is mounted into the
    container and imported dynamically by the agent runner.
    """

    categories = ["hook"]  # Fixed category for all hook plugins

    @abstractmethod
    def hook_module_path(self) -> str:
        """Return Python module path that provides hooks.

        The module must export a `create_hooks()` function that returns
        a dict mapping hook event names to lists of hook functions.

        Example:
            return "pynchy_plugin_agent_logger.hooks"

        The module's create_hooks() should return:
            {
                "PreCompact": [hook_fn1, hook_fn2],
                "Stop": [hook_fn3],
            }

        Returns:
            String module path (e.g., "package.module")
        """
        ...

    def plugin_source_path(self) -> Path | None:
        """Return path to plugin package directory to mount into container.

        By default, returns the parent directory of the plugin module file.
        Override if your plugin structure differs.

        The directory will be mounted to /workspace/plugins/{plugin-name}/
        inside the container so the hook module can be imported.

        Returns:
            Path to plugin source directory, or None to skip mounting
        """
        import inspect

        module = inspect.getmodule(self.__class__)
        if module and hasattr(module, "__file__") and module.__file__:
            return Path(module.__file__).parent
        return None
```

### 2. Export HookPlugin from plugin module

**File:** `src/pynchy/plugins/__init__.py`

Add to imports:
```python
from pynchy.plugins.hook import HookPlugin
```

Add to `__all__`:
```python
"HookPlugin",
```

### 3. Register in PluginRegistry

Already handled - the existing discovery code registers plugins by category:
```python
if "hook" in plugin.categories:
    registry.hooks.append(plugin)
```

## Tests

**File:** `tests/test_plugin_hook.py`

```python
"""Tests for HookPlugin base class."""

from pathlib import Path

import pytest

from pynchy.plugins import HookPlugin, discover_plugins


class TestHookPlugin(HookPlugin):
    """Minimal test hook plugin."""

    name = "test-hook"
    version = "0.1.0"
    description = "Test hook plugin"

    def hook_module_path(self) -> str:
        return "test_hooks"


def test_hook_plugin_creation():
    """Test creating a hook plugin instance."""
    plugin = TestHookPlugin()
    assert plugin.name == "test-hook"
    assert plugin.categories == ["hook"]
    assert plugin.hook_module_path() == "test_hooks"


def test_hook_plugin_validation():
    """Test hook plugin validation passes."""
    plugin = TestHookPlugin()
    plugin.validate()  # Should not raise


def test_hook_plugin_source_path():
    """Test default plugin_source_path implementation."""
    plugin = TestHookPlugin()
    source_path = plugin.plugin_source_path()
    # Should return the directory containing this test file
    assert source_path is not None
    assert isinstance(source_path, Path)
    assert source_path.exists()


def test_hook_plugin_invalid_category():
    """Test validation fails for invalid category."""

    class BadHookPlugin(HookPlugin):
        name = "bad"
        categories = ["invalid"]

        def hook_module_path(self) -> str:
            return "foo"

    plugin = BadHookPlugin()
    with pytest.raises(ValueError, match="Invalid category"):
        plugin.validate()
```

## Success Criteria

- [ ] `HookPlugin` base class created in `src/pynchy/plugins/hook.py`
- [ ] Exported from `src/pynchy/plugins/__init__.py`
- [ ] Tests pass (basic instantiation, validation, discovery)
- [ ] No changes to container integration yet (comes in later steps)

## Next Steps

After this is complete:
- Step 2: Extend ContainerInput to support hook configs
- Step 3: Collect and mount hook plugin sources
- Step 4: Import and register hooks in agent runner
