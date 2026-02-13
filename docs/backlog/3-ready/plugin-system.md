# Plugin System Overview

> **Status: Split into separate plans** — This was a large monolithic plan that has been broken down into independent, more manageable pieces. See the individual plans in `2-planning/` for details.

## Summary

The plugin system enables extending Pynchy with external packages. Plugins can provide:
- Alternative container runtimes
- New communication channels
- Agent tools (MCP servers)
- Agent skills/instructions
- Agent lifecycle hooks

## Individual Plans

Each plugin type has its own plan document:

1. **[Plugin Discovery](../2-planning/plugin-discovery.md)** — Core infrastructure (implement first)
   - Python entry points discovery
   - Plugin registry
   - Manifest format
   - Base classes

2. **[Runtime Plugins](../2-planning/plugin-runtime.md)** — Alternative container runtimes
   - Apple Container, Podman, etc.
   - Platform matching & priority
   - Runtime selection logic

3. **[Channel Plugins](../2-planning/plugin-channel.md)** — Communication platforms
   - Telegram, Slack, Discord, etc.
   - Multi-channel support already exists

4. **[MCP Plugins](../2-planning/plugin-mcp.md)** — Agent tools
   - Voice, calendar, password managers, etc.
   - Container mounting & PYTHONPATH

5. **[Skill Plugins](../2-planning/plugin-skill.md)** — Agent capabilities
   - Instructions and examples
   - Synced to session directory

6. **[Hook Plugins](../2-planning/plugin-hook.md)** — Lifecycle events
   - PreCompact, Stop, etc.
   - Most complex (implement last)

## Shared Manifest

All plugins use a common manifest format in their `pyproject.toml`:

```toml
[project]
name = "pynchy-plugin-foo"
version = "0.1.0"
dependencies = ["pynchy"]

[project.entry-points."pynchy.plugins"]
foo = "pynchy_plugin_foo:FooPlugin"
```

Each plugin class declares its capabilities via attributes:

```python
class FooPlugin(PluginBase):
    name = "foo"
    version = "0.1.0"
    categories = ["channel", "mcp"]  # Can be multiple
    description = "Foo integration"
```

## Implementation Order

1. **Plugin Discovery** (foundation for all others)
2. **Runtime** OR **Channel** OR **MCP** (pick one, they're independent)
3. **Skill** (straightforward, low complexity)
4. **Hook** (most complex, save for last)

Each can be implemented independently as long as Plugin Discovery is done first.

## Design Principles

- **Install = activate**: No config files, plugins are active when installed
- **Entry points**: Standard Python discovery via `importlib.metadata`
- **Composites**: A plugin can provide multiple capabilities (e.g., Telegram = channel + MCP)
- **Graceful failure**: Broken plugins are logged and skipped, don't crash pynchy
- **Minimal core**: Only Docker/WhatsApp built-in, everything else is pluggable

## Dependencies Between Plans

```
plugin-discovery (base)
    ├── plugin-runtime (independent)
    ├── plugin-channel (independent)
    ├── plugin-mcp (independent)
    ├── plugin-skill (independent)
    └── plugin-hook (independent, most complex)
```

All plugin types depend on discovery, but are otherwise independent of each other.
