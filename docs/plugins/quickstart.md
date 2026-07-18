# Quickstart: Build Your First Plugin

This guide walks through creating, installing, and testing a Pynchy plugin.
You'll build a small skill plugin, the shortest complete extension that works
with every agent core.

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
description = "Hello world skill for Pynchy"
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
from pathlib import Path

import pluggy

hookimpl = pluggy.HookimplMarker("pynchy")


class HelloPlugin:
    """Skill plugin that teaches agents a repeatable greeting workflow."""

    @hookimpl
    def pynchy_skill_paths(self) -> list[str]:
        return [str(Path(__file__).parent / "skills" / "hello")]
```

Create `src/pynchy_plugin_hello/skills/hello/SKILL.md`:

```markdown
---
name: hello
description: Write a warm, concise greeting for a named person.
tier: community
---

Ask who the greeting is for if no name was supplied. Return one sentence,
using the person's name and no generic preamble.
```

That's the whole plugin. The `@hookimpl` decorator tells pluggy that the class
implements `pynchy_skill_paths`; no base class or manual registry is needed.

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
Plugin manager ready  plugins=[..., 'hello']
```

The exact built-in inventory changes with Pynchy releases; confirm that your
entry-point key (`hello`) appears in the final inventory.

Add `hello` to a profile's `skills` list and restart Pynchy. Workspaces using
that profile now receive the skill.

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
        ├── __init__.py
        └── skills/
            └── hello/
                └── SKILL.md
```

To add a privileged host action, read
[`pynchy_service_handler`](hooks.md#pynchy_service_handler) next. Host actions
need a typed descriptor, a semantic `ActionSpec`, an agent-container tool
surface, policy and idempotency contracts, and behavioral coverage; a raw
handler dictionary is not a complete agent tool.
