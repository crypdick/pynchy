# Hook Reference

Pynchy plugins implement hooks defined in `src/pynchy/plugin/hookspecs.py`. Each hook corresponds to a plugin category, and a plugin can implement any combination of hooks.

All hooks use pluggy's `@hookimpl` decorator:

```python
import pluggy

hookimpl = pluggy.HookimplMarker("pynchy")
```

## pynchy_agent_core_info

Provide an alternative LLM agent framework.

**Calling strategy:** All results collected (multiple cores can coexist; selected via `PYNCHY_AGENT_CORE` env var).

```python
@hookimpl
def pynchy_agent_core_info(self) -> dict[str, str | list[str] | None]:
    return {
        "name": "ollama",                              # Core identifier
        "module": "pynchy_plugin_ollama.core",         # Python module path
        "class_name": "OllamaAgentCore",               # Class to instantiate
        "packages": ["ollama>=0.1.0"],                 # pip packages for container
        "host_source_path": str(Path(__file__).parent), # Source to mount, or None
    }
```

**Return keys:**

| Key | Type | Description |
|-----|------|-------------|
| `name` | `str` | Unique core identifier |
| `module` | `str` | Fully qualified module path (importable inside container) |
| `class_name` | `str` | Class name to instantiate |
| `packages` | `list[str]` | pip packages to install in container |
| `host_source_path` | `str \| None` | Host path to mount into container at `/workspace/plugins/{name}/` |

## pynchy_mcp_server_spec

Provide tools to the agent via an MCP server that runs inside the container.

**Calling strategy:** All results collected (agents can use tools from all MCP servers).

```python
@hookimpl
def pynchy_mcp_server_spec(self) -> dict[str, Any]:
    return {
        "name": "weather",                             # Server identifier
        "command": "python",                           # Command to run in container
        "args": ["-m", "pynchy_plugin_weather.server"],# Command arguments
        "env": {"WEATHER_API_KEY": "..."},             # Environment variables
        "host_source": str(Path(__file__).parent),     # Source to mount, or None
    }
```

**Return keys:**

| Key | Type | Description |
|-----|------|-------------|
| `name` | `str` | Unique server identifier |
| `command` | `str` | Command to execute (e.g., `"python"`, `"node"`) |
| `args` | `list[str]` | Command arguments |
| `env` | `dict[str, str]` | Extra environment variables |
| `host_source` | `str \| None` | Host path to mount (sets `PYTHONPATH` automatically) |

**Container behavior:** Plugin source is mounted at `/workspace/plugins/{name}/` with `PYTHONPATH` set so the module is importable.

## pynchy_mcp_server_handler

Provide host-side handlers for service tools. Unlike `pynchy_mcp_server_spec` (which runs MCP servers inside the container), this hook provides handler functions that run on the **host process** and are dispatched to via IPC when container agents invoke service tools.

**Calling strategy:** All results collected; tool mappings are merged (last-write-wins on conflict).

```python
@hookimpl
def pynchy_mcp_server_handler(self) -> dict[str, Any]:
    return {
        "tools": {
            "list_calendar": _handle_list_calendar,
            "create_event": _handle_create_event,
            "delete_event": _handle_delete_event,
        },
    }
```

**Return keys:**

| Key | Type | Description |
|-----|------|-------------|
| `tools` | `dict[str, Callable]` | Mapping of tool_name to async handler function |

**Handler function signature:**

```python
async def handler(data: dict) -> dict:
    """Process a service tool request.

    Args:
        data: The full IPC request dict (includes type, request_id, and tool-specific fields)

    Returns:
        Dict with either {"result": ...} on success or {"error": "..."} on failure
    """
```

**Request flow:** Container MCP tool → IPC request → host policy check → plugin handler → IPC response

!!! warning
    Host-side handlers run **in the host process** with full access to host resources. Policy middleware (risk tiers, rate limits, human-approval) is enforced by the service handler before dispatching to the plugin.

## pynchy_skill_paths

Provide agent skills (markdown instruction files) that get mounted into the container.

**Calling strategy:** All results collected and flattened.

```python
@hookimpl
def pynchy_skill_paths(self) -> list[str]:
    return [str(Path(__file__).parent / "skills" / "code-review")]
```

**Return value:** List of absolute paths to skill directories. Each directory should contain a `SKILL.md` file following the Claude Agent SDK skill format (see `container/skills/agent-browser/SKILL.md` for an example).

**Skill directory structure:**

```
skills/
└── code-review/
    ├── SKILL.md          # Required: skill definition
    └── examples.md       # Optional: supporting files
```

## pynchy_create_channel

Provide a communication channel (Telegram, Slack, Discord, etc.).

**Calling strategy:** All non-`None` channels are collected; host config chooses the default channel.

```python
@hookimpl
def pynchy_create_channel(self, context: Any) -> Any | None:
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        return None  # This plugin doesn't apply
    return TelegramChannel(
        bot_token=bot_token,
        on_message=context.on_message_callback,
        on_chat_metadata=context.on_chat_metadata_callback,
        registered_groups=context.registered_groups,
    )
```

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `context` | `ChannelPluginContext` | Frozen dataclass with callbacks (see below) |

**`ChannelPluginContext` fields:**

| Field | Type | Description |
|-------|------|-------------|
| `on_message_callback` | `Callable[[str, NewMessage], None]` | Ingest a message for a JID |
| `on_chat_metadata_callback` | `Callable[[str, str, str \| None], None]` | Update chat metadata (JID, timestamp, display name) |
| `registered_groups` | `Callable[[], dict[str, RegisteredGroup]]` | Get all registered workspaces |
| `send_message` | `Callable[[str, str], Any]` | Send outbound text to a JID |
| `on_reaction_callback` | `Callable[..., None] \| None` | Optional reaction handler |

**Return value:** A `Channel` instance implementing the channel protocol, or `None` to pass.

**Channel protocol:**

```python
class Channel(Protocol):
    name: str

    async def connect(self) -> None: ...
    async def send_message(self, jid: str, text: str) -> None: ...
    def is_connected(self) -> bool: ...
    def owns_jid(self, jid: str) -> bool: ...
    async def disconnect(self) -> None: ...
```

Optional attributes (check with `hasattr`/`getattr`): `prefix_assistant_name` (bool, default `True`), `set_typing`, `create_group`.

!!! warning
    Channel plugins run **persistently on the host** with full filesystem and network access. This is the highest-risk plugin category. See [Security Model](../architecture/security.md).

## pynchy_container_runtime

Provide a host container runtime implementation (for example Apple Container).

**Calling strategy:** All results collected; runtime selection picks by config override (`[container].runtime`) or platform-aware auto-detection.

```python
@hookimpl
def pynchy_container_runtime(self) -> Any | None:
    return AppleContainerRuntime()
```

**Runtime object contract:**

| Attribute / Method | Type | Description |
|--------------------|------|-------------|
| `name` | `str` | Runtime identifier (for config override), e.g. `"apple"` |
| `cli` | `str` | CLI command used for container ops, e.g. `"container"` |
| `is_available()` | `() -> bool` | Returns whether runtime can be used on this host |
| `ensure_running()` | `() -> None` | Ensures daemon/service is running (or raises) |
| `list_running_containers(prefix)` | `(str) -> list[str]` | Lists active container names for orphan cleanup |

## pynchy_tunnel

Provide a tunnel provider for remote connectivity detection (Tailscale, Cloudflare Tunnel, WireGuard, etc.).

**Calling strategy:** All results collected; pynchy checks each provider at startup and warns if none are connected.

```python
@hookimpl
def pynchy_tunnel(self) -> Any | None:
    return MyTunnelProvider()
```

**Tunnel provider contract:**

| Attribute / Method | Type | Description |
|--------------------|------|-------------|
| `name` | `str` | Tunnel identifier (e.g., `"tailscale"`, `"cloudflare"`) |
| `is_available()` | `() -> bool` | Returns whether the tunnel software is installed on this host |
| `is_connected()` | `() -> bool` | Returns whether the tunnel is currently connected |
| `status_summary()` | `() -> str` | Human-readable status string for logging |

**Built-in:** Tailscale is provided as a built-in plugin (`src/pynchy/tunnels/plugins/tailscale.py`). It shells out to `tailscale status --json` and checks `BackendState`.

## pynchy_observer

Provide an event observer that subscribes to the EventBus and persists or processes events (SQLite, OpenTelemetry, log files, etc.).

**Calling strategy:** All results collected; each observer's `subscribe()` is called with the event bus during startup.

```python
@hookimpl
def pynchy_observer(self) -> Any | None:
    return SqliteEventObserver()
```

**Observer object contract:**

| Attribute / Method | Type | Description |
|--------------------|------|-------------|
| `name` | `str` | Observer identifier (e.g., `"sqlite"`, `"otel"`) |
| `subscribe(event_bus)` | `(EventBus) -> None` | Attach listeners to the event bus |
| `close()` | `async () -> None` | Async teardown — unsubscribe and flush |

**Event types available:**

| Event | Fields | Description |
|-------|--------|-------------|
| `MessageEvent` | `chat_jid`, `sender_name`, `content`, `timestamp`, `is_bot` | New message stored |
| `AgentActivityEvent` | `chat_jid`, `active` | Agent started/stopped processing |
| `AgentTraceEvent` | `chat_jid`, `trace_type`, `data` | Ephemeral trace (thinking, tool use, text) |
| `ChatClearedEvent` | `chat_jid` | Chat history cleared |

**Built-in:** The SQLite observer (`src/pynchy/observers/plugins/sqlite_observer/`) stores all events to a dedicated `events` table in the main database.

!!! warning
    Observer plugins run **in the host process** and subscribe to all events. A misbehaving observer can slow down event dispatch. Keep handlers lightweight and non-blocking.

## pynchy_workspace_spec

Provide a managed workspace definition (for example a periodic agent).

**Calling strategy:** All results collected and merged with user `config.toml` workspaces.

```python
@hookimpl
def pynchy_workspace_spec(self) -> dict[str, Any]:
    return {
        "folder": "code-improver",
        "config": {
            "project_access": True,
            "schedule": "0 4 * * *",
            "prompt": "Run scheduled code improvements",
            "context_mode": "isolated",
        },
        "claude_md": "# Code Improver\\n\\nAgent instructions...",
    }
```

**Return keys:**

| Key | Type | Description |
|-----|------|-------------|
| `folder` | `str` | Workspace folder name |
| `config` | `dict[str, Any]` | `WorkspaceConfig`-compatible fields |
| `claude_md` | `str \| None` | Optional `groups/{folder}/CLAUDE.md` content to seed when missing |

## Multi-Category Plugins

A single plugin can implement multiple hooks:

```python
class VoicePlugin:
    """Provides voice tools AND voice interaction skills."""

    @hookimpl
    def pynchy_mcp_server_spec(self) -> dict:
        return {
            "name": "voice",
            "command": "python",
            "args": ["-m", "pynchy_plugin_voice.server"],
            "env": {},
            "host_source": str(Path(__file__).parent),
        }

    @hookimpl
    def pynchy_skill_paths(self) -> list[str]:
        return [str(Path(__file__).parent / "skills" / "voice")]
```

No categories attribute needed — pluggy determines capabilities from which hooks the class implements.

## Hook Execution Order

Pluggy supports ordering hints:

```python
@hookimpl(trylast=True)   # Run after other plugins
@hookimpl(tryfirst=True)  # Run before other plugins
```

Most plugins don't need this. Use it when one plugin needs to see or modify another plugin's results.
