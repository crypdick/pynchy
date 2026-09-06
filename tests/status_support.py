"""Tests for the operational status collector and /status endpoint.

All subsystem behaviour is exercised through the public ``collect_status()``
entry point (and the ``/status`` HTTP endpoint), asserting on the observable
status dict rather than importing the private per-section collectors.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock, patch

from pynchy.canaries.api import declared_canary_scenarios
from pynchy.host.orchestrator.http_control import ControlPlaneRuntime, RequestRateLimiter
from pynchy.host.orchestrator.http_server import HttpDeployOperations
from pynchy.host.orchestrator.status import GitStatusOperations

if TYPE_CHECKING:
    from pynchy.scheduling.api import (
        ScheduledTask,
    )

_S = "pynchy.host.orchestrator.status"

_EMPTY_STATS = {
    "total_inbound": 0,
    "total_outbound": 0,
    "last_received_at": None,
    "last_sent_at": None,
    "pending_deliveries": 0,
}


def _runtime() -> ControlPlaneRuntime:
    return ControlPlaneRuntime(
        bind_host="127.0.0.1",
        port=8484,
        unix_socket=None,
        public_bind=False,
        remote_auth_required=False,
        allow_remote_deploy=False,
        auth_token=None,
        rate_limiter=RequestRateLimiter(request_limit=20, window_seconds=60),
        audit_security_event=AsyncMock(),
    )


def _inert_orchestration_states(tasks, jobs, _address, _namespace):
    """Return Temporal-shaped state without opening a Temporal connection."""
    return {
        **{
            ("task", task.id): {
                "source": "temporal",
                "state": "scheduled",
                "next_run": None,
                "schedule_id": None,
                "workflow_id": None,
                "error": None,
            }
            for task in tasks
        },
        **{
            ("host_job", job.id): {
                "source": "temporal",
                "state": "scheduled",
                "next_run": None,
                "schedule_id": None,
                "workflow_id": None,
                "error": None,
            }
            for job in jobs
        },
    }


@contextlib.contextmanager
def _inert_status():
    """Neutralise every I/O-bound status collector via its public deps.

    Patches the external functions ``collect_status`` fans out to (git, DB,
    docker, HTTP) so a test can drive the single public entry point while only
    the section it cares about does real work. Tests layer their own ``patch``
    calls on top; the innermost patch wins.
    """
    with contextlib.ExitStack() as stack:

        def p(name: str, **kwargs: Any) -> None:
            stack.enter_context(patch(f"{_S}.{name}", **kwargs))

        p("get_router_state", new_callable=AsyncMock, return_value=None)
        p("get_messaging_stats", new_callable=AsyncMock, return_value=dict(_EMPTY_STATS))
        p(
            "collect_capability_status",
            new_callable=AsyncMock,
            return_value={"summary": {}, "workspaces": []},
        )
        p("get_all_tasks", new_callable=AsyncMock, return_value=[])
        p("get_task_run_logs", new_callable=AsyncMock, return_value=[])
        p("get_all_host_jobs", new_callable=AsyncMock, return_value=[])
        p(
            "_get_temporal_orchestration_states",
            new_callable=AsyncMock,
            side_effect=_inert_orchestration_states,
        )
        p(
            "get_temporal_scheduler_status",
            create=True,
            return_value={
                "worker_running": False,
                "last_workflow_id": None,
                "last_task_id": None,
                "last_result": None,
                "last_started_at": None,
                "last_completed_at": None,
                "last_error": None,
            },
        )
        p(
            "_check_temporal_cluster_health",
            create=True,
            new_callable=AsyncMock,
            return_value={"healthy": None, "error": None},
        )
        stack.enter_context(patch("aiohttp.ClientSession", side_effect=Exception("skip")))
        yield


class MockStatusDeps:
    """Mock implementation of StatusDeps for testing."""

    def __init__(
        self,
        *,
        shutting_down: bool = False,
        channels: dict[str, bool] | None = None,
        queue: dict[str, Any] | None = None,
        gateway: dict[str, Any] | None = None,
        active_sessions: int = 0,
        workspace_count: int = 0,
        speech_synthesizer: Any | None = None,
        repo_slugs: tuple[str, ...] = (),
        temporal_address: str = "localhost:7233",
        temporal_namespace: str = "default",
        temporal_task_queue: str = "pynchy-scheduler",
    ):
        self._shutting_down = shutting_down
        self._channels = channels or {"whatsapp": True}
        self._queue = queue or {
            "active_containers": 1,
            "max_concurrent": 10,
            "groups_waiting": 0,
            "per_group": {},
        }
        self._gateway = gateway or {"mode": "litellm", "port": 4000, "key": "sk-test"}
        self.capability_status_operations = Mock()
        self.get_container_state = AsyncMock(return_value="not_found")
        self._active_sessions = active_sessions
        self._workspace_count = workspace_count
        self._speech_synthesizer = speech_synthesizer
        self.repo_slugs = repo_slugs
        self.temporal_address = temporal_address
        self.temporal_namespace = temporal_namespace
        self.temporal_task_queue = temporal_task_queue
        self.git_status = GitStatusOperations(
            get_repo_context=Mock(return_value=None),
            get_head_sha=Mock(return_value="0000000"),
            is_repo_dirty=Mock(return_value=False),
            count_unpushed_commits=Mock(return_value=0),
            get_head_commit_message=Mock(return_value=""),
            detect_main_branch=Mock(return_value="main"),
            run_git=Mock(),
        )
        self.get_canary_report = AsyncMock(return_value={"summary": {"unresolved_regressions": 0}})

    def is_shutting_down(self) -> bool:
        return self._shutting_down

    def get_channel_status(self) -> dict[str, bool]:
        return self._channels

    def get_connection_status(self) -> dict[str, bool]:
        return {}

    def get_queue_snapshot(self) -> dict[str, Any]:
        return self._queue

    def get_gateway_info(self) -> dict[str, Any]:
        return self._gateway

    def get_active_sessions_count(self) -> int:
        return self._active_sessions

    def get_workspace_count(self) -> int:
        return self._workspace_count

    def get_speech_synthesizer(self) -> Any | None:
        return self._speech_synthesizer


class MockHttpDeps:
    """Inert HTTP dependencies for exercising route registration."""

    broadcast_synthetic_user_input = AsyncMock()
    data_dir = None
    project_root = None
    capability_status_operations = Mock()
    deploy_operations = HttpDeployOperations(
        get_head_sha=Mock(return_value="head-sha"),
        push_local_commits=Mock(return_value=True),
        run_git=Mock(),
        files_changed_between=Mock(return_value=False),
        get_deploy_config_hash=Mock(return_value="config-hash"),
        get_head_commit_message=Mock(return_value="commit"),
        is_repo_dirty=Mock(return_value=False),
        start_deploy_workflow=AsyncMock(),
    )
    get_canary_report = AsyncMock(
        return_value={"summary": {"declared_scenarios": len(declared_canary_scenarios())}}
    )
    canary_run_to_dict = staticmethod(lambda _run: {})
    work_item_execution_to_dict = staticmethod(lambda _execution: {})

    async def broadcast_host_message(self, _jid: str, _text: str) -> None:
        return None

    def get_workspace(self, _folder: str) -> None:
        return None

    def dispatch_scheduled_task(self, _task: ScheduledTask) -> None:
        return None

    def admin_chat_jid(self) -> str:
        return "admin@g.us"
