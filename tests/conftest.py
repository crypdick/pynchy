"""Shared test fixtures for Pynchy."""

from __future__ import annotations

import os
import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import BaseModel, SecretStr

from pynchy.actions import ACTION_SPECS, ActionId, assess_hermetic_coverage
from pynchy.capabilities import (
    ApprovalContract,
    ApprovalMode,
    AuditContract,
    CapabilityDescriptor,
    CapabilityId,
    CapabilityKind,
    HostActionAccess,
    HostActionDescriptor,
    HostToolName,
    IdempotencyContract,
    IdempotencyMode,
)
from pynchy.config import (
    AgentConfig,
    CanaryConfig,
    CommandCenterConfig,
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
)
from pynchy.config.settings_sources import repository_settings_sources
from pynchy.host.container_manager.security.cop import (
    CopContextAvailability,
    CopInspectionContext,
    CopVerdict,
)
from pynchy.plugins.host_actions import HostActionCatalog
from pynchy.state import close_test_database, init_test_database
from pynchy.types import InboundFetchResult, NewMessage


@pytest.fixture(autouse=True)
def _clean_host_mutation_cop():
    """Give non-security tests a hermetic, successful Cop boundary."""
    with (
        patch(
            "pynchy.host.container_manager.security.cop_gate.inspect_outbound",
            new_callable=AsyncMock,
            return_value=CopVerdict(flagged=False),
        ),
        patch(
            "pynchy.host.container_manager.security.cop_gate.load_cop_inspection_context",
            new_callable=AsyncMock,
            return_value=CopInspectionContext(availability=CopContextAvailability.AVAILABLE),
        ),
    ):
        yield


__all__ = [
    "NullChannel",
    "NullIpcDeps",
    "init_test_database",
    "make_host_action_catalog",
]

_CGROUP_MEMORY_LIMIT_PATHS = (
    Path("/sys/fs/cgroup/memory.max"),
    Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
)
_UNBOUNDED_CGROUP_LIMIT = 1 << 60
_PYTEST_CONTROLLER_RESERVE_BYTES = 768 * 1024 * 1024
_PYTEST_WORKER_BUDGET_BYTES = 1024 * 1024 * 1024


def cgroup_memory_limit_bytes(
    paths: tuple[Path, ...] = _CGROUP_MEMORY_LIMIT_PATHS,
) -> int | None:
    """Read a finite cgroup v2 or v1 memory limit."""
    for path in paths:
        try:
            raw_limit = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if raw_limit == "max":
            continue
        try:
            limit = int(raw_limit)
        except ValueError:
            continue
        if 0 < limit < _UNBOUNDED_CGROUP_LIMIT:
            return limit
    return None


def pytest_xdist_auto_num_workers(config: pytest.Config) -> int | None:
    """Scale ``-n auto`` down when the test process is memory-constrained."""
    _ = config
    configured = os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS")
    if configured is not None and configured.isdigit() and int(configured) > 0:
        return int(configured)

    memory_limit = cgroup_memory_limit_bytes()
    if memory_limit is None:
        return None
    available_for_workers = max(0, memory_limit - _PYTEST_CONTROLLER_RESERVE_BYTES)
    memory_workers = max(1, available_for_workers // _PYTEST_WORKER_BUDGET_BYTES)
    return min(os.process_cpu_count() or 1, memory_workers)


def pytest_addoption(parser):
    """Register the opt-in dynamic action-coverage collection gate."""
    parser.addoption(
        "--action-coverage",
        action="store_true",
        default=False,
        help="require every registered action to have a pytest.mark.action test",
    )


def pytest_collection_finish(session):
    """Validate action markers after every test has been collected."""
    if not session.config.getoption("--action-coverage"):
        return
    marked_ids = [
        action_id
        for item in session.items
        for marker in item.iter_markers(name="action")
        for action_id in marker.args
    ]
    report = assess_hermetic_coverage(ACTION_SPECS, marked_ids)
    if not report.is_complete:
        raise pytest.UsageError(f"Action coverage incomplete: {report.describe()}")


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
    # Separate cached properties from model fields
    cached = {k: overrides.pop(k) for k in list(overrides) if k in _CACHED_PROPERTY_NAMES}

    defaults = {
        "agent": AgentConfig(),
        "container": ContainerConfig(),
        "server": ServerConfig(),
        "logging": LoggingConfig(),
        "secrets": SecretsConfig(),
        "profiles": {},
        "workspaces": {},
        "commands": CommandWordsConfig(),
        "scheduler": SchedulerConfig(),
        "canary": CanaryConfig(),
        "intervals": IntervalsConfig(),
        "queue": QueueConfig(),
        "security": SecurityConfig(),
        "command_center": CommandCenterConfig(),
        "plugins": {},
        "jobs": {},
    }
    defaults.update(overrides)
    s = Settings.model_construct(**defaults)

    for key, value in cached.items():
        s.__dict__[key] = value

    return s


def make_host_action_catalog(
    *tool_names: str,
    handler,
    read_tools: tuple[str, ...] = (),
    approval_mode: ApprovalMode = ApprovalMode.EXACT_REQUEST,
) -> HostActionCatalog:
    """Build a typed catalog for dispatch-focused tests.

    Catalog validation is covered separately. These tests intentionally use
    synthetic tool names so they can isolate dispatch and approval behavior.
    """
    actions = []
    for tool_name in tool_names:
        access = HostActionAccess.READ if tool_name in read_tools else HostActionAccess.WRITE
        actions.append(
            HostActionDescriptor(
                capability=CapabilityDescriptor(
                    id=CapabilityId(f"test.{tool_name.replace('_', '.')}"),
                    kind=CapabilityKind.HOST_ACTION,
                    owner="tests",
                    summary=f"Exercise the {tool_name} test action.",
                    action_ids=(ActionId(f"test.{tool_name.replace('_', '.')}"),),
                ),
                tool_name=HostToolName(tool_name),
                handler=handler,
                access=access,
                approval=ApprovalContract(mode=approval_mode),
                idempotency=IdempotencyContract(
                    IdempotencyMode.NOT_REQUIRED
                    if access is HostActionAccess.READ
                    else IdempotencyMode.IPC_REQUEST_ID
                ),
                audit=AuditContract(),
            )
        )
    return HostActionCatalog(actions=tuple(actions))


class NullIpcDeps:
    """No-op stand-in for every method on ``IpcDeps``.

    ``beartype_this_package()`` validates fake/mock arguments against the real
    ``IpcDeps`` Protocol at call time — structurally, by attribute name, not
    by behavior. Subclass this and override only the methods your test
    actually exercises; the rest are satisfied for free instead of each fake
    class hand-rolling all fifteen methods.
    """

    async def broadcast_to_channels(self, jid, event) -> None: ...

    async def broadcast_host_message(self, jid, text) -> None: ...

    async def broadcast_system_notice(self, jid, text) -> None: ...

    def workspaces(self) -> dict:
        return {}

    def register_workspace(self, profile) -> None: ...

    async def sync_group_metadata(self, *, force) -> None: ...

    async def get_available_groups(self) -> list:
        return []

    def write_groups_snapshot(
        self,
        group_folder,
        available_groups,
        registered_jids,
        *,
        is_admin,
    ) -> None: ...

    def has_active_session(self, group_folder) -> bool:
        return False

    async def clear_session(self, group_folder) -> None: ...

    def get_active_sessions(self) -> dict:
        return {}

    async def clear_chat_history(self, chat_jid) -> None: ...

    def enqueue_message_check(self, group_jid) -> None: ...

    def channels(self) -> list:
        return []

    def pending_question_store(self):
        return _NullPendingQuestionStore()

    async def request_deploy(
        self,
        *,
        chat_jid=None,
        commit_sha="",
        rebuild=False,
        resume_prompt="",
    ) -> None: ...

    async def trigger_deploy(self, previous_sha, *, rebuild=True) -> None: ...

    async def create_periodic_agent(self, request) -> None: ...

    async def get_scheduled_work_status(self, *, source_group, is_admin) -> tuple[list, list]:
        return [], []


class _NullPendingQuestionStore:
    def create(self, **kwargs) -> None:
        del kwargs

    def update_message_id(self, request_id, source_group, message_id) -> None:
        del request_id, source_group, message_id

    def resolve(self, request_id, source_group) -> None:
        del request_id, source_group


class NullChannel:
    """No-op stand-in for every method on ``Channel``.

    Same rationale as ``NullIpcDeps``: satisfies the ``Channel`` Protocol's
    isinstance check structurally so fakes only need to override the
    handful of members a given test actually exercises.
    """

    name = "null-channel"
    formatter = None

    async def connect(self) -> None: ...

    async def send_event(self, jid, event) -> None: ...

    def is_connected(self) -> bool:
        return True

    def owns_jid(self, jid) -> bool:
        return False

    async def disconnect(self) -> None: ...

    async def reconnect(self) -> None: ...

    def prepare_shutdown(self) -> None: ...

    async def fetch_inbound_since(self, channel_jid, since) -> InboundFetchResult:
        return InboundFetchResult(messages=[])


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
            setattr(obj, name, None)
        elif isinstance(val, BaseModel):
            _scrub_model(val)
        elif isinstance(val, str) and _CREDENTIAL_RE.search(val):
            setattr(obj, name, "")
        elif isinstance(val, dict):
            for v in val.values():
                if isinstance(v, BaseModel):
                    _scrub_model(v)


# ---------------------------------------------------------------------------
# Autouse fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="session")
def _clean_git_env():
    """Strip git env vars that hook runners can leak during their stash cycle.

    Hook runners can set GIT_INDEX_FILE (and potentially GIT_DIR, GIT_WORK_TREE)
    before invoking checks. Tests that create temporary git repos inherit these
    variables, causing ``git worktree add`` and similar commands to fail with
    ``fatal: .git/index: index file open failed: Not a directory``.
    """
    for var in ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE"):
        os.environ.pop(var, None)


@pytest.fixture(autouse=True)
def reset_settings(monkeypatch):
    """Ensure each test starts with a clean Settings singleton.

    Uses ``make_settings()`` to build from pure model defaults — no files,
    no .env, no file I/O. Tests are fully isolated from production config.
    Direct ``Settings()`` calls still read real environment variables, but not
    repo-local config files or dotenv files.

    Tests that mock ``get_settings()`` at the call site are unaffected — their
    mock takes precedence over the cached singleton.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    with repository_settings_sources(enabled=False):
        safe = make_settings()
        monkeypatch.setattr("pynchy.config.settings._state.settings", safe)
        yield


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
    close_test_database()


# ---------------------------------------------------------------------------
# Reusable fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def make_msg():
    """Factory fixture for creating test messages with defaults."""

    def _make(
        *,
        message_id: str = "1",
        chat_jid: str = "group@g.us",
        sender: str = "123@s.whatsapp.net",
        sender_name: str = "Alice",
        content: str = "hello",
        timestamp: str = "2024-01-01T00:00:00.000Z",
        is_from_me: bool | None = None,
    ) -> NewMessage:
        return NewMessage(
            id=message_id,
            chat_jid=chat_jid,
            sender=sender,
            sender_name=sender_name,
            content=content,
            timestamp=timestamp,
            is_from_me=is_from_me,
        )

    return _make
