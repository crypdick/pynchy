# Hook Reference

Pynchy plugins implement hooks defined in `src/pynchy/plugin/hookspecs.py`. Each hook corresponds to a plugin category. A plugin can implement any combination of hooks.

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

**Calling strategy:** `firstresult=True` — only the first non-`None` return wins.

```python
@hookimpl
def pynchy_create_channel(self, context: Any) -> Any | None:
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        return None  # This plugin doesn't apply
    return TelegramChannel(
        bot_token=bot_token,
        on_message=context.on_message_callback,
        registered_groups=context.registered_groups,
    )
```

**Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `context` | `PluginContext` | Provides `registered_groups` and `send_message` callbacks |

**Return value:** A `Channel` instance implementing the channel protocol, or `None` to pass.

**Channel protocol:**

```python
class Channel(Protocol):
    name: str
    prefix_assistant_name: bool

    async def connect(self) -> None: ...
    async def send_message(self, jid: str, text: str) -> None: ...
    async def disconnect(self) -> None: ...
    def is_connected(self) -> bool: ...
    def owns_jid(self, jid: str) -> bool: ...
```

!!! warning
    Channel plugins run **persistently on the host** with full filesystem and network access. This is the highest-risk plugin category. See [Security Model](../security.md).

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

No categories attribute needed — pluggy determines capabilities by which hooks are implemented.

## Hook Execution Order

Pluggy supports ordering hints:

```python
@hookimpl(trylast=True)   # Run after other plugins
@hookimpl(tryfirst=True)  # Run before other plugins
```

Most plugins don't need this. It's useful when one plugin needs to see or modify another plugin's results.
