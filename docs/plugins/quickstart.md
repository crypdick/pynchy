# Quickstart: Build Your First Plugin

This guide walks you through creating, installing, and testing a pynchy plugin. You'll build a minimal MCP server plugin that provides a single tool to the agent.

## Prerequisites

- A working pynchy installation (see [Installation](../install.md))
- `uv` for Python package management

## 1. Scaffold the Plugin

!!! tip
    The [`cookiecutter-pynchy-plugin`](https://github.com/crypdick/cookiecutter-pynchy-plugin) repository provides a ready-made template for this structure. Use it if you want a generated starter project with optional hook scaffolding.

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
description = "Hello world MCP tool for pynchy"
requires-python = ">=3.12"
dependencies = ["mcp>=1.0.0"]

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
from pathlib import Path

hookimpl = pluggy.HookimplMarker("pynchy")


class HelloPlugin:
    """MCP server plugin that provides a 'hello' tool."""

    @hookimpl
    def pynchy_mcp_server_spec(self) -> dict:
        return {
            "name": "hello",
            "command": "python",
            "args": ["-m", "pynchy_plugin_hello.server"],
            "env": {},
            "host_source": str(Path(__file__).parent),
        }
```

That covers the entire plugin class. The `@hookimpl` decorator tells pluggy this class implements the `pynchy_mcp_server_spec` hook. No base classes, no registration boilerplate.

## 3. Write the MCP Server

Create `src/pynchy_plugin_hello/server.py`:

```python
"""Minimal MCP server with one tool."""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("hello")


@mcp.tool()
def hello(name: str) -> str:
    """Say hello to someone."""
    return f"Hello, {name}! This is a pynchy plugin tool."


if __name__ == "__main__":
    mcp.run()
```

## 4. Install and Test

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
Plugin manager ready  plugins=['builtin-agent_claude', 'builtin-agent_openai', 'hello']
```

The agent now has a `hello` tool available via MCP.

## 5. Uninstall

```bash
uv pip uninstall pynchy-plugin-hello
```

Restart pynchy — the tool disappears.

## What's Next

- [**Hook Reference**](hooks.md) — Learn about all 4 plugin categories
- [**Packaging**](packaging.md) — Publish your plugin to PyPI or share via git

## Final Plugin Structure

```
pynchy-plugin-hello/
├── pyproject.toml
└── src/
    └── pynchy_plugin_hello/
        ├── __init__.py     # Plugin class with @hookimpl
        └── server.py       # MCP server implementation
```
