# Agent-core hook

## `pynchy_agent_core_info`

Provide an alternative LLM agent framework. Pynchy collects every core and
selects one with `[agent].default_core` or `AGENT__DEFAULT_CORE`.

```python
@hookimpl
def pynchy_agent_core_info(self) -> dict[str, str | list[str] | None]:
    return {
        "name": "ollama",
        "module": "pynchy_plugin_ollama.core",
        "class_name": "OllamaAgentCore",
        "packages": ["ollama>=0.1.0"],
        "host_source_path": str(Path(__file__).parent),
    }
```

| Key | Type | Description |
|-----|------|-------------|
| `name` | `str` | Unique core identifier. |
| `module` | `str` | Module importable inside the container. |
| `class_name` | `str` | Core class to instantiate. |
| `packages` | `list[str]` | Packages to install in the container. |
| `host_source_path` | `str \| None` | Host source mounted at `/workspace/plugins/{name}/`. |

For selecting built-in cores, see [Agent cores](../../usage/agent-cores.md).
