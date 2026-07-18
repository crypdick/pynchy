# Hook Reference

Pynchy plugins implement hooks defined in `src/pynchy/plugins/hookspecs.py`. Each hook corresponds to a plugin category, and a plugin can implement any combination of hooks.

All hooks use pluggy's `@hookimpl` decorator:

```python
import pluggy

hookimpl = pluggy.HookimplMarker("pynchy")
```

## pynchy_agent_core_info

Provide an alternative LLM agent framework.

**Calling strategy:** All results collected. Multiple cores can coexist; select one with `[agent].default_core` (or `AGENT__DEFAULT_CORE`).

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

Provide host-side handlers for service tools. The handler functions run in the **host process** and are dispatched via IPC when container agents invoke service tools.

**Calling strategy:** All results are parsed into one immutable host-action
catalog. Duplicate capability IDs or tool names fail startup. Each descriptor
must name an effective `ActionSpec` whose agent-tool surface has the same tool
name.

```python
@hookimpl
def pynchy_service_handler(self) -> HostActionRegistration:
    capability = CapabilityDescriptor(
        id=CapabilityId("weather.forecast.read"),
        kind=CapabilityKind.HOST_ACTION,
        owner="weather-plugin",
        summary="Read the current weather forecast.",
        action_ids=(ActionId("weather.forecast.read"),),
        requirements=(
            CapabilityRequirement(
                kind=CapabilityRequirementKind.WORKSPACE_TOOL,
                name="weather_get_forecast",
                description="Enable the weather tool in this workspace.",
            ),
        ),
        setup_hint="Configure the weather provider, then enable the tool.",
        documentation="https://example.com/pynchy-weather/setup",
        probe=_probe_weather,
    )
    return HostActionRegistration(
        actions=(
            HostActionDescriptor(
                capability=capability,
                tool_name=HostToolName("weather_get_forecast"),
                handler=_handle_forecast,
                access=HostActionAccess.READ,
                approval=ApprovalContract(),
                idempotency=IdempotencyContract(IdempotencyMode.NOT_REQUIRED),
                audit=AuditContract(),
            ),
        ),
    )
```

`CapabilityDescriptor` owns the operator-facing identity, requirements,
read-only probe, setup and recovery guidance, documentation, and semantic
action links. `HostActionDescriptor` owns dispatch metadata: tool name,
handler, read/write access, approval scope and expiry, idempotency, and terminal
audit contract. `ApprovalContract()` defaults to one approval per exact request.
Use `ApprovalContract(mode=ApprovalMode.SESSION_TOOL)` only for a tool whose
multi-step workflow should reuse approval until the active agent session ends.

For a new semantic action, the same plugin also implements
[`pynchy_action_specs`](#pynchy_action_specs). A plugin may use a built-in
action ID only when it implements that existing action and exposes the exact
registered surface.

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
    Host-side handlers run **in the host process** with full access to host resources. Pynchy re-checks current capability policy and tool trust at dispatch, records terminal outcomes, and uses the IPC request ledger for writes. A status snapshot is diagnostic and never authorizes execution.

!!! note "Legacy adapter"
    `{"tools": {name: handler}, "read_tools": (...)}` remains supported for
    existing plugins. Startup converts each entry to a typed descriptor only
    when an effective `ActionSpec` already exposes that tool. Unknown legacy
    tools fail closed. New plugins should return `HostActionRegistration`.

The in-container IPC proxy tool must also exist in the selected agent image.
`pynchy_service_handler` registers the privileged host half; it does not inject
Python code into a running agent container.

The hook can receive arguments contributed by other plugin categories. A plugin
only declares the arguments it consumes; pluggy permits implementations to omit
the rest. The built-in computer-use router, for example, declares
`computer_use_backends: tuple[object, ...]` to compose provider contributions.

## pynchy_computer_use_backend

Provide a platform-specific implementation for the backend-neutral
`computer_use` host action.

**Calling strategy:** All results are collected. The computer-use router chooses
the first available provider named in `[plugins.computer-use.options].providers`.
Unavailable providers can fall through; execution failures do not retry through
another provider because a mutating action may have partially completed.

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pluggy

from pynchy.plugins.computer_use import (
    ComputerUseBackendAvailability,
    ComputerUseRequest,
)

hookimpl = pluggy.HookimplMarker("pynchy")


@dataclass(frozen=True)
class MyDesktopBackend:
    @property
    def name(self) -> str:
        return "my-desktop"

    def availability(self) -> ComputerUseBackendAvailability:
        return ComputerUseBackendAvailability(available=True)

    async def execute(
        self,
        request: ComputerUseRequest,
        *,
        screenshot_path: Path | None = None,
    ) -> dict[str, Any]:
        ...


class MyDesktopPlugin:
    @hookimpl
    def pynchy_computer_use_backend(self) -> MyDesktopBackend:
        return MyDesktopBackend()
```

Provider names must be unique. Implementations receive a validated, closed
`ComputerUseRequest`; they should translate it to an allowlisted native API or
subprocess invocation rather than accepting arbitrary command arguments. The
router retains ownership of host-action policy, audit, idempotency, source-group
attribution, and screenshot paths.

## pynchy_action_specs

Provide semantic action contracts owned by a plugin.

```python
@hookimpl
def pynchy_action_specs(self) -> tuple[ActionSpec, ...]:
    return (
        ActionSpec(
            id=ActionId("weather.forecast.read"),
            owner="weather-plugin",
            summary="Read the current weather forecast.",
            surfaces=(
                ActionSurface(
                    transport=ActionTransport.AGENT_TOOL,
                    name="weather_get_forecast",
                ),
            ),
        ),
    )
```

Built-in and plugin-owned specifications are validated together. Duplicate or
invalid action IDs fail startup. Provider-changing actions should request
agentic evidence and name a canary scenario; every plugin should run its own
hermetic action-coverage gate in CI. See [Action coverage](../architecture/action-coverage.md).

## pynchy_skill_paths

Provide agent skills (markdown instruction files) that get mounted into the container.

**Calling strategy:** All results collected and flattened. Skills are filtered by the selected profile's `skills` field before being copied into each core's session directory.

```python
@hookimpl
def pynchy_skill_paths(self) -> list[str]:
    return [str(Path(__file__).parent / "skills" / "code-review")]
```

**Return value:** List of absolute paths to skill directories. Each directory must contain a `SKILL.md` file with Pynchy's supported YAML frontmatter.

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
| `description` | No | Human- and agent-facing summary of what the skill does |
| `tier` | No | Selection label (defaults to `community`) |
| `allowed-tools` | No | Tool permissions (e.g., `Bash(my-tool:*)`) |

**Skill tiers:**

| Tier | Purpose | Filtering behavior |
|------|---------|-------------------|
| `core` | Essential skills useful in all workspaces | Always included |
| `community` | General-purpose skills (default) | Included only when explicitly listed |
| Any other label | A project-defined category such as `dev`, `ops`, or `social` | Included only when explicitly listed |

Profiles opt into skills via the `skills` config field; workspaces receive that selection through their profile:

```toml
[profiles.my-profile]
skills = ["core", "dev"]           # tier names and/or individual skill names
```

When `skills` is unset, only core-tier skills are included (safe default). When set, entries are unioned — `["core", "my-skill"]` means all core-tier skills plus `my-skill`. Core is always implicit when any filtering is active. Use `["*"]` to include every skill.

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
        workspaces=context.workspaces,
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
| `workspaces` | `Callable[[], dict[str, WorkspaceProfile]]` | Get the configured workspace profiles |
| `send_message` | `Callable[[str, str], Any]` | Send outbound text to a JID |
| `on_reaction_callback` | `Callable[..., None] \| None` | Optional reaction handler |
| `on_ask_user_answer_callback` | `Callable[[str, dict[str, Any]], None] \| None` | Optional structured-answer handler |
| `on_approval_decision_callback` | `Callable[[str, str, str, str], None] \| None` | Optional approval-decision handler |

**Return value:** A `Channel` instance implementing the channel protocol, or `None` to pass.

**Channel protocol:**

```python
class Channel(Protocol):
    name: str
    formatter: Formatter

    async def connect(self) -> None: ...
    async def send_event(self, jid: str, event: OutboundEvent) -> None: ...
    def is_connected(self) -> bool: ...
    def owns_jid(self, jid: str) -> bool: ...
    async def disconnect(self) -> None: ...
    async def reconnect(self) -> None: ...
    def prepare_shutdown(self) -> None: ...
    async def fetch_inbound_since(self, channel_jid: str, since: str) -> InboundFetchResult: ...
```

Optional attributes (check with `hasattr`/`getattr`): `prefix_assistant_name` (bool, default `True`), `set_typing`, `create_group`.

!!! warning
    Channel plugins run **persistently on the host** with full filesystem and network access. This is the highest-risk plugin category. See [Security Model](../architecture/security.md).

## pynchy_speech_synthesizer

Provide a host-side synthesizer for final spoken channel replies.

**Calling strategy:** All valid results are collected; the first provider wins.

```python
@hookimpl
def pynchy_speech_synthesizer(self) -> Any | None:
    return MySpeechSynthesizer()
```

**Speech synthesizer contract:**

| Attribute / Method | Type | Description |
|--------------------|------|-------------|
| `name` | `str` | Provider identifier, for example `"pocket-tts"` |
| `synthesize(text, output_path)` | `async (str, Path) -> SpeechSynthesisResult` | Write synthesized audio to the requested local path |
| `health()` | `async () -> SpeechSynthesizerHealth` | Return provider readiness, endpoint, and an optional error |

Channel plugins receive the selected provider in
`ChannelPluginContext.speech_synthesizer`. A channel must tolerate `None` and
skip playback rather than choosing a fallback provider itself. The `/status`
endpoint calls `health()` and exposes the result in its `speech` section.

!!! warning
    Speech synthesis plugins run in the host process. Treat remote endpoints,
    model files, and provider credentials as host infrastructure, not container
    resources.

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

**Built-in:** Tailscale ships as a built-in plugin (`src/pynchy/plugins/tunnels/tailscale.py`). It shells out to `tailscale status --json` and checks `BackendState`.

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

The built-in SQLite observer subscribes only to operational events. Durable LLM
trace history is exported by LiteLLM to Phoenix.

**Built-in:** The SQLite observer (`src/pynchy/plugins/observers/sqlite_observer/`)
stores operational event summaries to a dedicated `events` table in the main
database.

!!! warning
    Observer plugins run **in the host process** and subscribe to every event. A misbehaving observer can slow down event dispatch. Keep handlers light and non-blocking.

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

**Built-in:** The SQLite memory plugin (`src/pynchy/plugins/memory/sqlite_memory/`) stores memories in its dedicated `data/memories.db` database with FTS5 full-text search.

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
| `name` | `str` | Server identifier matched by MCP-backed tool names |
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

**Instance expansion:** Users don't configure the base spec. They declare *instances* in `config.toml` that reference the plugin-provided template:

```toml
[tools."gdrive.anyscale"]
type = "mcp"

[tools."gdrive.anyscale".mcp]
runtime = "docker"
volumes = ["data/chrome-profiles/anyscale:/home/chrome"]
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
            "profiles": ["code-improver"],
        },
    }
```

**Return keys:**

| Key | Type | Description |
|-----|------|-------------|
| `folder` | `str` | Workspace folder name |
| `config` | `dict[str, Any]` | `WorkspaceConfig`-compatible fields |

Agent instructions are now delivered via [prompts](../usage/prompts.md) rather than seeded CLAUDE.md files. The `claude_md` field is ignored.

## Multi-Category Plugins

A single plugin can implement multiple hooks:

```python
class CalendarPlugin:
    """Provides calendar service handlers AND calendar skills."""

    @hookimpl
    def pynchy_service_handler(self) -> HostActionRegistration:
        return CALENDAR_HOST_ACTIONS

    @hookimpl
    def pynchy_action_specs(self) -> tuple[ActionSpec, ...]:
        return CALENDAR_ACTION_SPECS

    @hookimpl
    def pynchy_skill_paths(self) -> list[str]:
        return [str(Path(__file__).parent / "skills" / "calendar")]
```

No categories attribute needed. Pluggy figures out capabilities from which hooks the class implements.

## Hook Execution Order

Pluggy supports ordering hints:

```python
@hookimpl(trylast=True)   # Run after other plugins
@hookimpl(tryfirst=True)  # Run before other plugins
```

Most plugins don't need this. Use it when one plugin needs to see or modify another plugin's results.
