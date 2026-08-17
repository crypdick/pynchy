"""Behavior contracts for the shared Docker helper surface."""

from __future__ import annotations

import asyncio
import subprocess  # noqa: S404 - tests construct inert subprocess result/process fixtures.
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from pynchy.host.container_manager import docker


def _result(*, returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, "")


def test_docker_available_follows_path_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(docker.shutil, "which", lambda _name: None)
    assert docker.docker_available() is False

    monkeypatch.setattr(docker.shutil, "which", lambda _name: "/usr/bin/docker")
    assert docker.docker_available() is True


def test_container_helpers_select_kubernetes_adapter_and_cluster_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYNCHY_CONTAINER_CLI", "pynchy-kubernetes-runtime")
    monkeypatch.setattr(docker.shutil, "which", lambda name: f"/usr/bin/{name}")

    assert docker.docker_available() is True
    assert (
        docker.managed_container_url("pynchy-mcp-browser", host_port=19101, container_port=8931)
        == "http://pynchy-mcp-browser:8931"
    )


def test_managed_container_url_uses_published_host_port_by_default() -> None:
    assert (
        docker.managed_container_url("pynchy-mcp-browser", host_port=19101, container_port=8931)
        == "http://localhost:19101"
    )


@pytest.mark.asyncio
async def test_docker_image_and_network_are_created_only_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_docker = AsyncMock(
        side_effect=[_result(returncode=1), _result(), _result(returncode=1), _result()]
    )
    monkeypatch.setattr(docker, "run_docker", run_docker)

    await docker.ensure_image("example/image:latest")
    await docker.ensure_network("pynchy-test")

    assert run_docker.await_args_list[0].args == ("image", "inspect", "example/image:latest")
    assert run_docker.await_args_list[1].args == ("pull", "example/image:latest")
    assert run_docker.await_args_list[1].kwargs["command_timeout_seconds"] == 300
    assert run_docker.await_args_list[2].args == ("network", "inspect", "pynchy-test")
    assert run_docker.await_args_list[3].args == ("network", "create", "pynchy-test")


@pytest.mark.asyncio
async def test_docker_image_and_network_noop_when_already_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_docker = AsyncMock(return_value=_result())
    monkeypatch.setattr(docker, "run_docker", run_docker)

    await docker.ensure_image("example/image:latest")
    await docker.ensure_network("pynchy-test")

    assert [call.args for call in run_docker.await_args_list] == [
        ("image", "inspect", "example/image:latest"),
        ("network", "inspect", "pynchy-test"),
    ]


@pytest.mark.asyncio
async def test_docker_container_helpers_report_status_and_clean_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_docker = AsyncMock(
        side_effect=[
            _result(stdout="true\n"),
            _result(stdout="false\n"),
            _result(),
            _result(),
            _result(),
        ]
    )
    monkeypatch.setattr(docker, "run_docker", run_docker)

    assert await docker.is_container_running("running") is True
    assert await docker.is_container_running("stopped") is False
    await docker.remove_container("stale")
    await docker.stop_container("active", stop_timeout_seconds=9)

    assert run_docker.await_args_list[2].args == ("rm", "-f", "stale")
    assert run_docker.await_args_list[3].args == ("stop", "-t", "9", "active")
    assert run_docker.await_args_list[4].args == ("rm", "-f", "active")


@pytest.mark.asyncio
async def test_docker_inspect_logs_when_slow(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = MagicMock()
    clock.side_effect = lambda: 0.0 if clock.call_count == 1 else 1.0
    monkeypatch.setattr(docker.time, "monotonic", clock)
    monkeypatch.setattr(docker, "run_docker", AsyncMock(return_value=_result(stdout="true")))

    assert await docker.is_container_running("slow") is True


@pytest.mark.asyncio
async def test_docker_wait_healthy_accepts_an_http_endpoint() -> None:
    async def healthy(_request: web.Request) -> web.Response:
        await asyncio.sleep(0)
        return web.Response(status=200)

    app = web.Application()
    app.router.add_get("/health", healthy)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        sockets = site._server.sockets
        port = sockets[0].getsockname()[1]
        await docker.wait_healthy(
            docker.HealthCheckRequest(
                container_name="test-http",
                url=f"http://127.0.0.1:{port}/health",
                health_timeout_seconds=1.0,
                poll_interval=0.01,
            )
        )
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_docker_wait_healthy_does_not_inspect_an_external_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter((503, 200))

    async def eventually_healthy(_request: web.Request) -> web.Response:
        await asyncio.sleep(0)
        return web.Response(status=next(responses))

    app = web.Application()
    app.router.add_get("/health", eventually_healthy)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    inspect = AsyncMock()
    monkeypatch.setattr(docker, "is_container_running", inspect)
    try:
        port = site._server.sockets[0].getsockname()[1]
        await docker.wait_healthy(
            docker.HealthCheckRequest(
                container_name=None,
                url=f"http://127.0.0.1:{port}/health",
                health_timeout_seconds=1.0,
                poll_interval=0.01,
            )
        )
    finally:
        await runner.cleanup()

    inspect.assert_not_awaited()


@pytest.mark.asyncio
async def test_docker_wait_healthy_can_accept_non_server_errors() -> None:
    async def unavailable(_request: web.Request) -> web.Response:
        await asyncio.sleep(0)
        return web.Response(status=404)

    app = web.Application()
    app.router.add_get("/health", unavailable)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        port = site._server.sockets[0].getsockname()[1]
        await docker.wait_healthy(
            docker.HealthCheckRequest(
                container_name="test-http",
                url=f"http://127.0.0.1:{port}/health",
                health_timeout_seconds=1.0,
                any_non_5xx=True,
            )
        )
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_docker_wait_healthy_rejects_an_unrelated_healthy_listener() -> None:
    async def healthy(_request: web.Request) -> web.Response:
        await asyncio.sleep(0)
        return web.Response(status=200)

    app = web.Application()
    app.router.add_get("/health", healthy)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    process = subprocess.Popen.__new__(subprocess.Popen)
    process.poll = MagicMock(return_value=1)
    try:
        port = site._server.sockets[0].getsockname()[1]
        with pytest.raises(RuntimeError, match="exited unexpectedly"):
            await docker.wait_healthy(
                docker.HealthCheckRequest(
                    container_name="owned-process",
                    url=f"http://127.0.0.1:{port}/health",
                    health_timeout_seconds=1.0,
                    process=process,
                )
            )
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_docker_wait_healthy_reports_an_exited_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(docker, "is_container_running", AsyncMock(return_value=False))
    run_docker = AsyncMock(return_value=_result(stdout="container logs"))
    monkeypatch.setattr(docker, "run_docker", run_docker)

    with pytest.raises(RuntimeError, match="failed to start"):
        await docker.wait_healthy(
            docker.HealthCheckRequest(
                container_name="stopped-container",
                url="http://127.0.0.1:9/health",
                health_timeout_seconds=1.0,
            )
        )


@pytest.mark.asyncio
async def test_docker_wait_healthy_redacts_exited_container_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(docker, "is_container_running", AsyncMock(return_value=False))
    raw_secret = "".join(("visible", "-runtime-value"))
    monkeypatch.setattr(
        docker,
        "run_docker",
        AsyncMock(return_value=_result(stdout=f"api_key={raw_secret}")),
    )
    error = MagicMock()
    monkeypatch.setattr(docker.logger, "error", error)

    with pytest.raises(RuntimeError, match="failed to start"):
        await docker.wait_healthy(
            docker.HealthCheckRequest(
                container_name="stopped-container",
                url="http://127.0.0.1:9/health",
                health_timeout_seconds=1.0,
            )
        )

    assert raw_secret not in error.call_args.kwargs["logs"]


def test_container_log_redaction_happens_before_tail_truncation() -> None:
    raw_secret = "".join(("visible", "-runtime-value"))
    result = _result(stdout=f"api_key={raw_secret}")

    logs = docker.redacted_container_logs(result, limit=len(raw_secret) - 1)

    assert raw_secret[-10:] not in logs


def test_container_log_redaction_covers_bare_token_key() -> None:
    raw_secret = "".join(("visible", "-runtime-value"))

    logs = docker.redacted_container_logs(_result(stdout=f"token={raw_secret}"), limit=2000)

    assert raw_secret not in logs


@pytest.mark.asyncio
async def test_docker_wait_healthy_reports_a_process_exit() -> None:
    process = subprocess.Popen.__new__(subprocess.Popen)
    process.poll = MagicMock(return_value=1)

    with pytest.raises(RuntimeError, match="Script script exited unexpectedly"):
        await docker.wait_healthy(
            docker.HealthCheckRequest(
                container_name="script",
                url="http://127.0.0.1:9/health",
                health_timeout_seconds=1.0,
                process=process,
            )
        )


@pytest.mark.asyncio
async def test_docker_wait_healthy_times_out_after_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(docker, "is_container_running", AsyncMock(return_value=True))

    with pytest.raises(TimeoutError, match="did not become healthy"):
        await docker.wait_healthy(
            docker.HealthCheckRequest(
                container_name="slow-container",
                url="http://127.0.0.1:9/health",
                health_timeout_seconds=0.01,
                poll_interval=0.001,
            )
        )
