# Plugin System for Pynchy

> **Status: Future project** — This plan will be implemented after the Python port is complete and the security hardening plan is in place. It is not actively being worked on.

## Context

Pynchy uses Claude Code skills to add integrations (Telegram, Gmail, X/Twitter). This couples personal config to the main repo — leaking info and bloating pushes. We need a plugin system where external repos (e.g., `crypdick/pynchy-plugin-foo`) provide extensions that Pynchy discovers automatically with no main repo changes.

## Design Overview

**Discovery**: Python entry points via `importlib.metadata`. One group: `pynchy.plugins`.

```bash
uv pip install -e ../pynchy-plugin-voice       # local dev
uv pip install git+https://github.com/crypdick/pynchy-plugin-voice  # GitHub
```

Install = active. Uninstall = gone. No config files.

**Four plugin types** as ABC base classes. Plugins extend the base class(es) they need. Composite plugins use multiple inheritance.

## Plugin Types

### 1. ChannelPlugin — new communication platforms

```python
class ChannelPlugin(ABC):
    name: str

    @abstractmethod
    def create_channel(self, ctx: PluginContext) -> Channel:
        """Return a Channel instance (connects on startup)."""
        ...
```

Integrates with: `app.py:run()` — channel added to `self.channels`, connected alongside WhatsApp.

Existing code to reuse: `Channel` protocol (`types.py:115`), `_find_channel()` (`app.py:454`).

### 2. McpPlugin — agent tools

```python
class McpPlugin(ABC):
    name: str

    @abstractmethod
    def mcp_server_spec(self) -> McpServerSpec:
        """Return MCP server config for the container agent."""
        ...
```

```python
@dataclass
class McpServerSpec:
    name: str                       # MCP server name (e.g., "voice")
    command: str                    # Command inside container (e.g., "python")
    args: list[str]                 # e.g., ["-m", "pynchy_plugin_voice.mcp"]
    env: dict[str, str]             # Extra env vars passed to MCP process
    host_source: Path               # Plugin package dir to mount into container
```

Integrates with:
- `container_runner.py:_build_volume_mounts()` — mounts `host_source` → `/workspace/plugins/{name}/`
- `container_runner.py:_input_to_dict()` — passes MCP config in `ContainerInput`
- `agent_runner/main.py:360` — merges into `ClaudeAgentOptions.mcp_servers`

### 3. SkillPlugin — agent instructions/capabilities

```python
class SkillPlugin(ABC):
    name: str

    @abstractmethod
    def skill_paths(self) -> list[Path]:
        """Return paths to skill directories (each containing SKILL.md etc.)."""
        ...
```

Integrates with: `container_runner.py:_sync_skills()` — skills copied to session dir alongside built-in skills.

### 4. HookPlugin — agent lifecycle events

```python
class HookPlugin(ABC):
    name: str

    @abstractmethod
    def agent_hooks(self) -> dict[str, list[Callable]]:
        """Return {event_name: [hook_fn]} for agent runner hooks.

        Events: PreCompact, Stop, etc. (Claude Agent SDK hook events)
        """
        ...
```

Integrates with:
- Passed via `ContainerInput` is tricky (hooks are callables, not serializable)
- Better approach: plugin provides a hook module path, agent runner imports and registers it
- Or: plugin provides hook config that gets written to `.claude/hooks.json` in the session dir

**Note**: Hook integration needs more thought — see Implementation Step 6.

## Composite Plugins

A Telegram plugin needs both a channel and MCP tools:

```python
class TelegramPlugin(ChannelPlugin, McpPlugin):
    name = "telegram"

    def create_channel(self, ctx):
        return TelegramChannel(bot_token=os.environ["TELEGRAM_BOT_TOKEN"], ...)

    def mcp_server_spec(self):
        return McpServerSpec(
            name="telegram",
            command="python",
            args=["-m", "pynchy_plugin_telegram.mcp"],
            env={},
            host_source=Path(__file__).parent,
        )
```

## Discovery & Dispatch

```python
def discover_plugins() -> PluginRegistry:
    registry = PluginRegistry()
    for ep in entry_points(group="pynchy.plugins"):
        try:
            plugin = ep.load()()  # instantiate
            if isinstance(plugin, ChannelPlugin):
                registry.channels.append(plugin)
            if isinstance(plugin, McpPlugin):
                registry.mcp_servers.append(plugin)
            if isinstance(plugin, SkillPlugin):
                registry.skills.append(plugin)
            if isinstance(plugin, HookPlugin):
                registry.hooks.append(plugin)
        except Exception as e:
            logger.warning("Failed to load plugin", name=ep.name, error=str(e))
    return registry
```

Note: uses `if` not `elif` — a composite plugin registers in multiple lists.

## Plugin Repo Structure

```
pynchy-plugin-voice/
├── pyproject.toml
├── src/
│   └── pynchy_plugin_voice/
│       ├── __init__.py     # exports VoicePlugin
│       ├── plugin.py       # extends McpPlugin
│       └── mcp.py          # MCP server (transcribe_voice tool)
```

**pyproject.toml:**
```toml
[project]
name = "pynchy-plugin-voice"
version = "0.1.0"
dependencies = ["pynchy"]

[project.entry-points."pynchy.plugins"]
voice = "pynchy_plugin_voice:VoicePlugin"
```

## Files to Create/Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/pynchy/plugin.py` | **Create** | Base classes (ChannelPlugin, McpPlugin, SkillPlugin, HookPlugin), McpServerSpec, PluginContext, PluginRegistry, discover_plugins() |
| `src/pynchy/types.py` | Modify | Add `plugin_mcp_servers` field to ContainerInput |
| `src/pynchy/app.py` | Modify | Call discover_plugins() at startup, register channels, pass plugin data to container runner |
| `src/pynchy/container_runner.py` | Modify | Accept plugin mounts/MCP configs, extend `_build_volume_mounts()` and `_sync_skills()`, pass MCP configs in input JSON |
| `container/agent_runner/src/agent_runner/main.py` | Modify | Read `plugin_mcp_servers` from input, merge into `ClaudeAgentOptions.mcp_servers` |
| `tests/test_plugin.py` | **Create** | Test discovery, dispatch, MCP config merging, mount building, skill syncing |

## Implementation Steps

### 1. Create `src/pynchy/plugin.py`
- Four ABC base classes: `ChannelPlugin`, `McpPlugin`, `SkillPlugin`, `HookPlugin`
- `McpServerSpec` dataclass
- `PluginContext` dataclass (send_message, registered_groups, config)
- `PluginRegistry` dataclass (channels, mcp_servers, skills, hooks lists)
- `discover_plugins()` function

### 2. Extend ContainerInput (`types.py`)
- Add `plugin_mcp_servers: dict[str, dict] | None = None` — serialized MCP configs

### 3. Wire channels + startup into `app.py`
- After `_load_state()`: `self.registry = discover_plugins()`
- Create PluginContext, call `plugin.create_channel(ctx)` for each ChannelPlugin
- Store registry for use by container runner

### 4. Wire MCP + skills into `container_runner.py`
- `_build_volume_mounts()` receives plugin registry, appends MCP plugin mounts (`host_source` → `/workspace/plugins/{name}/`)
- `_sync_skills()` receives plugin registry, copies SkillPlugin paths alongside built-in skills
- `_input_to_dict()` includes `plugin_mcp_servers` dict
- `run_container_agent()` accepts plugin registry parameter

### 5. Wire MCP into agent runner (`main.py`)
- Read `plugin_mcp_servers` from input JSON (lines 298-303)
- Merge each into `options.mcp_servers` dict (line 360)
- Each gets `PYTHONPATH=/workspace/plugins/{name}` in env so imports work

### 6. Hook integration (deferred complexity)
- Hooks are callables — can't serialize through JSON input
- **Approach**: HookPlugin provides a module path + function name. Agent runner imports it at startup.
- Plugin's hook module gets mounted into container. Agent runner does: `importlib.import_module("pynchy_plugin_foo.hooks").create_hooks()`
- Merges returned hooks into `ClaudeAgentOptions.hooks`
- This is the trickiest piece — implement after the other three types are working.

### 7. Tests
- Mock entry points, verify discovery and dispatch by type
- Verify MCP configs flow: plugin → ContainerInput → agent runner
- Verify skill paths get synced to session dir
- Verify plugin mounts in volume mount list
- Verify broken plugins are logged and skipped (not crash)

## Container Dependency Note

Plugin MCP servers run inside the container. They can use packages in the container image (`mcp`, `croniter`, standard lib). If a plugin needs extra packages (e.g., `openai`), add them to the container Dockerfile. The user controls the image.

## Verification

1. Create test plugin at `/tmp/pynchy-plugin-test/` with a no-op McpPlugin
2. `uv pip install -e /tmp/pynchy-plugin-test`
3. `uv run python -c "from pynchy.plugin import discover_plugins; r = discover_plugins(); print(r)"` — shows plugin in mcp_servers list
4. `uv run pytest tests/` — all tests pass
5. `uv pip uninstall pynchy-plugin-test` — disappears
