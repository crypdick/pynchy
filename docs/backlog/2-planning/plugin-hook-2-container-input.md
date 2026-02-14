# Plugin Hook: Step 2 - Extend ContainerInput

## Overview

Extend `ContainerInput` to carry hook plugin configuration from the host to the container.

## Scope

This step adds the data structures needed to communicate hook plugin information across the container boundary. No actual hook loading yet.

## Dependencies

- ✅ Step 1: HookPlugin base class (must be completed first)

## Implementation

### 1. Extend ContainerInput Type

**File:** `src/pynchy/types.py`

Add field to `ContainerInput` dataclass:

```python
@dataclass
class ContainerInput:
    """Input data passed from host to container agent."""

    prompt: str
    group_folder: str
    chat_jid: str
    is_god: bool
    session_id: str | None = None
    is_scheduled_task: bool = False
    plugin_mcp_servers: list[dict[str, Any]] | None = None
    plugin_hooks: list[dict[str, str]] | None = None  # NEW
```

Where each dict in `plugin_hooks` contains:
```python
{
    "name": str,           # Plugin name (e.g., "agent-logger")
    "module_path": str,    # Module to import (e.g., "pynchy_plugin_agent_logger.hooks")
}
```

### 2. Update Serialization in container_runner.py

**File:** `src/pynchy/container_runner.py`

Update `_input_to_dict()` to include plugin_hooks:

```python
def _input_to_dict(input_data: ContainerInput) -> dict[str, Any]:
    """Convert ContainerInput to dict for the Python agent-runner."""
    d: dict[str, Any] = {
        "prompt": input_data.prompt,
        "group_folder": input_data.group_folder,
        "chat_jid": input_data.chat_jid,
        "is_god": input_data.is_god,
    }
    if input_data.session_id is not None:
        d["session_id"] = input_data.session_id
    if input_data.is_scheduled_task:
        d["is_scheduled_task"] = True
    if input_data.plugin_mcp_servers is not None:
        d["plugin_mcp_servers"] = input_data.plugin_mcp_servers
    if input_data.plugin_hooks is not None:  # NEW
        d["plugin_hooks"] = input_data.plugin_hooks
    return d
```

### 3. Update ContainerInput in agent_runner

**File:** `container/agent_runner/src/agent_runner/main.py`

Update the `ContainerInput` class to accept plugin_hooks:

```python
class ContainerInput:
    def __init__(self, data: dict[str, Any]) -> None:
        self.prompt: str = data["prompt"]
        self.session_id: str | None = data.get("session_id")
        self.group_folder: str = data["group_folder"]
        self.chat_jid: str = data["chat_jid"]
        self.is_god: bool = data["is_god"]
        self.is_scheduled_task: bool = data.get("is_scheduled_task", False)
        self.plugin_hooks: list[dict[str, str]] = data.get("plugin_hooks", [])  # NEW
```

## Tests

**File:** `tests/test_plugin_hook_input.py`

```python
"""Tests for hook plugin container input handling."""

from pynchy.types import ContainerInput
from pynchy.container_runner import _input_to_dict


def test_container_input_with_hooks():
    """Test ContainerInput with plugin_hooks field."""
    input_data = ContainerInput(
        prompt="test",
        group_folder="main",
        chat_jid="test@jid",
        is_god=True,
        plugin_hooks=[
            {"name": "logger", "module_path": "plugin_logger.hooks"},
            {"name": "monitor", "module_path": "plugin_monitor.hooks"},
        ],
    )

    assert input_data.plugin_hooks is not None
    assert len(input_data.plugin_hooks) == 2
    assert input_data.plugin_hooks[0]["name"] == "logger"


def test_container_input_without_hooks():
    """Test ContainerInput without plugin_hooks (defaults to None)."""
    input_data = ContainerInput(
        prompt="test",
        group_folder="main",
        chat_jid="test@jid",
        is_god=True,
    )

    assert input_data.plugin_hooks is None


def test_input_to_dict_serialization():
    """Test _input_to_dict includes plugin_hooks when present."""
    input_data = ContainerInput(
        prompt="test",
        group_folder="main",
        chat_jid="test@jid",
        is_god=True,
        plugin_hooks=[{"name": "test", "module_path": "test.hooks"}],
    )

    result = _input_to_dict(input_data)
    assert "plugin_hooks" in result
    assert result["plugin_hooks"] == [{"name": "test", "module_path": "test.hooks"}]


def test_input_to_dict_omits_empty_hooks():
    """Test _input_to_dict omits plugin_hooks when None."""
    input_data = ContainerInput(
        prompt="test",
        group_folder="main",
        chat_jid="test@jid",
        is_god=True,
    )

    result = _input_to_dict(input_data)
    assert "plugin_hooks" not in result
```

## Success Criteria

- [ ] `ContainerInput.plugin_hooks` field added to `types.py`
- [ ] `_input_to_dict()` serializes plugin_hooks
- [ ] Agent runner's ContainerInput accepts plugin_hooks
- [ ] Tests pass
- [ ] No actual hook loading yet (comes in later steps)

## Next Steps

After this is complete:
- Step 3: Collect hook configs and mount plugin sources
- Step 4: Import and register hooks in agent runner
