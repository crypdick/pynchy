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
        "groups_dir",
        "data_dir",
        "mount_allowlist_path",
        "worktrees_dir",
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
        CommandCenterConfig,
        CommandWordsConfig,
        ConnectionsConfig,
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
        "command_center": CommandCenterConfig(),
        "connection": ConnectionsConfig(),
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


@pytest.fixture(autouse=True, scope="session")
def _clean_git_env():
    """Strip git env vars that pre-commit leaks during its stash cycle.

    Pre-commit sets GIT_INDEX_FILE (and potentially GIT_DIR, GIT_WORK_TREE)
    before running hooks. Tests that create temporary git repos inherit these
    variables, causing ``git worktree add`` and similar commands to fail with
    ``fatal: .git/index: index file open failed: Not a directory``.
    """
    import os

    for var in ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE"):
        os.environ.pop(var, None)


@pytest.fixture(autouse=True)
def reset_settings(monkeypatch):
    """Ensure each test starts with a clean Settings singleton.

    Uses ``make_settings()`` to build from pure defaults — no config.toml,
    no .env, no file I/O. Tests are fully isolated from production config.

    Tests that mock ``get_settings()`` at the call site are unaffected — their
    mock takes precedence over the cached singleton.
    """
    safe = make_settings()
    monkeypatch.setattr("pynchy.config.settings._settings", safe)


@pytest.fixture(autouse=True, scope="session")
def _close_test_database():
    """Close the aiosqlite connection after all tests complete.

    Uses ``stop()`` + thread join rather than ``await close()`` because
    the connection was created on a function-scoped event loop (during a
    test).  ``stop()`` bypasses the event loop by putting the close
    command directly on the worker thread's queue.

    This is a sync fixture so it runs during session teardown regardless
    of event loop state — avoids the race where pytest-xdist workers
    close the loop before an async session fixture can tear down.
    """
    yield
    import pynchy.state.connection as db_conn

    if db_conn._db is not None:
        db_conn._db.stop()
        if db_conn._db._thread is not None and db_conn._db._thread.is_alive():
            db_conn._db._thread.join(timeout=2)
        db_conn._db = None


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
