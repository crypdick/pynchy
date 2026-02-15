# Migrate Plugin System to Pluggy

Replace the manual plugin system with `pluggy` (pytest's plugin framework) for robust, type-safe plugin management.

**Strategy:** Single-phase immediate cutover. No third-party plugins exist yet, so no backward compatibility needed.

## Context

Current plugin system uses manual entry point discovery, abstract base classes, and category-based registration. While functional, it lacks:

- **Type safety** - No enforcement of hook signatures between specs and implementations
- **Validation** - Manual validation in `PluginBase.validate()`
- **Hook orchestration** - Simple list iteration, no calling strategies (firstresult, wrappers, etc.)
- **Plugin management** - No built-in way to disable/enable specific plugins
- **Error handling** - Manual try/catch around entry point loading

Pluggy provides all of this out-of-the-box and is battle-tested (powers pytest, tox, devpi).

## Current Architecture

```
src/pynchy/plugin/
  base.py          # PluginBase ABC, PluginRegistry dataclass
  __init__.py      # discover_plugins() - manual entry point iteration
  channel.py       # ChannelPlugin ABC
  mcp.py           # McpPlugin ABC
  skill.py         # SkillPlugin ABC
  agent_core.py    # AgentCorePlugin ABC
  builtin_*.py     # Built-in plugin implementations

Usage:
  registry = discover_plugins()
  for plugin in registry.agent_cores:
      module = plugin.core_module()
```

**Entry points:** Plugins register via `pynchy.plugins` group in `pyproject.toml`

**Category system:** Plugins declare `categories = ["agent_core"]` and are sorted into category-specific lists

## Plugin Categories

Pynchy has **four plugin categories**, each serving a different purpose:

| Category | Purpose | Hook Name | Current Implementations |
|----------|---------|-----------|------------------------|
| **agent_core** | LLM agent frameworks (Claude, OpenAI, Ollama) | `pynchy_agent_core_info()` | Claude (built-in) |
| **channel** | Communication channels (WhatsApp, Discord, Slack) | `pynchy_create_channel()` | WhatsApp (built-in) |
| **mcp** | Agent tools via MCP servers | `pynchy_mcp_server_spec()` | None yet |
| **skill** | Agent skills/capabilities | `pynchy_skill_paths()` | agent-browser (built-in) |

**Key insight:** A single plugin can implement multiple categories. For example, a "voice" plugin might provide:
- MCP server for voice transcription tools (`pynchy_mcp_server_spec`)
- Skill for voice interaction patterns (`pynchy_skill_paths`)

With pluggy, plugins simply implement whichever hooks they need. No more manual `categories = ["mcp", "skill"]` attribute.

## Proposed Architecture with Pluggy

### Hook Specifications

Define hook specs for each plugin category:

```python
# src/pynchy/plugin/hookspecs.py
import pluggy

hookspec = pluggy.HookspecMarker("pynchy")

class PynchySpec:
    """Hook specifications for pynchy plugins."""

    @hookspec
    def pynchy_agent_core_info(self) -> dict[str, str]:
        """Provide agent core implementation info.

        Returns:
            Dict with keys: name, module, class_name, packages (list), host_source_path
        """

    @hookspec
    def pynchy_mcp_server_spec(self) -> dict[str, Any]:
        """Provide MCP server specification.

        Returns:
            Dict with keys: name, command, args, env, host_source
        """

    @hookspec
    def pynchy_skill_paths(self) -> list[str]:
        """Provide paths to skill directories.

        Returns:
            List of absolute paths to skill directories
        """

    @hookspec(firstresult=True)
    def pynchy_create_channel(self, context) -> ChannelProtocol | None:
        """Create a communication channel instance.

        Args:
            context: PluginContext with callbacks

        Returns:
            Channel instance or None if this plugin doesn't provide channels
        """
```

### Plugin Implementations

Plugins implement hooks using `@hookimpl`:

```python
# Built-in Claude core plugin
import pluggy

hookimpl = pluggy.HookimplMarker("pynchy")

class ClaudeAgentCorePlugin:
    """Built-in Claude SDK agent core."""

    @hookimpl
    def pynchy_agent_core_info(self):
        return {
            "name": "claude",
            "module": "agent_runner.cores.claude",
            "class_name": "ClaudeAgentCore",
            "packages": [],
            "host_source_path": None,
        }
```

### Plugin Manager

Replace `discover_plugins()` with pluggy PluginManager:

```python
# src/pynchy/plugin/__init__.py
import pluggy
from .hookspecs import PynchySpec

def get_plugin_manager() -> pluggy.PluginManager:
    """Create and configure the plugin manager."""
    pm = pluggy.PluginManager("pynchy")
    pm.add_hookspecs(PynchySpec)

    # Discover and register plugins from entry points
    pm.load_setuptools_entrypoints("pynchy")

    # Register built-in plugins
    from .builtin_agent_claude import ClaudeAgentCorePlugin
    pm.register(ClaudeAgentCorePlugin())

    return pm

# Usage
pm = get_plugin_manager()
cores = pm.hook.pynchy_agent_core_info()  # List of dicts from all plugins
```

## Migration Strategy

**Single-phase immediate cutover.** No third-party plugins exist yet, so no backward compatibility needed.

### Implementation Steps

1. **Add pluggy dependency**
   ```bash
   uv add pluggy
   ```

2. **Create hook specifications**
   - New file: `src/pynchy/plugin/hookspecs.py`
   - Define hookspecs for all plugin categories
   - One hook per category (agent_core, mcp, skill, channel)

3. **Replace `discover_plugins()` with `get_plugin_manager()`**
   - Update `src/pynchy/plugin/__init__.py`
   - Remove manual entry point iteration
   - Use `pm.load_setuptools_entrypoints("pynchy")`

4. **Convert built-in plugins to `@hookimpl`**
   - Remove ABC inheritance
   - Replace abstract methods with single `@hookimpl` method
   - Return dict instead of multiple method calls

5. **Update all call sites**
   - Replace `registry.agent_cores[0].core_module()` with:
   - `pm.hook.pynchy_agent_core_info()[0]["module"]`
   - Update `app.py`, `task_scheduler.py`, `container_runner.py`

6. **Delete old plugin system**
   - Remove `src/pynchy/plugin/base.py` (PluginBase, PluginRegistry)
   - Remove ABC classes (AgentCorePlugin, McpPlugin, SkillPlugin, ChannelPlugin)
   - Keep only hookspecs and `get_plugin_manager()`

7. **Update tests**
   - Replace ABC-based tests with hookspec tests
   - Verify hook calling works correctly
   - Integration tests unchanged (same behavior, different implementation)

## Hook Calling Strategies

Pluggy provides multiple calling strategies:

### Default (All Results)
```python
# Returns list of all plugin results
cores = pm.hook.pynchy_agent_core_info()
# [{"name": "claude", ...}, {"name": "openai", ...}]
```

### First Result
```python
@hookspec(firstresult=True)
def pynchy_create_channel(self, context): ...

# Returns first non-None result, stops calling after that
channel = pm.hook.pynchy_create_channel(context=ctx)
```

### Wrappers
```python
@hookimpl(hookwrapper=True)
def pynchy_agent_core_info(self):
    # Can modify results from other plugins
    outcome = yield
    results = outcome.get_result()
    # Modify results...
    outcome.force_result(modified_results)
```

**Use cases:**
- **All results** - Agent cores, MCP servers, skills (collect from all plugins)
- **First result** - Channel creation (only one channel wins)
- **Wrappers** - Logging, validation, transformation (cross-cutting concerns)

## Plugin Registration

### Entry Points (Third-Party Plugins)

Plugins register via `pyproject.toml` as before, but group name stays `pynchy`:

```toml
[project.entry-points."pynchy"]
my_plugin = "my_package.plugin:MyPlugin"
```

Pluggy discovers these automatically via `load_setuptools_entrypoints("pynchy")`.

### Direct Registration (Built-In Plugins)

Built-in plugins registered directly in code:

```python
pm.register(ClaudeAgentCorePlugin())
pm.register(WhatsAppChannelPlugin())
```

## Plugin Installation & Storage

### Built-In Plugins

**Location:** Inside the pynchy package at `src/pynchy/plugin/builtin_*.py`

**Registration:** Directly in `get_plugin_manager()` via `pm.register()`

**Examples:**
- `builtin_agent_claude.py` - Claude SDK agent core
- `builtin_channel_whatsapp.py` - WhatsApp channel (future)
- `builtin_skill_browser.py` - agent-browser skill (future)

**Packaging:** Bundled with pynchy, no separate installation needed.

### Third-Party Plugins (Future)

**Installation methods:**

1. **PyPI (Standard Python Packages)**
   ```bash
   uv pip install pynchy-plugin-voice
   ```
   - Plugin package declares entry point in its `pyproject.toml`
   - Pluggy auto-discovers via `load_setuptools_entrypoints("pynchy")`
   - No configuration needed, works immediately after install

2. **GitHub/Git (Development or Private Plugins)**
   ```bash
   uv pip install git+https://github.com/user/pynchy-plugin-custom.git
   ```
   - Installs from git repository directly
   - Entry points work the same as PyPI packages
   - Useful for development or private plugins

3. **Local Development**
   ```bash
   uv pip install -e /path/to/plugin  # Editable install
   ```
   - For plugin development
   - Changes reflected immediately without reinstall

**Plugin package structure:**
```
pynchy-plugin-voice/
├── pyproject.toml          # Entry point registration
├── src/
│   └── pynchy_plugin_voice/
│       ├── __init__.py
│       ├── plugin.py       # Plugin class with @hookimpl methods
│       ├── mcp_server.py   # MCP server implementation (if applicable)
│       └── container/      # Container-side code (if applicable)
│           └── core.py
```

**Entry point registration in plugin's `pyproject.toml`:**
```toml
[project.entry-points."pynchy"]
voice = "pynchy_plugin_voice.plugin:VoicePlugin"
```

### Plugin Discovery Flow

```
1. App starts → calls get_plugin_manager()
2. Plugin manager created → pm = pluggy.PluginManager("pynchy")
3. Add hook specs → pm.add_hookspecs(PynchySpec)
4. Register built-ins → pm.register(ClaudeAgentCorePlugin())
5. Discover third-party → pm.load_setuptools_entrypoints("pynchy")
   ├─ Scans installed packages for pynchy entry points
   ├─ Loads each entry point (imports the plugin class)
   ├─ Instantiates the class
   └─ Registers with plugin manager
6. Ready to use → pm.hook.pynchy_agent_core_info()
```

### No Separate Plugin Storage

Unlike some systems, pynchy doesn't have a separate plugin directory. Plugins are regular Python packages installed via pip/uv into the virtual environment. This means:

✅ **Standard Python tooling** - Use `uv`, `pip`, `poetry`, etc.
✅ **Version management** - `uv.lock` tracks plugin versions
✅ **Dependency resolution** - pip handles plugin dependencies
✅ **Virtual environments** - Isolated per installation
✅ **Uninstall is simple** - `uv pip uninstall pynchy-plugin-voice`

### Container Access to Plugins

For plugins that provide container-side code (agent cores, MCP servers), the plugin's `host_source_path()` method returns the path to mount:

```python
@hookimpl
def pynchy_agent_core_info(self):
    return {
        "name": "openai",
        "module": "pynchy_plugin_openai.core",
        "class_name": "OpenAIAgentCore",
        "host_source_path": str(Path(__file__).parent),  # Mount plugin dir
        ...
    }
```

The host mounts this path into the container at `/workspace/plugins/{name}/`, making the module importable from the container.

## Benefits

### 1. Type Safety

Pluggy validates hook signatures:

```python
@hookimpl
def pynchy_agent_core_info(self):
    return "invalid"  # ❌ Pluggy catches type mismatch at registration
```

### 2. Plugin Blocking/Enabling

```python
# Disable a specific plugin
pm.set_blocked("openai-plugin")

# List registered plugins
for plugin in pm.get_plugins():
    print(plugin)
```

### 3. Better Error Messages

Current:
```
Failed to load plugin: name=unknown error='NoneType' object has no attribute 'core_module'
```

With pluggy:
```
ValidationError: pynchy_agent_core_info() missing required return key 'module'
  at my_package.plugin:MyPlugin line 42
```

### 4. Hook Execution Order

```python
@hookimpl(trylast=True)  # Run after other plugins
@hookimpl(tryfirst=True) # Run before other plugins
```

### 5. No Manual Category Management

Instead of:
```python
if "agent_core" in plugin.categories:
    registry.agent_cores.append(plugin)
```

Just:
```python
cores = pm.hook.pynchy_agent_core_info()
```

## Compatibility with Current Design

### Container Input

No changes needed - still pass module/class names to container:

```python
pm = get_plugin_manager()
cores = pm.hook.pynchy_agent_core_info()
claude = next(c for c in cores if c["name"] == "claude")

input_data = ContainerInput(
    agent_core_module=claude["module"],
    agent_core_class=claude["class_name"],
    ...
)
```

### Plugin Security Model

Unchanged - all plugin code still runs on host during discovery:

```python
pm.register(MyPlugin())  # Plugin.__init__() runs on host
pm.hook.pynchy_agent_core_info()  # Hook methods run on host
```

### Channel Plugins

Channel `start()` still runs persistently:

```python
@hookimpl(firstresult=True)
def pynchy_create_channel(self, context):
    return WhatsAppChannel(context)

channel = pm.hook.pynchy_create_channel(context=ctx)
await channel.start()  # Persistent host process
```

## Testing Strategy

### Unit Tests

```python
def test_hookspec_signature():
    """Test hookspecs are defined correctly."""
    pm = pluggy.PluginManager("pynchy")
    pm.add_hookspecs(PynchySpec)
    assert pm.hook.pynchy_agent_core_info

def test_plugin_registration():
    """Test plugin registers successfully."""
    pm = get_plugin_manager()
    assert "ClaudeAgentCorePlugin" in [p.__class__.__name__ for p in pm.get_plugins()]

def test_hook_calling():
    """Test hook returns expected data."""
    pm = get_plugin_manager()
    cores = pm.hook.pynchy_agent_core_info()
    assert len(cores) >= 1  # At least Claude
    assert cores[0]["name"] == "claude"
```

### Integration Tests

```python
def test_container_still_works():
    """Test container can still create agent core."""
    pm = get_plugin_manager()
    cores = pm.hook.pynchy_agent_core_info()
    claude = next(c for c in cores if c["name"] == "claude")

    # Verify ContainerInput still works
    input_data = ContainerInput(
        agent_core_module=claude["module"],
        agent_core_class=claude["class_name"],
        ...
    )
    # Run container...
```


## Plugin Author Guide (Future Third-Party Plugins)

### Single-Category Plugin Example

A plugin that only provides one category (agent core):

```python
import pluggy
from pathlib import Path

hookimpl = pluggy.HookimplMarker("pynchy")

class OpenAIPlugin:
    """OpenAI agent core plugin."""

    @hookimpl
    def pynchy_agent_core_info(self):
        return {
            "name": "openai",
            "module": "pynchy_plugin_openai.core",
            "class_name": "OpenAIAgentCore",
            "packages": ["openai>=1.0.0"],
            "host_source_path": str(Path(__file__).parent),
        }
```

### Multi-Category Plugin Example

A plugin can implement multiple hooks to provide multiple capabilities:

```python
import pluggy
from pathlib import Path

hookimpl = pluggy.HookimplMarker("pynchy")

class VoicePlugin:
    """Voice transcription and TTS plugin.

    Provides:
    - MCP server for transcription/TTS tools
    - Skills for voice interaction patterns
    """

    @hookimpl
    def pynchy_mcp_server_spec(self):
        """Provide MCP server for voice tools."""
        return {
            "name": "voice",
            "command": "python",
            "args": ["-m", "pynchy_plugin_voice.mcp_server"],
            "env": {},
            "host_source": str(Path(__file__).parent),
        }

    @hookimpl
    def pynchy_skill_paths(self):
        """Provide voice interaction skills."""
        skill_dir = Path(__file__).parent / "skills"
        return [str(skill_dir)]
```

**No categories attribute needed!** Pluggy determines what a plugin provides by which hooks it implements.

### Plugin Package Registration

Register via `pyproject.toml`:
```toml
[project.entry-points."pynchy"]
openai = "pynchy_plugin_openai.plugin:OpenAIPlugin"
voice = "pynchy_plugin_voice.plugin:VoicePlugin"
```

## Risks

### Risk: Breaking Future Third-Party Plugins

**Impact:** None - no third-party plugins exist yet

This is the ideal time to switch to pluggy. Once we ship this, any future third-party plugins will start with pluggy from day one.

### Risk: Pluggy Dependency

**Impact:** Very low

- **Mature:** 10+ years old, powers pytest, tox, devpi
- **Lightweight:** ~500KB, no dependencies
- **Stable API:** Rarely breaks compatibility
- **Already familiar:** Most Python devs know pluggy from pytest

## Implementation Checklist

- [ ] Add `pluggy` to dependencies (`uv add pluggy`)
- [ ] Create `src/pynchy/plugin/hookspecs.py` with hook specifications
- [ ] Update `src/pynchy/plugin/__init__.py` to export `get_plugin_manager()`
- [ ] Convert `builtin_agent_claude.py` to use `@hookimpl`
- [ ] Update `app.py` to use `pm.hook.pynchy_agent_core_info()`
- [ ] Update `task_scheduler.py` to use `pm.hook.pynchy_agent_core_info()`
- [ ] Update `container_runner.py` MCP/skill mounting to use hooks
- [ ] Delete `src/pynchy/plugin/base.py` (PluginBase, PluginRegistry)
- [ ] Delete ABC plugin classes (AgentCorePlugin, McpPlugin, SkillPlugin, ChannelPlugin)
- [ ] Update tests to use hookspecs instead of ABCs
- [ ] Update CLAUDE.md with pluggy pattern
- [ ] Verify all tests pass

## Timeline Estimate

**Total:** 1-2 days (immediate cutover, no parallel phase)

## Design Decisions

1. **Hook naming:** Use `pynchy_*` prefix for all hooks
   - Examples: `pynchy_agent_core_info`, `pynchy_mcp_server_spec`, `pynchy_skill_paths`
   - Rationale: Consistent namespace, avoids conflicts with other pluggy apps

2. **Return types:** Return dicts, not dataclasses
   - More flexible for third-party plugins
   - Matches current pattern
   - Pluggy doesn't enforce structure anyway (just signatures)

3. **Plugin registration:** Built-ins in `get_plugin_manager()`, third-party via entry points
   - Simplifies initialization
   - Entry points still work via `load_setuptools_entrypoints("pynchy")`

4. **Entry point group:** Keep `"pynchy"` as the group name
   - Already in use for current plugins
   - Pluggy will discover them automatically

## FAQ

### How do I install a plugin from GitHub?

```bash
uv pip install git+https://github.com/user/pynchy-plugin-name.git
```

The plugin is installed into your virtual environment like any Python package. Pynchy auto-discovers it on next startup via entry points.

### Where are plugins stored?

In your virtual environment's `site-packages/`, just like any pip package. There's no separate plugin directory.

To see installed plugins:
```bash
uv pip list | grep pynchy-plugin
```

### Can a plugin provide multiple capabilities?

Yes! A single plugin can implement multiple hooks. For example, a "voice" plugin might implement:
- `pynchy_mcp_server_spec()` for voice transcription tools
- `pynchy_skill_paths()` for voice interaction patterns

### How do I uninstall a plugin?

```bash
uv pip uninstall pynchy-plugin-name
```

### How do I temporarily disable a plugin without uninstalling?

Use pluggy's blocking API:
```python
pm = get_plugin_manager()
pm.set_blocked("plugin-name")
```

(This feature would need to be exposed via CLI or config in the future)

### Do plugins need to declare categories?

No! With pluggy, you just implement the hooks you need. The old `categories = ["agent_core"]` attribute is gone.

### Can I install plugins from private repositories?

Yes:
```bash
uv pip install git+https://github.com/user/private-plugin.git
```

For authentication, use GitHub tokens:
```bash
uv pip install git+https://${GITHUB_TOKEN}@github.com/user/private-plugin.git
```

### How are plugin versions managed?

Via `uv.lock` or your lockfile, just like regular dependencies. To pin a specific version:
```bash
uv pip install pynchy-plugin-voice==1.2.3
```

## References

- [Pluggy Documentation](https://pluggy.readthedocs.io/)
- [Pytest's Plugin System](https://docs.pytest.org/en/stable/how-to/writing_plugins.html)
- [Current Plugin System](../../src/pynchy/plugin/)
