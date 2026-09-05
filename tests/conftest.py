"""Shared test fixtures for Pynchy."""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import BaseModel, SecretStr

import pynchy.config.api as config_api
import pynchy.host.orchestrator.api as orchestrator_api
import pynchy.host.orchestrator.workspace_config as workspace_config
from pynchy.actions.api import ACTION_SPECS, assess_hermetic_coverage
from pynchy.canaries.api import CanaryRuntime, configure_canary_runtime
from pynchy.config.api import (
    JobConfig,
    Settings,
    WorkspaceConfig,
    access,
    apply_tool_access,
    mutate_config_toml,
    parse_chat_ref,
    repository_settings_sources,
    resolve_tool_access,
    tool_process_environment,
    validate_settings_mapping,
)
from pynchy.config.api import (
    reset_settings as reset_config_settings,
)
from pynchy.host.container_manager.api import AgentHomeMounts, RepoMountResolution
from pynchy.host.container_manager.credentials import configure_workspace_environment
from pynchy.host.container_manager.gateway import configure_gateway_runtime
from pynchy.host.container_manager.ipc.bootstrap import register_builtin_handlers
from pynchy.host.container_manager.ipc.handlers_approval import configure_approval_runtime
from pynchy.host.container_manager.ipc.handlers_lifecycle import (
    LifecycleRuntime,
    configure_lifecycle_runtime,
)
from pynchy.host.container_manager.ipc.handlers_managed_feature import (
    ManagedFeatureRuntime,
    configure_managed_feature_runtime,
)
from pynchy.host.container_manager.ipc.handlers_service import configure_service_runtime
from pynchy.host.container_manager.ipc.write import configure_ipc_base_dir
from pynchy.host.container_manager.mcp.manager import configure_mcp_manager_runtime
from pynchy.host.container_manager.mcp.resolution import configure_mcp_resolution_runtime
from pynchy.host.container_manager.mounts import MountOperations, configure_mount_operations
from pynchy.host.container_manager.orchestrator import configure_container_spawn_runtime
from pynchy.host.container_manager.process import configure_container_process_runtime
from pynchy.host.container_manager.security.approval import configure_approval_state_root
from pynchy.host.container_manager.security.audit import configure_security_audit_storage
from pynchy.host.container_manager.security.cop import (
    CopVerdict,
)
from pynchy.host.container_manager.security.gate import configure_security_resolution
from pynchy.host.git_ops import repo
from pynchy.host.git_ops.api import (
    GitSyncRuntime,
    check_local_head_drift,
    check_origin_drift,
    configure_git_sync_runtime,
    detect_main_branch,
    find_pynchy_repo_ctx,
    get_deploy_config_hash,
    get_head_commit_message,
    get_head_sha,
    get_local_head_sha,
    get_repo_context,
    git_env_with_token,
    host_create_pr_from_managed_feature,
    host_create_pr_from_worktree,
    host_get_origin_main_sha,
    host_notify_worktree_updates,
    host_rebase_managed_feature,
    host_update_main_result,
    is_repo_dirty,
    last_notified_sha,
    needs_deploy,
    probe_origin_main_sha,
    prune_stale_worktree_venvs,
    read_managed_feature_patch,
    redact_git_diagnostic,
    resolve_managed_feature_publication,
    run_git,
)
from pynchy.host.git_ops.utils import configure_git_default_cwd
from pynchy.host.learning.api import (
    LearningPathsRuntime,
    configure_learning_paths_runtime,
    prepare_agent_homes,
    resolve_learning_paths,
)
from pynchy.host.learning.skill_activation import (
    SkillActivationRuntime,
    configure_skill_activation_runtime,
)
from pynchy.host.learning.skills import configure_personalized_skills_root
from pynchy.host.orchestrator.job_sources import (
    PluginJobsRuntime,
    configure_plugin_jobs_runtime,
)
from pynchy.host.orchestrator.messaging.pending_questions import (
    configure_pending_questions_ipc_base_dir,
)
from pynchy.host.orchestrator.messaging.reconciler import configure_allowed_message_filter
from pynchy.host.orchestrator.startup_handler import (
    StartupRuntime,
    configure_startup_runtime,
)
from pynchy.host.orchestrator.temporal.git_sync import (
    TemporalGitSyncRuntime,
    configure_temporal_git_sync_runtime,
)
from pynchy.host.orchestrator.workspace_placement import configure_workspace_placement
from pynchy.host.orchestrator.workspace_registration import (
    configure_workspace_registration_runtime,
    workspace_security,
)
from pynchy.host.orchestrator.workspace_threads import configure_workspace_threads_runtime
from pynchy.plugins.api import (
    NewMessage,
)
from pynchy.plugins.integrations.api import get_active_matrix_route
from pynchy.state import (
    close_test_database,
    get_latest_canary_runs,
    get_recent_canary_runs,
    get_unresolved_canary_regressions,
    init_test_database,
    prune_messages_by_sender,
    record_canary_run,
    store_message_direct,
)
from pynchy.state.api import (
    get_conversation,
    get_in_flight_turn_for_group,
    get_unfinished_work_item_execution,
    get_work_item_execution_for_task,
    get_work_item_execution_for_turn,
)
from pynchy.workspace.api import WorkspaceProfile
from tests.conftest_helpers import (
    NullChannel,
    NullIpcDeps,
    make_command_matcher,
    make_container_agent_operations,
    make_container_runtime_operations,
    make_host_action_catalog,
    make_host_runtime_operations,
    make_settings,
)
from tests.conftest_linear import configure_linear_accounts_for


def pytest_configure() -> None:  # noqa: V103
    pytest.register_assert_rewrite(
        "tests.action_intents_support",
        "tests.app_integration_support",
        "tests.conversation_routing_support",
        "tests.git_policy_support",
        "tests.group_queue_support",
        "tests.ipc_auth_support",
        "tests.linear_decision_inbox_support",
        "tests.linear_webhooks_support",
        "tests.linear_work_items_support",
        "tests.mcp_proxy_support",
        "tests.state_support",
        "tests.task_scheduler_support",
        "tests.temporal_scheduler_support",
        "tests.webhook_lifecycle_support",
    )


register_builtin_handlers()


def configure_workspace_placement_for(settings: Settings) -> None:
    """Wire workspace placement to one test's resolved settings."""

    def missing_workspace_profile(folder, control_parent):
        config = settings.workspace_config(folder)
        resolved = settings.resolved_workspace_config(folder)
        if config is None or resolved is None:
            return None
        return WorkspaceProfile(
            jid=control_parent.jid,
            name=folder.replace("-", " ").title(),
            folder=folder,
            trigger=control_parent.trigger,
            container_config=control_parent.container_config,
            security=workspace_security(config, resolved),
            is_admin=resolved.is_admin,
            added_at=datetime.now(UTC).isoformat(),
        )

    configure_workspace_placement(
        workspace_parent=settings.workspace_parent,
        missing_workspace_profile=missing_workspace_profile,
    )


def configure_skill_activation_for(settings: Settings) -> None:
    """Wire skill activation to one test's resolved settings."""

    def workspace_skill_selection(folder: str):
        resolved = settings.resolved_workspace_config(folder)
        if resolved is None:
            return None
        return (
            tuple(resolved.skills),
            tuple(resolved.denied_skills),
            tuple(resolved.tools),
        )

    configure_skill_activation_runtime(
        SkillActivationRuntime(
            project_root=settings.project_root,
            sessions_root=settings.data_dir / "sessions",
            tool_skills={
                name: tuple(getattr(tool, "skills", ())) for name, tool in settings.tools.items()
            },
            resolve_workspace_skill_selection=workspace_skill_selection,
            resolve_learning_paths=lambda folder, profile: resolve_learning_paths(
                folder, profile_override=profile
            ),
        )
    )


def configure_learning_paths_for(settings: Settings) -> None:
    """Wire learning-path resolution to one test's resolved settings."""

    def profile_for_workspace(folder: str) -> str | None:
        workspace = settings.workspaces.get(folder)
        return workspace.profiles[0] if workspace is not None and workspace.profiles else None

    configure_learning_paths_runtime(
        LearningPathsRuntime(
            enabled=settings.learning.enabled,
            vault_root=settings.learning.obsidian.vault_root,
            vault_mount_path=settings.learning.obsidian.mount_path,
            default_profile_root=settings.learning.obsidian.default_profile_root,
            memory_dir_name=settings.learning.obsidian.memory_dir_name,
            profile_for_workspace=profile_for_workspace,
        )
    )


@pytest.fixture(autouse=True)
def _clean_host_mutation_cop():
    """Give non-security tests a hermetic, successful Cop boundary."""
    with (
        patch(
            "pynchy.host.container_manager.security.cop_gate.inspect_outbound",
            new_callable=AsyncMock,
            return_value=CopVerdict(flagged=False),
        ),
    ):
        yield


__all__ = [
    "NullChannel",
    "NullIpcDeps",
    "configure_linear_accounts_for",
    "init_test_database",
    "make_command_matcher",
    "make_container_agent_operations",
    "make_container_runtime_operations",
    "make_host_action_catalog",
    "make_host_runtime_operations",
    "make_settings",
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


def pytest_addoption(parser):  # noqa: V103
    """Register the opt-in dynamic action-coverage collection gate."""
    parser.addoption(
        "--action-coverage",
        action="store_true",
        default=False,
        help="require every registered action to have a pytest.mark.action test",
    )


def pytest_collection_finish(session):  # noqa: V103
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
        configure_ipc_base_dir(safe.data_dir / "ipc")
        configure_approval_state_root(safe.data_dir / "approvals")
        configure_security_audit_storage(
            store_security_audit=store_message_direct,
            prune_security_audit=prune_messages_by_sender,
        )
        configure_canary_runtime(
            CanaryRuntime(
                record_run=record_canary_run,
                latest_runs=get_latest_canary_runs,
                recent_runs=lambda limit: get_recent_canary_runs(limit=limit),
                unresolved_regressions=get_unresolved_canary_regressions,
                code_revision=lambda: "test-revision",
            )
        )
        configure_pending_questions_ipc_base_dir(safe.data_dir / "ipc")
        configure_personalized_skills_root(safe.project_root)
        configure_git_default_cwd(safe.project_root)
        configure_git_sync_runtime(
            GitSyncRuntime(
                project_root=safe.project_root,
                repo_slugs=tuple(safe.repos.overrides),
                get_restart_hash=lambda: "test-config",
            )
        )
        configure_allowed_message_filter(access.filter_allowed_messages)
        configure_workspace_placement_for(safe)
        configure_learning_paths_for(safe)
        configure_skill_activation_for(safe)
        configure_linear_accounts_for(safe)

        def mount_agent_homes(folder: str, plugins: object | None) -> AgentHomeMounts:
            homes = prepare_agent_homes(folder, plugins)
            return AgentHomeMounts(
                claude_home=homes.claude_home,
                codex_home=homes.codex_home,
                vault_mount_root=homes.learning_paths.vault_root if homes.learning_paths else None,
                vault_mount_path=(
                    homes.learning_paths.vault_mount_path
                    if homes.learning_paths is not None
                    else None
                ),
            )

        configure_mount_operations(
            MountOperations(
                prepare_agent_homes=mount_agent_homes,
                repo_container_path=lambda slug: f"/home/agent/src/{slug}",
                runtime_name=lambda: "docker",
            )
        )
        configure_workspace_environment(lambda *, is_admin, group_folder: {})
        configure_container_spawn_runtime(
            container_cli="docker",
            ensure_agent_image=lambda **_kwargs: None,
            resolve_repo_mounts=lambda _folder, _repos: RepoMountResolution(),
        )
        configure_container_process_runtime(
            container_cli="docker",
            is_apple_runtime=False,
            container_is_running=lambda _name: False,
        )

        def settings_source() -> Settings:
            return config_api.get_settings()

        def read_prompts(names: list[str]) -> str | None:
            if not names:
                return None
            settings = settings_source()
            return config_api.load_prompt_catalog(
                default_prompts=settings.project_root / "data/defaults/prompts",
                personalized_prompts=settings.project_root / "data/personalization/prompts",
            ).compose(names)

        def resolve_workspace_config(folder: str, settings: Settings | None = None):
            return workspace_config.load_resolved_config(folder, settings=settings)

        def resolve_repositories(folder: str, _turn_id: str | None = None):
            return repo.resolve_repos_for_group(folder)

        repo.configure_repo_runtime(
            get_settings=settings_source,
            resolve_workspace_config=resolve_workspace_config,
        )
        workspace_config.configure_workspace_config_runtime(
            workspace_config.WorkspaceConfigRuntime(
                get_settings=settings_source,
                read_prompts=read_prompts,
                parse_workspace_config=WorkspaceConfig.model_validate,
                apply_tool_access=apply_tool_access,
                resolve_tool_access=resolve_tool_access,
                mutate_config_toml=mutate_config_toml,
                validate_settings_mapping=validate_settings_mapping,
                reset_settings=reset_config_settings,
            )
        )
        configure_workspace_registration_runtime(parse_chat_reference=parse_chat_ref)
        configure_workspace_threads_runtime(settings=settings_source)
        configure_plugin_jobs_runtime(
            PluginJobsRuntime(
                get_settings=settings_source,
                parse_job=JobConfig.model_validate,
            )
        )
        configure_startup_runtime(
            StartupRuntime(
                get_settings=settings_source,
                head_commit_message=get_head_commit_message,
                head_sha=get_head_sha,
                repo_dirty=is_repo_dirty,
                git=run_git,
            )
        )
        configure_temporal_git_sync_runtime(
            TemporalGitSyncRuntime(
                get_settings=settings_source,
                check_local_head_drift=check_local_head_drift,
                check_origin_drift=check_origin_drift,
                find_pynchy_repo_ctx=find_pynchy_repo_ctx,
                get_deploy_config_hash=get_deploy_config_hash,
                get_local_head_sha=get_local_head_sha,
                get_repo_context=get_repo_context,
                git_env_with_token=git_env_with_token,
                host_get_origin_main_sha=host_get_origin_main_sha,
                host_notify_worktree_updates=host_notify_worktree_updates,
                host_update_main_result=host_update_main_result,
                last_notified_sha=last_notified_sha,
                needs_deploy=needs_deploy,
                probe_origin_main_sha=probe_origin_main_sha,
                prune_stale_worktree_venvs=prune_stale_worktree_venvs,
                refresh_host_config=AsyncMock(
                    side_effect=lambda config_hash: orchestrator_api.ConfigRefreshResult(
                        orchestrator_api.ConfigRefreshStatus.UNCHANGED,
                        config_hash,
                    )
                ),
            )
        )
        configure_gateway_runtime(is_apple_container=False, get_settings=settings_source)
        configure_mcp_resolution_runtime(
            apply_tool_access=apply_tool_access,
            tool_process_environment=tool_process_environment,
        )
        configure_mcp_manager_runtime(
            static_workspace_folder=orchestrator_api.static_workspace_folder,
            load_resolved_workspace_config=resolve_workspace_config,
        )
        configure_security_resolution(
            get_settings=settings_source,
            resolve_workspace_config=resolve_workspace_config,
        )
        configure_approval_runtime(get_settings=settings_source)
        configure_service_runtime(
            get_settings=settings_source,
            resolve_workspace_config=resolve_workspace_config,
            active_matrix_route=get_active_matrix_route,
        )
        configure_lifecycle_runtime(
            LifecycleRuntime(
                settings=settings_source,
                resolve_publication_repos=resolve_repositories,
                get_work_item_execution_for_turn=get_work_item_execution_for_turn,
                get_work_item_execution_for_task=get_work_item_execution_for_task,
                get_unfinished_work_item_execution=get_unfinished_work_item_execution,
                get_current_turn=get_in_flight_turn_for_group,
                get_conversation=get_conversation,
                attach_work_item_pull_request=AsyncMock(return_value=None),
                detect_main_branch=detect_main_branch,
                host_create_pr_from_worktree=host_create_pr_from_worktree,
                redact_git_diagnostic=redact_git_diagnostic,
                run_git=run_git,
            )
        )
        configure_managed_feature_runtime(
            ManagedFeatureRuntime(
                settings=settings_source,
                resolve_repos_for_group=resolve_repositories,
                resolve_managed_feature_publication=resolve_managed_feature_publication,
                read_managed_feature_patch=read_managed_feature_patch,
                host_create_pr_from_managed_feature=host_create_pr_from_managed_feature,
                host_rebase_managed_feature=host_rebase_managed_feature,
                redact_git_diagnostic=redact_git_diagnostic,
            )
        )
        yield


@pytest.fixture(autouse=True, scope="session")
def _reap_orphaned_test_containers(request: pytest.FixtureRequest) -> None:
    """Remove Docker resources abandoned by earlier runtime runs.

    Runs only for sessions that provision Docker, so a default unit run pays no
    cost. Resources owned by a live process are never touched, which keeps
    concurrent suites in sibling worktrees safe.
    """
    from pynchy.host.container_manager import (  # noqa: PLC0415 - keep unit-run import surface small.
        docker,
        reaper,
    )

    marker_names = (marker.name for item in request.session.items for marker in item.iter_markers())
    if not reaper.wants_reaping(marker_names) or not docker.docker_available():
        return
    reaped = reaper.reap_now()
    if reaped:
        logging.getLogger(__name__).info(
            "Reaped %d orphaned test container(s): %s", len(reaped), ", ".join(reaped)
        )


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
