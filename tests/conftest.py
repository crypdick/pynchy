"""Shared test fixtures for Pynchy."""

from __future__ import annotations

import re

import pytest
from pydantic import BaseModel, SecretStr

from pynchy.types import NewMessage

# ---------------------------------------------------------------------------
# Shared helpers (plain functions, not fixtures — importable by test files)
# ---------------------------------------------------------------------------

# Cached property names that must be set via __dict__ (not model_construct).
_CACHED_PROPERTY_NAMES = frozenset(
    {
        "project_root",
        "home_dir",
        "store_dir",
        "groups_dir",
        "data_dir",
        "mount_allowlist_path",
        "worktrees_dir",
        "plugins_dir",
        "container_timeout",
        "idle_timeout",
        "trigger_pattern",
        "timezone",
    }
)


def make_settings(**overrides):
    """Create a Settings object with sensible defaults for testing.

    Accepts both model fields (agent, container, etc.) and cached property
    overrides (project_root, data_dir, groups_dir, etc.).

    Usage::

        s = make_settings(data_dir=tmp_path)
        s = make_settings(container=ContainerConfig(max_concurrent=3))
        s = make_settings(project_root=tmp_path, groups_dir=tmp_path / "groups")
    """
    from pynchy.config import (
        AgentConfig,
        ChannelsConfig,
        CommandWordsConfig,
        ContainerConfig,
        IntervalsConfig,
        LoggingConfig,
        QueueConfig,
        SchedulerConfig,
        SecretsConfig,
        SecurityConfig,
        ServerConfig,
        Settings,
        WorkspaceDefaultsConfig,
    )

    # Separate cached properties from model fields
    cached = {k: overrides.pop(k) for k in list(overrides) if k in _CACHED_PROPERTY_NAMES}

    defaults = {
        "agent": AgentConfig(),
        "container": ContainerConfig(),
        "server": ServerConfig(),
        "logging": LoggingConfig(),
        "secrets": SecretsConfig(),
        "workspace_defaults": WorkspaceDefaultsConfig(),
        "workspaces": {},
        "commands": CommandWordsConfig(),
        "scheduler": SchedulerConfig(),
        "intervals": IntervalsConfig(),
        "queue": QueueConfig(),
        "security": SecurityConfig(),
        "channels": ChannelsConfig(),
        "plugins": {},
        "cron_jobs": {},
    }
    defaults.update(overrides)
    s = Settings.model_construct(**defaults)

    for key, value in cached.items():
        s.__dict__[key] = value

    return s


# ---------------------------------------------------------------------------
# Secret scrubbing — detect credentials in both SecretStr and plain str fields
# ---------------------------------------------------------------------------

# Known credential prefixes / patterns in plain strings.
# Catches tokens even if someone puts them in a non-SecretStr field or URL.
_CREDENTIAL_RE = re.compile(
    r"xoxb-"  # Slack bot token
    r"|xapp-"  # Slack app-level token
    r"|sk-ant-"  # Anthropic API key
    r"|sk-proj-"  # OpenAI API key
    r"|ghp_|gho_|ghs_"  # GitHub PAT / OAuth / server token
    r"|://[^/\s]*:[^@\s]+@",  # credentials embedded in URLs  (user:pass@host)
)


def _scrub_model(obj: BaseModel) -> None:
    """Recursively nullify SecretStr fields and credential-bearing strings.

    Walks all Pydantic model fields (including nested sub-models and dicts of
    sub-models) and replaces:
    - ``SecretStr`` values → ``None``
    - Plain ``str`` values matching ``_CREDENTIAL_RE`` → ``""``
    """
    for name in type(obj).model_fields:
        val = getattr(obj, name, None)
        if val is None:
            continue

        if isinstance(val, SecretStr):
            object.__setattr__(obj, name, None)
        elif isinstance(val, BaseModel):
            _scrub_model(val)
        elif isinstance(val, str) and _CREDENTIAL_RE.search(val):
            object.__setattr__(obj, name, "")
        elif isinstance(val, dict):
            for v in val.values():
                if isinstance(v, BaseModel):
                    _scrub_model(v)


# ---------------------------------------------------------------------------
# Autouse fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_settings(monkeypatch):
    """Ensure each test starts with a Settings singleton scrubbed of secrets.

    Settings are loaded from config.toml as usual (so non-secret config like
    ``agent.name`` and ``container.image`` are available), then all SecretStr
    fields and credential-bearing plain strings are replaced with safe values.

    Tests that mock ``get_settings()`` at the call site are unaffected — their
    mock takes precedence over the cached singleton.
    """
    from pynchy.config import Settings

    safe = Settings()
    _scrub_model(safe)
    monkeypatch.setattr("pynchy.config._settings", safe)


# ---------------------------------------------------------------------------
# Reusable fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def make_msg():
    """Factory fixture for creating test messages with defaults."""

    def _make(
        *,
        id: str = "1",
        chat_jid: str = "group@g.us",
        sender: str = "123@s.whatsapp.net",
        sender_name: str = "Alice",
        content: str = "hello",
        timestamp: str = "2024-01-01T00:00:00.000Z",
        is_from_me: bool | None = None,
    ) -> NewMessage:
        return NewMessage(
            id=id,
            chat_jid=chat_jid,
            sender=sender,
            sender_name=sender_name,
            content=content,
            timestamp=timestamp,
            is_from_me=is_from_me,
        )

    return _make
