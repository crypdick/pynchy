"""Tests for HTTP server endpoints and utilities."""

from __future__ import annotations

# allow: file-length -- HTTP endpoint contracts share one server fixture set.
import json
import subprocess  # noqa: S404 - test helpers mock subprocess behavior and exceptions
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestClient, TestServer

from pynchy.deployments import (
    DeployClaim,
    DeployClaimStatus,
)
from pynchy.host.git_ops.api import (
    get_head_commit_message,
    get_head_sha,
    is_repo_dirty,
    push_local_commits,
)
from pynchy.host.orchestrator.http_control import ControlPlaneRuntime, RequestRateLimiter
from pynchy.host.orchestrator.http_server import (
    ControlPlaneReadiness,
    HttpDeployOperations,
    activate_http_server,
    create_http_app,
    prepare_http_server,
    publish_http_server,
)

if TYPE_CHECKING:
    from pynchy.scheduling.api import ScheduledTask
    from pynchy.workspace.api import WorkspaceProfile


def _cp(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    """Fake a run_git() return value with a real CompletedProcess."""
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


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


def _deploy_operations() -> HttpDeployOperations:
    return HttpDeployOperations(
        get_head_sha=Mock(return_value="head-sha"),
        push_local_commits=Mock(return_value=True),
        run_git=Mock(return_value=_cp(stdout="No local changes")),
        files_changed_between=Mock(return_value=False),
        get_deploy_config_hash=Mock(return_value="config-hash"),
        get_head_commit_message=Mock(return_value="commit"),
        is_repo_dirty=Mock(return_value=False),
        start_deploy_workflow=AsyncMock(return_value=DeployClaim(DeployClaimStatus.CLAIMED)),
    )


# ---------------------------------------------------------------------------
# Git utility tests
# ---------------------------------------------------------------------------


def test_get_head_sha_success():
    """get_head_sha returns SHA when git succeeds."""
    with patch("pynchy.host.git_ops.utils.run_git") as mock_run:
        mock_run.return_value = _cp(
            returncode=0,
            stdout="head-sha-001\n",
        )
        assert get_head_sha() == "head-sha-001"


def test_get_head_sha_failure():
    """get_head_sha returns 'unknown' when git fails."""
    with patch("pynchy.host.git_ops.utils.run_git") as mock_run:
        mock_run.return_value = _cp(returncode=1, stdout="")
        assert get_head_sha() == "unknown"


def test_is_repo_dirty_clean():
    """is_repo_dirty returns False when no uncommitted changes."""
    with patch("pynchy.host.git_ops.utils.run_git") as mock_run:
        mock_run.return_value = _cp(returncode=0, stdout="")
        assert is_repo_dirty() is False


def test_is_repo_dirty_has_changes():
    """is_repo_dirty returns True when uncommitted changes exist."""
    with patch("pynchy.host.git_ops.utils.run_git") as mock_run:
        mock_run.return_value = _cp(
            returncode=0,
            stdout=" M src/pynchy/app.py\n?? newfile.txt\n",
        )
        assert is_repo_dirty() is True


def test_is_repo_dirty_failure():
    """is_repo_dirty returns False when git fails."""
    with patch("pynchy.host.git_ops.utils.run_git") as mock_run:
        mock_run.return_value = _cp(returncode=1, stdout="")
        assert is_repo_dirty() is False


def test_get_head_commit_message_success():
    """get_head_commit_message returns commit subject."""
    with patch("pynchy.host.git_ops.utils._run_git_process") as mock_run:
        mock_run.return_value = _cp(
            returncode=0,
            stdout="Add feature X\n",
        )
        assert get_head_commit_message() == "Add feature X"


def test_get_head_commit_message_truncation():
    """get_head_commit_message truncates long subjects."""
    long_msg = "A" * 80
    with patch("pynchy.host.git_ops.utils._run_git_process") as mock_run:
        mock_run.return_value = _cp(returncode=0, stdout=f"{long_msg}\n")
        result = get_head_commit_message(max_length=72)
        assert len(result) == 72
        assert result.endswith("…")


def test_get_head_commit_message_failure():
    """get_head_commit_message returns empty string on failure."""
    with patch("pynchy.host.git_ops.utils._run_git_process") as mock_run:
        mock_run.return_value = _cp(returncode=1, stdout="")
        assert not get_head_commit_message()


def test_get_head_commit_message_exception():
    """get_head_commit_message returns empty string when subprocess raises."""
    with patch("pynchy.host.git_ops.utils._run_git_process", side_effect=OSError):
        assert not get_head_commit_message()


# ---------------------------------------------------------------------------
# Push local commits tests
# ---------------------------------------------------------------------------


def test_push_local_commits_nothing_to_push():
    """push_local_commits returns True when no local commits exist."""
    with patch("pynchy.host.git_ops.utils._run_git_process") as mock_run:
        mock_run.side_effect = [
            _cp(returncode=0, stdout="refs/remotes/origin/main\n"),  # detect_main_branch
            _cp(returncode=0),  # fetch
            _cp(returncode=0, stdout="0\n"),  # rev-list
        ]
        assert push_local_commits() is True


def test_push_local_commits_success():
    """push_local_commits returns True when push succeeds."""
    with patch("pynchy.host.git_ops.utils._run_git_process") as mock_run:
        mock_run.side_effect = [
            _cp(returncode=0, stdout="refs/remotes/origin/main\n"),  # detect_main_branch
            _cp(returncode=0),  # fetch
            _cp(returncode=0, stdout="3\n"),  # rev-list (3 commits)
            _cp(returncode=0),  # rebase
            _cp(returncode=0),  # push
        ]
        assert push_local_commits() is True


def test_push_local_commits_fetch_failure():
    """push_local_commits returns False when fetch fails."""
    with patch("pynchy.host.git_ops.utils._run_git_process") as mock_run:
        mock_run.return_value = _cp(returncode=1, stderr="network error", stdout="")
        assert push_local_commits() is False


def test_push_local_commits_rebase_failure_retries_and_fails():
    """push_local_commits retries once after rebase failure, then gives up."""
    with patch("pynchy.host.git_ops.utils._run_git_process") as mock_run:
        mock_run.side_effect = [
            _cp(returncode=0, stdout="refs/remotes/origin/main\n"),  # detect_main_branch
            _cp(returncode=0),  # fetch
            _cp(returncode=0, stdout="2\n"),  # rev-list
            _cp(returncode=1, stderr="CONFLICT"),  # rebase fails (attempt 1)
            _cp(returncode=0),  # rebase --abort
            _cp(returncode=0),  # retry fetch
            _cp(returncode=1, stderr="CONFLICT"),  # rebase fails (attempt 2)
            _cp(returncode=0),  # rebase --abort
        ]
        assert push_local_commits() is False


def test_push_local_commits_rebase_retry_succeeds():
    """push_local_commits succeeds on retry when origin advanced mid-push."""
    with patch("pynchy.host.git_ops.utils._run_git_process") as mock_run:
        mock_run.side_effect = [
            _cp(returncode=0, stdout="refs/remotes/origin/main\n"),  # detect_main_branch
            _cp(returncode=0),  # fetch
            _cp(returncode=0, stdout="2\n"),  # rev-list
            _cp(returncode=1, stderr="CONFLICT"),  # rebase fails (attempt 1)
            _cp(returncode=0),  # rebase --abort
            _cp(returncode=0),  # retry fetch
            _cp(returncode=0),  # rebase succeeds (attempt 2)
            _cp(returncode=0),  # push
        ]
        assert push_local_commits() is True


def test_push_local_commits_push_failure():
    """push_local_commits returns False when push is rejected."""
    with patch("pynchy.host.git_ops.utils._run_git_process") as mock_run:
        mock_run.side_effect = [
            _cp(returncode=0, stdout="refs/remotes/origin/main\n"),  # detect_main_branch
            _cp(returncode=0),  # fetch
            _cp(returncode=0, stdout="1\n"),  # rev-list
            _cp(returncode=0),  # rebase
            _cp(returncode=1, stderr="rejected"),  # push fails
        ]
        assert push_local_commits() is False


def test_push_local_commits_skip_fetch():
    """push_local_commits skips fetch when skip_fetch=True."""
    with patch("pynchy.host.git_ops.utils._run_git_process") as mock_run:
        mock_run.side_effect = [
            _cp(returncode=0, stdout="refs/remotes/origin/main\n"),  # detect_main_branch
            _cp(returncode=0, stdout="0\n"),  # rev-list
        ]
        assert push_local_commits(skip_fetch=True) is True


def test_push_local_commits_exception():
    """push_local_commits returns False on unexpected exception."""
    with patch(
        "pynchy.host.git_ops.utils._run_git_process",
        side_effect=OSError("disk error"),
    ):
        assert push_local_commits() is False


# ---------------------------------------------------------------------------
# Deploy rollback warning tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("existing_warnings", "expected_warnings"),
    [
        (None, ["Deploy rolled back"]),
        ('["Earlier warning"]', ["Earlier warning", "Deploy rolled back"]),
        ("{invalid json}", ["Deploy rolled back"]),
    ],
)
async def test_failed_deploy_records_operator_boot_warning(
    tmp_path: Path,
    existing_warnings: str | None,
    expected_warnings: list[str],
):
    """POST /deploy preserves an actionable warning when rebase fails."""
    warnings_file = tmp_path / "boot_warnings.json"
    if existing_warnings is not None:
        warnings_file.write_text(existing_warnings)

    def failed_rebase(*args: str) -> subprocess.CompletedProcess[str]:
        if args == ("rebase", "origin/main"):
            return _cp(returncode=1, stderr="merge conflict")
        if args == ("stash",):
            return _cp(stdout="No local changes")
        return _cp()

    runtime = ControlPlaneRuntime(
        bind_host="127.0.0.1",
        port=8484,
        unix_socket=None,
        public_bind=False,
        remote_auth_required=False,
        allow_remote_deploy=True,
        auth_token=None,
        rate_limiter=RequestRateLimiter(request_limit=20, window_seconds=60),
        audit_security_event=AsyncMock(),
    )
    deps = MockHttpDeps()
    deps.deploy_operations.get_head_sha.return_value = "same-sha"
    deps.deploy_operations.push_local_commits.return_value = True
    deps.deploy_operations.run_git.side_effect = failed_rebase
    deps.deploy_operations.get_deploy_config_hash.return_value = "config"
    deps.deploy_operations.start_deploy_workflow.return_value = DeployClaim(
        DeployClaimStatus.CLAIMED
    )
    deps.deploy_operations.get_head_commit_message.return_value = "commit"
    deps.deploy_operations.is_repo_dirty.return_value = False
    deps.data_dir = tmp_path
    deps.project_root = tmp_path
    app = create_http_app(deps, runtime=runtime)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        response = await client.post("/deploy")
        assert response.status == 200
        assert (await response.json())["status"] == "restarting"
    finally:
        await client.close()

    warnings = json.loads(warnings_file.read_text())
    assert len(warnings) == len(expected_warnings)
    for actual, expected in zip(warnings, expected_warnings, strict=True):
        assert expected in actual


# ---------------------------------------------------------------------------
# HTTP endpoint tests
# ---------------------------------------------------------------------------


class MockHttpDeps:
    """Mock implementation of HttpDeps for testing."""

    def __init__(self):
        self.broadcasts: list[tuple[str, str]] = []
        self.capability_status_operations = Mock()
        self.runtime_messages: list[tuple[str, str]] = []
        self.synthetic_user_inputs: list[tuple[str, str]] = []
        self._admin_jid = "admin-1@g.us"
        self.data_dir = Path.cwd() / "data"
        self.project_root = Path.cwd()
        self.deploy_operations = _deploy_operations()
        self.get_canary_report = AsyncMock(return_value={"scenarios": []})
        self.canary_run_to_dict = Mock(return_value={})
        self.work_item_execution_to_dict = Mock(return_value={})

    async def broadcast_host_message(self, jid: str, text: str) -> None:
        self.broadcasts.append((jid, text))

    async def broadcast_synthetic_user_input(self, jid: str, content: str) -> None:
        self.synthetic_user_inputs.append((jid, content))

    def admin_chat_jid(self) -> str:
        return self._admin_jid

    async def ingest_runtime_harness_message(self, jid: str, content: str) -> None:
        self.runtime_messages.append((jid, content))

    def get_plugin_manager(self) -> object:
        return object()

    def get_workspace(self, folder: str) -> WorkspaceProfile | None:
        del folder
        return None

    def dispatch_scheduled_task(self, task: ScheduledTask) -> None:
        del task


class TestHealthEndpoint(AioHTTPTestCase):
    """Tests for /health endpoint."""

    async def get_application(self) -> web.Application:  # noqa: V105
        self.deps = MockHttpDeps()
        return create_http_app(self.deps, runtime=_runtime())

    async def test_health_returns_status_ok(self):
        """Health endpoint returns only its non-sensitive readiness contract."""
        resp = await self.client.get("/health")
        assert resp.status == 200
        assert await resp.json() == {"status": "ok"}

    async def test_health_does_not_inspect_repository_or_channel_state(self):
        """Public readiness cannot expose repository or channel details."""
        resp = await self.client.get("/health")
        assert resp.status == 200
        operations = self.deps.deploy_operations
        operations.get_head_sha.assert_not_called()
        operations.get_head_commit_message.assert_not_called()
        operations.is_repo_dirty.assert_not_called()


@pytest.mark.asyncio
async def test_http_preparation_does_not_publish_before_activation(tmp_path) -> None:
    start_sites = AsyncMock()
    with (
        patch(
            "pynchy.host.orchestrator.http_server.collect_webhook_routes",
            return_value=(),
        ),
        patch(
            "pynchy.host.orchestrator.http_server.start_control_plane_sites",
            start_sites,
        ),
    ):
        prepared = await prepare_http_server(MockHttpDeps(), runtime=_runtime())
        start_sites.assert_not_awaited()
        assert prepared.readiness.accepting_requests is False
        runner = await activate_http_server(prepared)

    start_sites.assert_awaited_once_with(prepared.runner, prepared.runtime)
    assert prepared.readiness.accepting_requests is False
    publish_http_server(prepared)
    assert prepared.readiness.accepting_requests is True
    await runner.cleanup()


@pytest.mark.asyncio
async def test_control_plane_gate_rejects_requests_until_publication() -> None:
    readiness = ControlPlaneReadiness()
    client = TestClient(
        TestServer(create_http_app(MockHttpDeps(), runtime=_runtime(), readiness=readiness))
    )
    await client.start_server()
    try:
        starting = await client.get("/health")
        assert starting.status == 503
        assert await starting.json() == {"status": "starting"}

        readiness.accepting_requests = True
        ready = await client.get("/health")
        assert ready.status == 200
    finally:
        await client.close()


async def test_runtime_harness_ingress_is_absent_from_normal_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PYNCHY_RUNTIME_HARNESS", raising=False)
    client = TestClient(TestServer(create_http_app(MockHttpDeps(), runtime=_runtime())))
    await client.start_server()
    try:
        response = await client.post(
            "/__pynchy_runtime__/messages",
            json={"jid": "runtime:pynchy", "content": "hello"},
        )
        assert response.status == 404
    finally:
        await client.close()


async def test_runtime_harness_ingress_calls_real_ingestion_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYNCHY_RUNTIME_HARNESS", "1")
    deps = MockHttpDeps()
    client = TestClient(TestServer(create_http_app(deps, runtime=_runtime())))
    await client.start_server()
    try:
        response = await client.post(
            "/__pynchy_runtime__/messages",
            json={"jid": "runtime:pynchy", "content": "hello"},
        )
        assert response.status == 200
        assert await response.json() == {"status": "accepted"}
        assert deps.runtime_messages == [("runtime:pynchy", "hello")]
    finally:
        await client.close()


async def test_canary_message_uses_existing_channel_broadcast() -> None:
    deps = MockHttpDeps()
    client = TestClient(TestServer(create_http_app(deps, runtime=_runtime())))
    await client.start_server()
    try:
        response = await client.post(
            "/canaries/messages",
            json={
                "jid": "discord:channel:1532671814738776174",
                "content": "use native search_skills",
            },
        )
        assert response.status == 200
        assert await response.json() == {"status": "accepted"}
        assert deps.synthetic_user_inputs == [
            ("discord:channel:1532671814738776174", "use native search_skills")
        ]
    finally:
        await client.close()


@pytest.mark.parametrize(
    "body",
    [
        ["not", "an", "object"],
        {"jid": "discord:channel:1"},
        {"jid": "slack:C1", "content": "wrong channel"},
    ],
)
async def test_canary_message_rejects_invalid_bodies(body: object) -> None:
    client = TestClient(TestServer(create_http_app(MockHttpDeps(), runtime=_runtime())))
    await client.start_server()
    try:
        response = await client.post("/canaries/messages", json=body)
        assert response.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/work-items?limit=0",
        "/actions?limit=201",
        "/webhook-effects?limit=not-a-number",
        "/canaries/report?limit=0",
        "/canaries/runs?limit=201",
    ],
)
async def test_diagnostic_endpoints_reject_invalid_limits(path: str) -> None:
    client = TestClient(TestServer(create_http_app(MockHttpDeps(), runtime=_runtime())))
    await client.start_server()
    try:
        response = await client.get(path)
        assert response.status == 400
        assert await response.json() == {"error": "limit must be an integer from 1 to 200"}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_webhook_effects_accept_all_status_filter() -> None:
    client = TestClient(TestServer(create_http_app(MockHttpDeps(), runtime=_runtime())))
    await client.start_server()
    try:
        with patch(
            "pynchy.host.orchestrator.http_server.list_webhook_effects",
            new=AsyncMock(return_value=[]),
        ) as list_effects:
            response = await client.get("/webhook-effects?status=all")
        assert response.status == 200
        assert await response.json() == {"status": "all", "effects": []}
        list_effects.assert_awaited_once_with(status=None, limit=100)

        response = await client.get("/webhook-effects?status=unknown")
        assert response.status == 400
        assert await response.json() == {"error": "unknown webhook effect status"}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_webhook_effect_absence_validates_json_and_conflicts() -> None:
    client = TestClient(TestServer(create_http_app(MockHttpDeps(), runtime=_runtime())))
    await client.start_server()
    try:
        response = await client.post(
            "/webhook-effects/effect-1/reconcile-absent",
            data=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        assert response.status == 400
        assert await response.json() == {"error": "verified_absent must be exactly true"}

        with patch(
            "pynchy.host.orchestrator.http_server.reconcile_webhook_effect_absent",
            new=AsyncMock(side_effect=ValueError("effect already resolved")),
        ):
            response = await client.post(
                "/webhook-effects/effect-1/reconcile-absent",
                json={"verified_absent": True},
            )
        assert response.status == 409
        assert await response.json() == {"error": "effect already resolved"}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_runtime_harness_ingress_rejects_invalid_bodies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYNCHY_RUNTIME_HARNESS", "1")
    client = TestClient(TestServer(create_http_app(MockHttpDeps(), runtime=_runtime())))
    await client.start_server()
    try:
        response = await client.post(
            "/__pynchy_runtime__/messages",
            json=["not", "an", "object"],
        )
        assert response.status == 400
        assert await response.json() == {"error": "request body must be an object"}

        response = await client.post(
            "/__pynchy_runtime__/messages",
            json={"jid": "runtime:pynchy"},
        )
        assert response.status == 400
        assert await response.json() == {"error": "jid and content are required strings"}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_prepare_http_server_cleans_runner_when_setup_fails() -> None:
    runner = web.AppRunner(web.Application())
    setup = AsyncMock(side_effect=RuntimeError("runner setup failed"))
    cleanup = AsyncMock()
    with (
        patch(
            "pynchy.host.orchestrator.http_server.collect_webhook_routes",
            return_value=(),
        ),
        patch("pynchy.host.orchestrator.http_server.web.AppRunner", return_value=runner),
        patch("aiohttp.web_runner.AppRunner.setup", new=setup),
        patch("aiohttp.web_runner.AppRunner.cleanup", new=cleanup),
        pytest.raises(RuntimeError, match="runner setup failed"),
    ):
        await prepare_http_server(MockHttpDeps(), runtime=_runtime())
    cleanup.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_deploy_continues_when_pre_deploy_push_fails() -> None:
    deps = MockHttpDeps()
    deps.deploy_operations.push_local_commits.return_value = False
    runtime = replace(_runtime(), allow_remote_deploy=True)
    client = TestClient(TestServer(create_http_app(deps, runtime=runtime)))
    await client.start_server()
    try:
        response = await client.post("/deploy")
        assert response.status == 200
        assert (await response.json())["status"] == "restarting"
    finally:
        await client.close()

    deps.deploy_operations.push_local_commits.assert_called_once_with(skip_fetch=True)


@pytest.mark.asyncio
async def test_deploy_restores_dirty_stash_before_returning() -> None:
    deps = MockHttpDeps()
    deps.deploy_operations.run_git.side_effect = [
        _cp(),  # fetch
        _cp(stdout=" M local.txt\n"),  # stash
        _cp(),  # rebase
        _cp(),  # stash pop
    ]
    runtime = replace(_runtime(), allow_remote_deploy=True)
    client = TestClient(TestServer(create_http_app(deps, runtime=runtime)))
    await client.start_server()
    try:
        response = await client.post("/deploy")
        assert response.status == 200
    finally:
        await client.close()

    assert deps.deploy_operations.run_git.call_args_list[-1].args == ("stash", "pop")


@pytest.mark.asyncio
async def test_deploy_stops_when_dirty_stash_cannot_be_restored() -> None:
    deps = MockHttpDeps()
    deps.deploy_operations.run_git.side_effect = [
        _cp(),  # fetch
        _cp(stdout=" M local.txt\n"),  # stash
        _cp(),  # rebase
        _cp(returncode=1, stderr="conflict"),  # stash pop
    ]
    runtime = replace(_runtime(), allow_remote_deploy=True)
    client = TestClient(TestServer(create_http_app(deps, runtime=runtime)))
    await client.start_server()
    try:
        response = await client.post("/deploy")
        assert response.status == 409
        assert await response.json() == {"error": "failed to restore local changes"}
    finally:
        await client.close()

    deps.deploy_operations.start_deploy_workflow.assert_not_awaited()


@pytest.mark.asyncio
async def test_deploy_rolls_back_when_import_validation_fails(tmp_path: Path) -> None:
    deps = MockHttpDeps()
    deps.data_dir = tmp_path
    deps.project_root = tmp_path
    deps.deploy_operations.get_head_sha.side_effect = ["old-sha", "new-sha", "old-sha"]
    deps.deploy_operations.run_git.side_effect = [
        _cp(),  # fetch
        _cp(stdout="No local changes"),  # stash
        _cp(),  # rebase
        _cp(),  # dirty-status check
        _cp(),  # reset --hard
    ]
    runtime = replace(_runtime(), allow_remote_deploy=True)
    client = TestClient(TestServer(create_http_app(deps, runtime=runtime)))
    await client.start_server()
    try:
        with patch(
            "pynchy.host.orchestrator.http_server.subprocess.run",
            return_value=_cp(returncode=1, stderr="import failed"),
        ):
            response = await client.post("/deploy")
        assert response.status == 422
        assert await response.json() == {
            "error": "import validation failed",
            "rolled_back_to": "old-sha",
        }
    finally:
        await client.close()

    assert deps.deploy_operations.run_git.call_args_list[-1].args == (
        "reset",
        "--hard",
        "old-sha",
    )
    assert deps.broadcasts == [
        ("admin-1@g.us", "Deploy failed — import validation error, rolled back to old-sha.")
    ]


@pytest.mark.asyncio
async def test_deploy_validates_restored_changes_and_preserves_them_on_rollback(
    tmp_path: Path,
) -> None:
    deps = MockHttpDeps()
    deps.data_dir = tmp_path
    deps.project_root = tmp_path
    deps.deploy_operations.get_head_sha.side_effect = ["old-sha", "new-sha", "old-sha"]
    deps.deploy_operations.run_git.side_effect = [
        _cp(),  # fetch
        _cp(stdout=" M local.txt\n"),  # stash
        _cp(),  # rebase
        _cp(),  # restore original stash before validation
        _cp(stdout=" M local.txt\n"),  # rollback dirty-status check
        _cp(stdout="Saved working directory"),  # protect local changes again
        _cp(),  # reset --hard
        _cp(),  # restore protected local changes
    ]
    runtime = replace(_runtime(), allow_remote_deploy=True)
    client = TestClient(TestServer(create_http_app(deps, runtime=runtime)))
    await client.start_server()
    try:
        with patch(
            "pynchy.host.orchestrator.http_server.subprocess.run",
            return_value=_cp(returncode=1, stderr="import failed"),
        ):
            response = await client.post("/deploy")
        assert response.status == 422
    finally:
        await client.close()

    assert [call.args for call in deps.deploy_operations.run_git.call_args_list[-5:]] == [
        ("stash", "pop"),
        ("status", "--porcelain", "--untracked-files=normal"),
        ("stash", "push", "--include-untracked"),
        ("reset", "--hard", "old-sha"),
        ("stash", "pop"),
    ]


@pytest.mark.asyncio
async def test_deploy_reports_import_rollback_failure(tmp_path: Path) -> None:
    deps = MockHttpDeps()
    deps.data_dir = tmp_path
    deps.project_root = tmp_path
    deps.deploy_operations.get_head_sha.side_effect = ["old-sha", "new-sha"]
    deps.deploy_operations.run_git.side_effect = [
        _cp(),  # fetch
        _cp(stdout="No local changes"),  # stash
        _cp(),  # rebase
        _cp(),  # dirty-status check
        _cp(returncode=1, stderr="reset failed"),
    ]
    runtime = replace(_runtime(), allow_remote_deploy=True)
    client = TestClient(TestServer(create_http_app(deps, runtime=runtime)))
    await client.start_server()
    try:
        with patch(
            "pynchy.host.orchestrator.http_server.subprocess.run",
            return_value=_cp(returncode=1, stderr="import failed"),
        ):
            response = await client.post("/deploy")
        assert response.status == 500
        assert await response.json() == {
            "error": "import validation failed",
            "rollback_failed": True,
        }
    finally:
        await client.close()


async def test_deploy_import_validation_failure_without_admin_notification(
    tmp_path: Path,
) -> None:
    deps = MockHttpDeps()
    deps._admin_jid = ""
    deps.data_dir = tmp_path
    deps.project_root = tmp_path
    deps.deploy_operations.get_head_sha.side_effect = ["old-sha", "new-sha", "old-sha"]
    deps.deploy_operations.run_git.side_effect = [
        _cp(),
        _cp(stdout="No local changes"),
        _cp(),
        _cp(),
        _cp(),
    ]
    runtime = replace(_runtime(), allow_remote_deploy=True)
    client = TestClient(TestServer(create_http_app(deps, runtime=runtime)))
    await client.start_server()
    try:
        with patch(
            "pynchy.host.orchestrator.http_server.subprocess.run",
            return_value=_cp(returncode=1, stderr="import failed"),
        ):
            response = await client.post("/deploy")
        assert response.status == 422
    finally:
        await client.close()

    assert deps.broadcasts == []


async def test_deploy_continues_after_import_validation_succeeds(tmp_path: Path) -> None:
    deps = MockHttpDeps()
    deps.data_dir = tmp_path
    deps.project_root = tmp_path
    deps.deploy_operations.get_head_sha.side_effect = ["old-sha", "new-sha"]
    deps.deploy_operations.run_git.side_effect = [
        _cp(),
        _cp(stdout="No local changes"),
        _cp(),
    ]
    runtime = replace(_runtime(), allow_remote_deploy=True)
    client = TestClient(TestServer(create_http_app(deps, runtime=runtime)))
    await client.start_server()
    try:
        with patch(
            "pynchy.host.orchestrator.http_server.subprocess.run",
            return_value=_cp(),
        ):
            response = await client.post("/deploy")
        assert response.status == 200
    finally:
        await client.close()
