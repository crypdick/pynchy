# Quickstart: Build Your First Plugin

This guide walks you through creating, installing, and testing a pynchy plugin. You'll build a service handler plugin that provides a host-side tool to the agent.

## Prerequisites

- A working pynchy installation (see [Installation](../install.md))
- `uv` for Python package management

## 1. Scaffold the Plugin

Create a new directory for your plugin:

```bash
mkdir pynchy-plugin-hello
cd pynchy-plugin-hello
```

Create `pyproject.toml`:

```toml
[project]
name = "pynchy-plugin-hello"
version = "0.1.0"
description = "Hello world service tool for pynchy"
requires-python = ">=3.12"
dependencies = []

[project.entry-points."pynchy"]
hello = "pynchy_plugin_hello:HelloPlugin"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

!!! note
    The entry point group must be `"pynchy"` — this is what pluggy scans during discovery.

## 2. Write the Plugin Class

Create `src/pynchy_plugin_hello/__init__.py`:

```python
import pluggy

hookimpl = pluggy.HookimplMarker("pynchy")


class HelloPlugin:
    """Service handler plugin that provides a 'hello' tool."""

    @hookimpl
    def pynchy_service_handler(self) -> dict:
        return {
            "tools": {
                "hello": _handle_hello,
            },
        }


async def _handle_hello(data: dict) -> dict:
    name = data.get("name", "World")
    return {"result": f"Hello, {name}! This is a pynchy plugin tool."}
```

That covers the entire plugin. The `@hookimpl` decorator tells pluggy this class implements the `pynchy_service_handler` hook. No base classes, no registration boilerplate.

The handler function runs on the **host process** and is dispatched via IPC when an agent invokes the `hello` service tool. Policy middleware (risk tiers, rate limits) is enforced before your handler is called.

## 3. Install and Test

Install your plugin in editable mode (from the pynchy virtualenv):

```bash
uv pip install -e /path/to/pynchy-plugin-hello
```

Verify it's discoverable:

```bash
uv pip list | grep pynchy-plugin
```

Restart pynchy. Check the logs for:

```
Discovered third-party plugins  count=1
Plugin manager ready  plugins=['builtin-agent_claude', 'builtin-agent_openai', 'builtin-mcp_caldav', 'builtin-slack', 'builtin-tailscale', 'hello']
```

The agent now has a `hello` service tool available via IPC.

## 4. Uninstall

```bash
uv pip uninstall pynchy-plugin-hello
```

Restart pynchy — the tool disappears.

## What's Next

- [**Hook Reference**](hooks.md) — Learn about all plugin hooks
- [**Packaging**](packaging.md) — Publish your plugin to PyPI or share via git

## Final Plugin Structure

```
pynchy-plugin-hello/
├── pyproject.toml
└── src/
    └── pynchy_plugin_hello/
        └── __init__.py     # Plugin class with @hookimpl + handler
```
