# Hook Reference

Pynchy plugins implement hooks defined in `src/pynchy/plugins/hookspecs.py`. Each hook corresponds to a plugin category, and a plugin can implement any combination of hooks.

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

## pynchy_service_handler

Provide host-side handlers for service tools. This hook provides handler functions that run on the **host process** and are dispatched to via IPC when container agents invoke service tools.

**Calling strategy:** All results collected; tool mappings are merged (last-write-wins on conflict).

```python
@hookimpl
def pynchy_service_handler(self) -> dict[str, Any]:
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

**Calling strategy:** All results collected and flattened. Skills are filtered per-workspace based on the `skills` config field before being copied into the session directory.

```python
@hookimpl
def pynchy_skill_paths(self) -> list[str]:
    return [str(Path(__file__).parent / "skills" / "code-review")]
```

**Return value:** List of absolute paths to skill directories. Each directory should contain a `SKILL.md` file following the Claude Agent SDK skill format.

**Skill directory structure:**

```
skills/
└── code-review/
    ├── SKILL.md          # Required: skill definition
    └── examples.md       # Optional: supporting files
```

**SKILL.md frontmatter:**

Skills declare metadata via YAML frontmatter at the top of `SKILL.md`:

```yaml
---
name: code-review
description: Review code for bugs and style issues.
tier: community
---
```

| Field | Required | Description |
|-------|----------|-------------|
| `name` | No | Skill identifier (defaults to directory name) |
| `description` | Yes | What the skill does (used by the agent to decide when to invoke it) |
| `tier` | No | `core`, `community`, or `dev` (defaults to `community`) |
| `allowed-tools` | No | Tool permissions (e.g., `Bash(my-tool:*)`) |

**Skill tiers:**

| Tier | Purpose | Filtering behavior |
|------|---------|-------------------|
| `core` | Essential skills useful in all workspaces | Always included when any filtering is active |
| `community` | General-purpose skills (default) | Included only when explicitly listed |
| `dev` | Skills for developing pynchy itself | Included only when explicitly listed |

Workspaces opt into skills via the `skills` config field:

```toml
[workspaces.my-workspace]
skills = ["core", "dev"]           # tier names and/or individual skill names
```

When `skills` is not set, only core-tier skills are included (safe default). When set, entries are unioned — `["core", "my-skill"]` means all core-tier skills plus `my-skill` specifically. Core is always implicit when any filtering is active. Use `["all"]` to include every available skill.

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

## pynchy_memory

Provide a persistent memory backend (save, recall, forget, list). Agents use memory tools to store facts across sessions.

**Calling strategy:** All results collected; first non-`None` result wins.

```python
@hookimpl
def pynchy_memory(self) -> Any | None:
    return MyMemoryBackend()
```

**Memory backend contract:**

| Attribute / Method | Type | Description |
|--------------------|------|-------------|
| `name` | `str` | Backend identifier (e.g., `"sqlite"`, `"jsonl"`) |
| `save(group_folder, key, content, category, metadata)` | `async (...) -> dict` | Store or update a memory entry |
| `recall(group_folder, query, category, limit)` | `async (...) -> list[dict]` | Search memories by keyword (BM25-ranked) |
| `forget(group_folder, key)` | `async (...) -> dict` | Delete a memory entry by key |
| `list_keys(group_folder, category)` | `async (...) -> list[dict]` | List all memory keys, optionally filtered by category |
| `init()` | `async () -> None` | Create tables or other setup |
| `close()` | `async () -> None` | Flush and teardown |

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `group_folder` | `str` | Workspace folder name — memories are isolated per group |
| `key` | `str` | Unique identifier for the memory entry |
| `content` | `str` | The information to store |
| `category` | `str` | `"core"` (permanent), `"daily"` (session), or `"conversation"` (auto-archived) |
| `metadata` | `dict \| None` | Optional metadata attached to the entry |
| `query` | `str` | Search keywords for recall |
| `limit` | `int` | Maximum results to return |

**Built-in:** The SQLite memory plugin (`src/pynchy/memory/plugins/sqlite_memory/`) stores memories in the main database with FTS5 full-text search.

## pynchy_mcp_server_spec

Provide an MCP server specification. Plugin-provided specs are merged with user-defined servers in `config.toml`. Config.toml definitions override plugin defaults when both use the same server name.

**Calling strategy:** All results collected and merged. A plugin can return a single dict or a list of dicts (for plugins providing multiple servers).

```python
@hookimpl
def pynchy_mcp_server_spec(self) -> list[dict[str, Any]]:
    return [
        {
            "name": "gdrive",
            "type": "docker",
            "image": "pynchy-mcp-gdrive:latest",
            "dockerfile": "src/pynchy/agent/mcp/gdrive.Dockerfile",
            "port": 3100,
            "transport": "streamable_http",
            "env": {"GDRIVE_OAUTH_PATH": "/home/chrome/gcp-oauth.keys.json"},
        },
    ]
```

**Return keys:**

| Key | Type | Description |
|-----|------|-------------|
| `name` | `str` | Server identifier (used as the `mcp_servers` key) |
| `type` | `str` | `"docker"`, `"script"`, or `"url"` (default `"script"`) |
| `image` | `str` | Docker image name (required for `type="docker"`) |
| `dockerfile` | `str \| None` | Relative path to a local Dockerfile — auto-built by the MCP manager |
| `command` | `str \| None` | Executable to run (for `type="script"`) |
| `args` | `list[str] \| None` | Command arguments |
| `port` | `int` | HTTP port the server listens on |
| `extra_ports` | `list[int] \| None` | Additional ports to publish (e.g., `[8888]` for JupyterLab) |
| `transport` | `str` | MCP transport type (default `"streamable_http"`) |
| `idle_timeout` | `int` | Seconds before auto-stop (default `600`) |
| `env` | `dict[str, str] \| None` | Static env vars passed to the server |
| `env_forward` | `list[str] \| dict[str, str] \| None` | Host env vars to forward |
| `volumes` | `list[str] \| None` | Volume mounts as `"host_path:container_path"` strings; supports `{key}` placeholders expanded from instance kwargs |

**Instance expansion:** Users don't configure the base spec — they declare *instances* in `config.toml` that reference the plugin-provided template:

```toml
[mcp_servers.gdrive.anyscale]
chrome_profile = "anyscale"
```

The MCP manager merges this with the plugin-provided base spec, auto-assigns ports, and mounts chrome profile directories. See [MCP Servers](../usage/mcp.md) for user-facing config details.

## pynchy_workspace_spec

Provide a managed workspace definition (for example a periodic agent).

**Calling strategy:** All results collected and merged with user `config.toml` workspaces.

```python
@hookimpl
def pynchy_workspace_spec(self) -> dict[str, Any]:
    return {
        "folder": "code-improver",
        "config": {
            "pynchy_repo_access": True,
            "schedule": "0 4 * * *",
            "prompt": "Run scheduled code improvements",
            "context_mode": "isolated",
        },
    }
```

**Return keys:**

| Key | Type | Description |
|-----|------|-------------|
| `folder` | `str` | Workspace folder name |
| `config` | `dict[str, Any]` | `WorkspaceConfig`-compatible fields |

Agent instructions are now delivered via [directives](../usage/directives.md) rather than seeded CLAUDE.md files. The `claude_md` field is ignored.

## Multi-Category Plugins

A single plugin can implement multiple hooks:

```python
class CalendarPlugin:
    """Provides calendar service handlers AND calendar skills."""

    @hookimpl
    def pynchy_service_handler(self) -> dict:
        return {
            "tools": {
                "list_calendar": _handle_list_calendar,
                "create_event": _handle_create_event,
            },
        }

    @hookimpl
    def pynchy_skill_paths(self) -> list[str]:
        return [str(Path(__file__).parent / "skills" / "calendar")]
```

No categories attribute needed — pluggy determines capabilities from which hooks the class implements.

## Hook Execution Order

Pluggy supports ordering hints:

```python
@hookimpl(trylast=True)   # Run after other plugins
@hookimpl(tryfirst=True)  # Run before other plugins
```

Most plugins don't need this. Use it when one plugin needs to see or modify another plugin's results.
