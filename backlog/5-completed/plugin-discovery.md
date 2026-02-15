# Plugin Discovery & Registry System

## Overview

Core plugin infrastructure that enables discovery and registration of external extensions via Python entry points. This is the foundation that all other plugin types depend on.

## Design

### Discovery Mechanism

Use Python's `importlib.metadata` entry points system. Plugins register themselves via `pyproject.toml`:

```toml
[project.entry-points."pynchy.plugins"]
my-plugin = "pynchy_plugin_foo:FooPlugin"
```

Installation = activation. Uninstall = removal. No config files needed.

### Plugin Manifest

Each plugin class defines its capabilities via class attributes:

```python
class MyPlugin:
    name: str               # Unique identifier
    version: str            # Semantic version
    categories: list[str]   # ["runtime", "channel", "mcp", "skill", "hook"]
    description: str        # Human-readable description
```

### Plugin Registry

Central registry that holds discovered plugins:

```python
@dataclass
class PluginRegistry:
    all_plugins: list[PluginBase]           # All discovered plugins
    runtimes: list[RuntimePlugin]           # Filtered by category
    channels: list[ChannelPlugin]
    mcp_servers: list[McpPlugin]
    skills: list[SkillPlugin]
    hooks: list[HookPlugin]
```

### Discovery Function

```python
def discover_plugins() -> PluginRegistry:
    """Discover all installed plugins via entry points."""
    registry = PluginRegistry()

    for ep in entry_points(group="pynchy.plugins"):
        try:
            plugin = ep.load()()  # Instantiate
            registry.all_plugins.append(plugin)

            # Register in category-specific lists based on manifest
            if "runtime" in plugin.categories:
                registry.runtimes.append(plugin)
            if "channel" in plugin.categories:
                registry.channels.append(plugin)
            # ... etc for other categories

        except Exception as e:
            logger.warning("Failed to load plugin", name=ep.name, error=str(e))

    return registry
```

**Note**: Uses `if` not `elif` — a composite plugin can register in multiple categories.

### Base Plugin Class

```python
class PluginBase(ABC):
    """All plugins extend this base class."""

    name: str
    version: str = "0.1.0"
    categories: list[str]  # Must be non-empty
    description: str = ""

    def validate(self) -> None:
        """Called during discovery. Raise ValueError if invalid."""
        if not self.name:
            raise ValueError("Plugin must have a name")
        if not self.categories:
            raise ValueError("Plugin must declare at least one category")
```

## Implementation Files

| File | Action | Purpose |
|------|--------|---------|
| `src/pynchy/plugin/base.py` | Create | PluginBase ABC, PluginRegistry dataclass |
| `src/pynchy/plugin/__init__.py` | Create | Public exports (discover_plugins, PluginRegistry) |
| `src/pynchy/app.py` | Modify | Call discover_plugins() at startup, store in self.registry |
| `tests/test_plugin_discovery.py` | Create | Test discovery, validation, error handling |

## Validation

1. Create minimal test plugin:
   ```python
   class TestPlugin(PluginBase):
       name = "test"
       categories = ["mcp"]
   ```

2. Install it: `uv pip install -e /tmp/pynchy-plugin-test`

3. Verify discovery:
   ```python
   from pynchy.plugin import discover_plugins
   registry = discover_plugins()
   assert any(p.name == "test" for p in registry.all_plugins)
   ```

4. Uninstall and verify removal:
   ```bash
   uv pip uninstall pynchy-plugin-test
   # Plugin should no longer appear in registry
   ```

## Open Questions

- Should plugins declare minimum pynchy version requirement?
- How to handle plugin conflicts (two plugins with same name)?
- Should discovery cache plugins or re-scan on every startup?
- Error handling: fail fast vs skip broken plugins?

## Dependencies

None - this is the foundation that other plugin types build on.

## Next Steps

After this is implemented:
1. RuntimePlugin can use the discovery system
2. ChannelPlugin can use the discovery system
3. McpPlugin can use the discovery system
4. etc.
