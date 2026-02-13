# MCP Plugins

## Overview

Enable agent tools (MCP servers) to be provided by external plugins. Plugins package their MCP server code and declare how to run it.

## Dependencies

- Plugin discovery system (plugin-discovery.md)

## Design

### McpPlugin Class

```python
class McpPlugin(PluginBase):
    """Base class for MCP tool plugins."""

    categories = ["mcp"]  # Fixed

    @abstractmethod
    def mcp_server_spec(self) -> McpServerSpec:
        """Return MCP server configuration for the container agent."""
        ...
```

### McpServerSpec

```python
@dataclass
class McpServerSpec:
    """Specification for running an MCP server inside the agent container."""

    name: str                       # MCP server name (e.g., "voice")
    command: str                    # Command inside container (e.g., "python")
    args: list[str]                 # e.g., ["-m", "pynchy_plugin_voice.mcp"]
    env: dict[str, str] = field(default_factory=dict)  # Extra env vars
    host_source: Path | None = None # Plugin package dir to mount
```

## Example: Voice Plugin

**pyproject.toml:**
```toml
[project]
name = "pynchy-plugin-voice"
dependencies = ["pynchy", "openai-whisper"]

[project.entry-points."pynchy.plugins"]
voice = "pynchy_plugin_voice:VoicePlugin"
```

**Plugin structure:**
```
pynchy-plugin-voice/
├── pyproject.toml
└── src/
    └── pynchy_plugin_voice/
        ├── __init__.py     # exports VoicePlugin
        ├── plugin.py       # McpPlugin implementation
        └── mcp.py          # MCP server (transcribe_voice tool)
```

**plugin.py:**
```python
from pathlib import Path
from pynchy.plugin import McpPlugin, McpServerSpec

class VoicePlugin(McpPlugin):
    name = "voice"
    version = "0.1.0"
    description = "Voice transcription via Whisper"

    def mcp_server_spec(self) -> McpServerSpec:
        return McpServerSpec(
            name="voice",
            command="python",
            args=["-m", "pynchy_plugin_voice.mcp"],
            env={},
            host_source=Path(__file__).parent,
        )
```

**mcp.py:**
Standard MCP server that provides `transcribe_voice` tool.

## Container Integration

The agent container needs access to plugin code:

1. **Mount plugin source:**
   - `host_source` directory mounted to `/workspace/plugins/{name}/`
   - `PYTHONPATH` includes plugin mount point

2. **MCP server config:**
   - Plugin specs collected during discovery
   - Serialized and passed to container via `ContainerInput`
   - Agent runner merges into `ClaudeAgentOptions.mcp_servers`

3. **Inside container:**
   ```python
   # Agent runner reads plugin MCP configs
   plugin_mcps = input_data.get("plugin_mcp_servers", {})

   for name, spec in plugin_mcps.items():
       options.mcp_servers[name] = {
           "command": spec["command"],
           "args": spec["args"],
           "env": {
               **spec["env"],
               "PYTHONPATH": f"/workspace/plugins/{name}",
           }
       }
   ```

## Implementation Steps

1. Define `McpPlugin` and `McpServerSpec` in `plugin/mcp.py`
2. Extend `ContainerInput` in `types.py`:
   - Add `plugin_mcp_servers: dict[str, dict] | None = None`
3. Update `container_runner.py`:
   - `_build_volume_mounts()` adds plugin mounts
   - `_input_to_dict()` includes plugin MCP specs
   - Accept `PluginRegistry` parameter
4. Update `agent_runner/main.py`:
   - Read `plugin_mcp_servers` from input
   - Merge into `options.mcp_servers`
5. Tests: mount verification, MCP config merging, tool invocation

## Integration Points

- `container_runner.py:_build_volume_mounts()` — adds plugin source mounts
- `container_runner.py:_input_to_dict()` — serializes MCP specs to JSON
- `agent_runner/main.py` — reads specs, configures MCP servers
- Container Dockerfile — must include dependencies (or plugins declare them)

## Open Questions

- How to handle plugin dependencies not in base container image?
- Should plugins be able to specify required container packages?
- Do we need version compatibility checking between plugin and pynchy?
- How to handle MCP server crashes or failures?
- Should plugins be able to provide multiple MCP servers?

## Container Dependency Note

Plugin MCP servers run inside the container. They can use:
- Packages in container image (`mcp`, `croniter`, standard lib)
- Their own code (mounted from `host_source`)

If a plugin needs extra packages (e.g., `openai`), the user must add them to the container Dockerfile. The plugin's README should document this.

**Alternative approach:** Plugins could declare a `requirements.txt`, and the container build could optionally install them. This needs design work.

## Verification

1. Create test MCP plugin with simple tool
2. Install: `uv pip install -e /tmp/pynchy-plugin-test-mcp`
3. Verify mount appears in container args
4. Verify MCP server config in agent input JSON
5. Start agent, verify tool is available
6. Invoke tool from agent, verify it works
7. Uninstall and verify tool disappears
