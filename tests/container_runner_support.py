"""Tests for the container runner."""

from __future__ import annotations

import asyncio
import contextlib
import json
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from conftest import (
    configure_learning_paths_for,
    configure_skill_activation_for,
    make_container_agent_operations,
    make_host_runtime_operations,
    make_settings,
)
from pydantic import SecretStr

from pynchy.agent_protocol.api import (
    AgentExecutionRuntime,
    ContainerInput,
    ContainerOutput,
    parse_container_output,
)
from pynchy.config.api import (
    GatewayConfig,
    LearningConfig,
    ProfileConfig,
    WorkspaceConfig,
)
from pynchy.host.container_manager.api import RepoMount
from pynchy.host.container_manager.gateway_builtin import BuiltinGateway
from pynchy.host.container_manager.mounts import (
    build_volume_mounts as _build_volume_mounts,
)
from pynchy.host.container_manager.session import start_session
from pynchy.host.git_ops.api import RepoContext
from pynchy.host.learning.skills import configure_personalized_skills_root
from pynchy.host.orchestrator.concurrency import GroupQueue
from pynchy.workspace.api import (
    WorkspaceProfile,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEST_GROUP = WorkspaceProfile(
    jid="test@g.us",
    name="Test Group",
    folder="test-group",
    trigger="@pynchy",
    added_at="2024-01-01T00:00:00.000Z",
)

_CR_CREDS = "pynchy.host.container_manager.credentials"
_CR_ORCH = "pynchy.host.container_manager.orchestrator"
_GATEWAY = "pynchy.host.container_manager.gateway"


class _MockGateway(BuiltinGateway):
    """Lightweight stand-in for ``gateway.Gateway`` in credential tests.

    Subclasses the real ``BuiltinGateway`` (without calling its ``__init__``)
    so it satisfies the ``LiteLLMGateway | BuiltinGateway | None`` isinstance
    check without pulling in real gateway startup behavior.
    """

    def __init__(
        self,
        providers: set[str] | None = None,
        *,
        base_url: str = "http://host.docker.internal:4010",
    ) -> None:
        self.key = "gw-test-key"
        self._providers = providers or set()
        self._base_url = base_url

    @property
    def base_url(self) -> str:
        return self._base_url

    def has_provider(self, name: str) -> bool:
        return name in self._providers


class _AgentRunnerDeps:
    """Contract-complete runner dependencies for focused orchestration tests."""

    def __init__(self, sessions: dict[str, str] | None = None) -> None:
        self.sessions = sessions or {}
        self.session_cleared: set[str] = set()
        self.workspaces: dict[str, WorkspaceProfile] = {}
        self.queue = MagicMock(spec=GroupQueue)
        self.plugin_manager = None
        self.agent_execution_runtime = _agent_runtime(make_settings())
        self.container_agent_operations = make_container_agent_operations()
        self.host_runtime_operations = make_host_runtime_operations()
        self.refresh_personalized_agent_skills = MagicMock()
        self.admin_repo_notices = MagicMock(return_value=[])

    async def get_available_groups(self) -> list[dict[str, Any]]:
        return []

    async def broadcast_agent_input(
        self,
        chat_jid: str,
        messages: list[dict[str, Any]],
        *,
        source: str = "user",
    ) -> None:
        return None

    async def broadcast_host_message(self, chat_jid: str, text: str) -> None:
        return None


_SETTINGS_MODULES = [
    "pynchy.host.orchestrator.workspace_config",
]

_test_settings: ContextVar[Any | None] = ContextVar("test_settings", default=None)


def build_volume_mounts(group, **kwargs):
    settings = _test_settings.get()
    if settings is None:
        raise RuntimeError("build_volume_mounts requires _patch_settings")
    repo_ctx = kwargs.get("repo_ctx")
    worktree_path = kwargs.get("worktree_path")
    if isinstance(repo_ctx, RepoContext) and isinstance(worktree_path, Path):
        kwargs["repo_ctx"] = RepoMount(
            slug=repo_ctx.slug,
            root=repo_ctx.root,
            worktree_path=worktree_path,
        )
    repo_mounts = kwargs.get("repo_mounts")
    if repo_mounts is not None:
        kwargs["repo_mounts"] = [
            RepoMount(slug=repo.slug, root=repo.root, worktree_path=worktree)
            for repo, worktree in repo_mounts
        ]
    return _build_volume_mounts(
        group,
        groups_dir=settings.groups_dir,
        data_dir=settings.data_dir,
        project_root=settings.project_root,
        mount_allowlist_path=settings.mount_allowlist_path,
        blocked_mount_patterns=tuple(settings.security.blocked_patterns),
        **kwargs,
    )


def _agent_runtime(settings: object) -> AgentExecutionRuntime:
    return AgentExecutionRuntime(
        project_root=settings.project_root,
        groups_dir=settings.groups_dir,
        data_dir=settings.data_dir,
        mount_allowlist_path=settings.mount_allowlist_path,
        blocked_mount_patterns=tuple(settings.security.blocked_patterns),
        agent_image=settings.container.image,
        agent_memory_mb=settings.container.memory_mb,
        container_timeout=settings.container_timeout,
        default_core=settings.agent.default_core,
        idle_timeout=settings.idle_timeout,
        model=settings.agent.model,
        model_reasoning_effort=settings.agent.model_reasoning_effort,
    )


def _settings_overrides(
    *,
    tmp_path: Path | None,
    learning: LearningConfig | None,
    profiles: dict[str, ProfileConfig] | None,
    workspaces: dict[str, WorkspaceConfig] | None,
    container_timeout: float | None,
    idle_timeout: float | None,
) -> dict[str, object]:
    overrides: dict[str, object] = {
        "gateway": GatewayConfig(),
        "learning": learning or LearningConfig(),
        "profiles": profiles or {},
        "workspaces": workspaces or {},
    }
    if tmp_path is not None:
        overrides.update(
            project_root=tmp_path,
            groups_dir=tmp_path / "groups",
            data_dir=tmp_path / "data",
        )
    if container_timeout is not None:
        overrides["container_timeout"] = container_timeout
    if idle_timeout is not None:
        overrides["idle_timeout"] = idle_timeout
    return overrides


def _apply_secret_overrides(
    settings,
    secret_overrides: dict[str, str] | None,
) -> None:
    if not secret_overrides:
        return
    for key, value in secret_overrides.items():
        setattr(settings.secrets, key, SecretStr(value))


def _profile_workspace(
    profile_name: str = "test-profile",
    *,
    skills: list[str] | None = None,
    model: str | None = None,
):
    profile = ProfileConfig(skills=skills or [], model=model)
    return {profile_name: profile}, WorkspaceConfig(profiles=[profile_name])


@contextlib.contextmanager
def _patch_settings(
    tmp_path: Path | None = None,
    *,
    core: str | None = None,
    container_timeout: float | None = None,
    idle_timeout: float | None = None,
    learning: LearningConfig | None = None,
    profiles: dict[str, ProfileConfig] | None = None,
    workspaces: dict[str, WorkspaceConfig] | None = None,
    secret_overrides: dict[str, str] | None = None,
):
    """Patch get_settings() across all container_runner submodules."""
    if tmp_path is not None:
        prompt_root = tmp_path / "data/defaults/prompts"
        (prompt_root / "souls").mkdir(parents=True, exist_ok=True)
        (prompt_root / "executors").mkdir(exist_ok=True)
        (prompt_root / "souls/default.md").write_text("Test soul.", encoding="utf-8")
        (prompt_root / "executors/default.md").write_text(
            "Test executor.",
            encoding="utf-8",
        )
    overrides = _settings_overrides(
        tmp_path=tmp_path,
        learning=learning,
        profiles=profiles,
        workspaces=workspaces,
        container_timeout=container_timeout,
        idle_timeout=idle_timeout,
    )
    s = make_settings(**overrides)
    if core is not None:
        s.agent.default_core = core
    _apply_secret_overrides(s, secret_overrides)
    configure_personalized_skills_root(s.project_root)
    configure_learning_paths_for(s)
    configure_skill_activation_for(s)
    token = _test_settings.set(s)
    try:
        with contextlib.ExitStack() as stack:
            for mod in _SETTINGS_MODULES:
                stack.enter_context(patch(f"{mod}.get_settings", return_value=s))
            yield s
    finally:
        _test_settings.reset(token)


class FakeProcess(asyncio.subprocess.Process):
    """Simulates asyncio.subprocess.Process for testing.

    No stdin — the host writes initial input via IPC files, not a pipe.
    """

    def __init__(self) -> None:
        self.stdin = None  # stdin=DEVNULL → no pipe
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self._returncode: int | None = None
        self._wait_event = asyncio.Event()
        self.pid = 12345
        self._killed = False

    def emit_stderr(self, data: bytes) -> None:
        self.stderr.feed_data(data)

    def close(self, code: int = 0) -> None:
        """Simulate process exit."""
        self._returncode = code
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._wait_event.set()

    async def wait(self) -> int:
        await self._wait_event.wait()
        return self._returncode  # type: ignore[return-value]

    def kill(self) -> None:
        self._killed = True

    @property
    def returncode(self) -> int | None:
        return self._returncode


class KillableHangingProcess(asyncio.subprocess.Process):
    """Subprocess fake that blocks until kill and can then be reaped."""

    def __init__(self, *, timeout_first_wait: bool = False) -> None:
        self.killed = False
        self.wait_calls = 0
        self._returncode: int | None = None
        self._timeout_first_wait = timeout_first_wait
        self._exited = asyncio.Event()

    async def wait(self) -> int:
        self.wait_calls += 1
        if self._timeout_first_wait and self.wait_calls == 1:
            raise TimeoutError
        await self._exited.wait()
        return -9

    def kill(self) -> None:
        self.killed = True
        self._returncode = -9
        self._exited.set()

    @property
    def returncode(self) -> int | None:
        return self._returncode


class DelayedExitProcess(asyncio.subprocess.Process):
    """Killed child that exits only after its bounded waits have elapsed."""

    def __init__(self) -> None:
        self.killed = False
        self.wait_calls = 0
        self._returncode: int | None = None
        self._exited = asyncio.Event()

    async def wait(self) -> int:
        self.wait_calls += 1
        if self.wait_calls <= 2:
            raise TimeoutError
        await self._exited.wait()
        return -9

    def kill(self) -> None:
        self.killed = True

    def release(self) -> None:
        self._returncode = -9
        self._exited.set()

    @property
    def returncode(self) -> int | None:
        return self._returncode


class CompletedProcess:
    """Minimal subprocess fake for commands that finish via communicate()."""

    def __init__(self, stdout: bytes = b"") -> None:
        self.stdout = stdout

    async def communicate(self) -> tuple[bytes, bytes]:
        return self.stdout, b""


def _parsed_output_with_all_fields() -> ContainerOutput:
    return parse_container_output(
        json.dumps(
            {
                "status": "success",
                "result": "done",
                "new_session_id": "s1",
                "type": "tool_use",
                "thinking": "Let me think...",
                "tool_name": "Read",
                "tool_input": {"file_path": "/test.py"},
                "text": "some text",
                "system_subtype": "compact",
                "system_data": {"key": "val"},
                "tool_result_id": "tr-1",
                "tool_result_content": "file contents",
                "tool_result_is_error": False,
                "result_metadata": {"duration_ms": 1234},
            }
        )
    )


async def create_session(
    group_folder, container_name, proc, *, data_dir, idle_timeout, invocation_ts=0.0
):
    """Start a real session around a supplied process instead of spawning a container."""
    group = replace(TEST_GROUP, folder=group_folder)
    runtime = replace(_agent_runtime(make_settings()), data_dir=data_dir, idle_timeout=idle_timeout)
    input_data = ContainerInput(
        messages=[],
        group_folder=group_folder,
        chat_jid=group.jid,
        is_admin=False,
        invocation_ts=invocation_ts,
    )
    with (
        patch(
            "pynchy.host.container_manager.session.stable_container_name",
            return_value=container_name,
        ),
        patch(
            "pynchy.host.container_manager.session._spawn_container",
            new=AsyncMock(return_value=(proc, ())),
        ),
        patch("pynchy.host.container_manager.session.docker_rm_force", new=AsyncMock()),
    ):
        session, _failures = await start_session(group, input_data, runtime)
        return session
