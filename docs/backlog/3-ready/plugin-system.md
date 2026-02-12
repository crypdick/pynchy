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

**Five plugin types** as ABC base classes. Plugins extend the base class(es) they need. Composite plugins use multiple inheritance.

## Plugin Types

### 1. RuntimePlugin — alternative container runtimes

Docker is the only built-in runtime. Other runtimes (Apple Container, Podman, etc.) are installed as plugins.

```python
@runtime_checkable
class ContainerRuntime(Protocol):
    """What every container runtime must provide."""
    name: str                      # e.g. "apple", "podman"
    cli: str                       # binary name, e.g. "container", "podman"

    def ensure_running(self) -> None:
        """Verify/start the runtime daemon."""
        ...

    def list_running_containers(self, prefix: str = "pynchy-") -> list[str]:
        """Return names of running containers matching prefix."""
        ...

    def build_run_args(self, mounts: list[VolumeMount], container_name: str, image: str) -> list[str]:
        """Return full CLI args for `run` (after the binary name).

        Default OCI-compatible implementation lives on the base class.
        Override only if the runtime's CLI diverges from Docker.
        """
        ...
```

```python
class RuntimePlugin(ABC):
    name: str

    @abstractmethod
    def create_runtime(self) -> ContainerRuntime:
        """Return a ContainerRuntime instance."""
        ...

    def platform_matches(self) -> bool:
        """Return True if this runtime applies to the current platform.

        Called during discovery — non-matching runtimes are skipped.
        Default: True (always available).
        """
        return True

    @property
    def priority(self) -> int:
        """Higher = preferred when multiple runtimes match.

        Docker built-in has priority 0. Plugins default to 10.
        Only matters when CONTAINER_RUNTIME env var is not set.
        """
        return 10
```

**Built-in**: `DockerRuntime` — the only runtime in the core package. It stays in `runtime.py`.

**Selection logic** (replaces current `detect_runtime()`):
1. `CONTAINER_RUNTIME` env var → exact match by name (built-in or plugin), error if not found
2. Discovered `RuntimePlugin`s where `platform_matches()` is True, sorted by `priority` (descending)
3. Fallback: `DockerRuntime` (always available, priority 0)

Integrates with:
- `runtime.py:detect_runtime()` — queries plugin registry for RuntimePlugins
- `container_runner.py:_build_container_args()` — delegates to `runtime.build_run_args()`
- `container_runner.py:run_container_agent()` — uses `runtime.cli` for subprocess exec
- `app.py:_ensure_container_system()` — calls `runtime.ensure_running()`
- `container/build.sh` — simplified to Docker-only; plugins can provide their own build scripts

**Removing Apple Container from core**: The entire `_ensure_apple()`, `_list_apple()` code moves out of `runtime.py` into `pynchy-plugin-apple-container`. `detect_runtime()` no longer checks `sys.platform == "darwin"` or `shutil.which("container")`. The fallback is always Docker.

### 2. ChannelPlugin — new communication platforms

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

### 3. McpPlugin — agent tools

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

### 4. SkillPlugin — agent instructions/capabilities

```python
class SkillPlugin(ABC):
    name: str

    @abstractmethod
    def skill_paths(self) -> list[Path]:
        """Return paths to skill directories (each containing SKILL.md etc.)."""
        ...
```

Integrates with: `container_runner.py:_sync_skills()` — skills copied to session dir alongside built-in skills.

### 5. HookPlugin — agent lifecycle events

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

**Note**: Hook integration needs more thought — see Implementation Step 8.

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
            if isinstance(plugin, RuntimePlugin):
                if plugin.platform_matches():
                    registry.runtimes.append(plugin)
                else:
                    logger.debug("Skipping runtime plugin (platform mismatch)", name=ep.name)
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

Runtime selection (called by `detect_runtime()`):
```python
def select_runtime(registry: PluginRegistry) -> ContainerRuntime:
    override = os.environ.get("CONTAINER_RUNTIME", "").lower()
    if override:
        # Check plugins first, then built-in
        for rp in registry.runtimes:
            rt = rp.create_runtime()
            if rt.name == override:
                return rt
        if override == "docker":
            return DockerRuntime()
        raise RuntimeError(f"Unknown CONTAINER_RUNTIME={override!r}. "
                           f"Available: docker, {', '.join(rp.name for rp in registry.runtimes)}")

    # Auto-detect: highest-priority plugin that matches this platform
    if registry.runtimes:
        best = sorted(registry.runtimes, key=lambda rp: rp.priority, reverse=True)[0]
        return best.create_runtime()

    # Fallback: always Docker
    return DockerRuntime()
```

## Plugin Repo Structure

### Example: MCP plugin (voice)

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

### Example: Runtime plugin (Apple Container)

This is the existing Apple Container code extracted from `runtime.py`:

```
pynchy-plugin-apple-container/
├── pyproject.toml
├── src/
│   └── pynchy_plugin_apple_container/
│       ├── __init__.py     # exports AppleContainerPlugin
│       ├── plugin.py       # extends RuntimePlugin
│       └── runtime.py      # AppleContainerRuntime (ensure_running, list_running_containers)
```

**pyproject.toml:**
```toml
[project]
name = "pynchy-plugin-apple-container"
version = "0.1.0"
dependencies = ["pynchy"]

[project.entry-points."pynchy.plugins"]
apple-container = "pynchy_plugin_apple_container:AppleContainerPlugin"
```

**plugin.py:**
```python
import sys
from pynchy.plugin import RuntimePlugin, ContainerRuntime
from pynchy_plugin_apple_container.runtime import AppleContainerRuntime

class AppleContainerPlugin(RuntimePlugin):
    name = "apple-container"

    def create_runtime(self) -> ContainerRuntime:
        return AppleContainerRuntime()

    def platform_matches(self) -> bool:
        return sys.platform == "darwin"

    @property
    def priority(self) -> int:
        return 20  # Prefer over Docker on macOS when installed
```

**runtime.py** — contains the `_ensure_apple()`, `_list_apple()` logic currently in `src/pynchy/runtime.py`, wrapped in an `AppleContainerRuntime` class implementing the `ContainerRuntime` protocol.

For macOS users: `uv pip install pynchy-plugin-apple-container` — that's it. The plugin auto-detects Darwin and takes priority over Docker.

## Files to Create/Modify

| File | Action | Purpose |
|------|--------|---------|
| `src/pynchy/plugin.py` | **Create** | Base classes (RuntimePlugin, ChannelPlugin, McpPlugin, SkillPlugin, HookPlugin), ContainerRuntime Protocol, McpServerSpec, PluginContext, PluginRegistry, discover_plugins(), select_runtime() |
| `src/pynchy/runtime.py` | **Rewrite** | Remove Apple Container code. Keep `DockerRuntime` (implements ContainerRuntime Protocol), `detect_runtime()` delegates to `select_runtime()`, lazy singleton stays |
| `src/pynchy/types.py` | Modify | Add `plugin_mcp_servers` field to ContainerInput |
| `src/pynchy/app.py` | Modify | Call discover_plugins() at startup, pass registry to runtime detection, register channels, pass plugin data to container runner |
| `src/pynchy/container_runner.py` | Modify | Accept plugin mounts/MCP configs, delegate arg building to `runtime.build_run_args()`, extend `_build_volume_mounts()` and `_sync_skills()`, pass MCP configs in input JSON |
| `container/build.sh` | Simplify | Remove Apple Container detection — Docker only. Plugin runtimes can ship their own build scripts |
| `container/agent_runner/src/agent_runner/main.py` | Modify | Read `plugin_mcp_servers` from input, merge into `ClaudeAgentOptions.mcp_servers` |
| `tests/test_runtime.py` | Modify | Remove Apple Container tests (they move to the plugin's test suite), add runtime plugin selection tests |
| `tests/test_plugin.py` | **Create** | Test discovery, dispatch, runtime selection, MCP config merging, mount building, skill syncing |

## Implementation Steps

### 1. Create `src/pynchy/plugin.py`
- `ContainerRuntime` Protocol (name, cli, ensure_running, list_running_containers, build_run_args)
- Five ABC base classes: `RuntimePlugin`, `ChannelPlugin`, `McpPlugin`, `SkillPlugin`, `HookPlugin`
- `McpServerSpec` dataclass
- `PluginContext` dataclass (send_message, registered_groups, config)
- `PluginRegistry` dataclass (runtimes, channels, mcp_servers, skills, hooks lists)
- `discover_plugins()` function
- `select_runtime()` function (env var → plugins → Docker fallback)

### 2. Rewrite `src/pynchy/runtime.py` — Docker only
- Delete `_ensure_apple()`, `_list_apple()`, and all `sys.platform == "darwin"` / `shutil.which("container")` logic
- `ContainerRuntime` dataclass → `DockerRuntime` class implementing the `ContainerRuntime` Protocol from `plugin.py`
- `DockerRuntime` contains `_ensure_docker()` and `_list_docker()` as methods, plus default `build_run_args()` (the OCI-compatible arg builder currently in `container_runner.py:_build_container_args()`)
- `detect_runtime()` calls `select_runtime(registry)` when a registry is available, otherwise returns `DockerRuntime()` directly
- Lazy singleton updated: `get_runtime()` accepts optional `PluginRegistry`, passes it through on first call

### 3. Simplify `container/build.sh`
- Remove Apple Container / `CONTAINER_RUNTIME` env var branching
- Hard-code `docker build`. Users with other runtimes use their own build commands or plugin-provided build scripts

### 4. Extend ContainerInput (`types.py`)
- Add `plugin_mcp_servers: dict[str, dict] | None = None` — serialized MCP configs

### 5. Wire runtime + channels + startup into `app.py`
- After `_load_state()`: `self.registry = discover_plugins()`
- Pass `self.registry` to `get_runtime()` so runtime selection considers plugins
- Create PluginContext, call `plugin.create_channel(ctx)` for each ChannelPlugin
- Store registry for use by container runner

### 6. Wire MCP + skills into `container_runner.py`
- Move `_build_container_args()` logic into `runtime.build_run_args()` — container_runner calls `get_runtime().build_run_args(mounts, name, image)` instead of building args itself
- `_build_volume_mounts()` receives plugin registry, appends MCP plugin mounts (`host_source` → `/workspace/plugins/{name}/`)
- `_sync_skills()` receives plugin registry, copies SkillPlugin paths alongside built-in skills
- `_input_to_dict()` includes `plugin_mcp_servers` dict
- `run_container_agent()` accepts plugin registry parameter

### 7. Wire MCP into agent runner (`main.py`)
- Read `plugin_mcp_servers` from input JSON (lines 298-303)
- Merge each into `options.mcp_servers` dict (line 360)
- Each gets `PYTHONPATH=/workspace/plugins/{name}` in env so imports work

### 8. Hook integration (deferred complexity)
- Hooks are callables — can't serialize through JSON input
- **Approach**: HookPlugin provides a module path + function name. Agent runner imports it at startup.
- Plugin's hook module gets mounted into container. Agent runner does: `importlib.import_module("pynchy_plugin_foo.hooks").create_hooks()`
- Merges returned hooks into `ClaudeAgentOptions.hooks`
- This is the trickiest piece — implement after the other four types are working.

### 9. Create `pynchy-plugin-apple-container` (separate repo)
- Extract `_ensure_apple()` and `_list_apple()` from current `runtime.py` into `AppleContainerRuntime`
- `AppleContainerPlugin`: `platform_matches()` checks `sys.platform == "darwin"`, priority 20
- Own test suite (the Apple Container tests from `tests/test_runtime.py` move here)
- Own `build.sh` that uses the `container` CLI

### 10. Tests
- Mock entry points, verify discovery and dispatch by type
- Runtime selection: env var override picks correct runtime
- Runtime selection: plugin with highest priority wins when no env var
- Runtime selection: `platform_matches()=False` plugins are skipped
- Runtime selection: falls back to Docker when no plugins match
- Verify MCP configs flow: plugin → ContainerInput → agent runner
- Verify skill paths get synced to session dir
- Verify plugin mounts in volume mount list
- Verify broken plugins are logged and skipped (not crash)

## Container Dependency Note

Plugin MCP servers run inside the container. They can use packages in the container image (`mcp`, `croniter`, standard lib). If a plugin needs extra packages (e.g., `openai`), add them to the container Dockerfile. The user controls the image.

## Runtime Protocol Details

The `ContainerRuntime` Protocol is the contract every runtime must satisfy. Runtimes that use OCI-compatible CLIs (Docker, Podman, Apple Container) can inherit a default `build_run_args()` from a base class. Runtimes with fundamentally different CLIs override it entirely.

**What the Protocol does NOT own**: Image building. Each runtime plugin can optionally ship a build script, but the core only provides `container/build.sh` targeting Docker. The `CONTAINER_IMAGE` config value is shared — all runtimes must be able to run the same OCI image.

**Env var escape hatch**: `CONTAINER_RUNTIME=docker` always works even if a plugin has higher priority. This is important for CI environments and debugging.

## Verification

### Runtime plugin
1. On macOS: `uv pip install pynchy-plugin-apple-container`
2. `uv run python -c "from pynchy.plugin import discover_plugins; r = discover_plugins(); print(r.runtimes)"` — shows AppleContainerPlugin
3. `uv run python -c "from pynchy.runtime import get_runtime; print(get_runtime().name)"` — prints `apple` on macOS
4. `CONTAINER_RUNTIME=docker uv run python -c "from pynchy.runtime import get_runtime; print(get_runtime().name)"` — prints `docker` (env var override)
5. `uv pip uninstall pynchy-plugin-apple-container` — falls back to Docker

### MCP/Channel/Skill plugin
6. Create test plugin at `/tmp/pynchy-plugin-test/` with a no-op McpPlugin
7. `uv pip install -e /tmp/pynchy-plugin-test`
8. `uv run python -c "from pynchy.plugin import discover_plugins; r = discover_plugins(); print(r)"` — shows plugin in mcp_servers list
9. `uv run pytest tests/` — all tests pass
10. `uv pip uninstall pynchy-plugin-test` — disappears
