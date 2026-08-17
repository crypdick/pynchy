"""Public LiteLLM startup behavior around PostgreSQL readiness."""

from __future__ import annotations

import subprocess  # noqa: S404 - tests construct inert subprocess results.
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from pynchy.host.container_manager.gateway import LiteLLMGateway

if TYPE_CHECKING:
    from pathlib import Path


_MODULE = "pynchy.host.container_manager.gateway_litellm"
_DOCKER = "pynchy.host.container_manager.docker"
_GATEWAY_KWARGS = {
    "port": 4000,
    "container_host": "host.docker.internal",
    "image": "ghcr.io/berriai/litellm:main-latest",
    "postgres_image": "postgres:17-alpine",
    "master_key": "test-master-key",
}


def _docker_result(*, returncode: int, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, "")


def _gateway(tmp_path: Path) -> LiteLLMGateway:
    config = tmp_path / "litellm.yaml"
    config.write_text(
        "model_list:\n  - model_name: gpt-5.5\n    litellm_params:\n      model: chatgpt/gpt-5.5\n"
    )
    return LiteLLMGateway(config_path=str(config), data_dir=tmp_path, **_GATEWAY_KWARGS)


def _startup_patches(gateway: LiteLLMGateway):
    return (
        patch(f"{_MODULE}.docker_available", return_value=True),
        patch(f"{_MODULE}.ensure_network", new_callable=AsyncMock),
        patch(f"{_MODULE}.ensure_image", new_callable=AsyncMock),
        patch(f"{_MODULE}.remove_container", new_callable=AsyncMock),
        patch(f"{_MODULE}.wait_healthy", new_callable=AsyncMock),
        patch(f"{_MODULE}.LiteLLMResponsesAvailability.refresh", new_callable=AsyncMock),
    )


@pytest.mark.asyncio
async def test_start_accepts_a_healthy_postgres_sidecar(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)
    docker = AsyncMock(
        side_effect=[
            _docker_result(returncode=0),  # postgres run
            _docker_result(returncode=0),  # pg_isready
            _docker_result(returncode=0),  # LiteLLM run
        ]
    )

    patches = _startup_patches(gateway)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patch(f"{_MODULE}.run_docker", docker),
    ):
        await gateway.start()

    assert docker.await_args_list[1].args[:3] == (
        "exec",
        "pynchy-litellm-db",
        "pg_isready",
    )


@pytest.mark.asyncio
async def test_external_litellm_checks_readiness_without_managing_containers(
    tmp_path: Path,
) -> None:
    config = tmp_path / "litellm.yaml"
    config.write_text(
        "model_list:\n  - model_name: gpt-5.5\n    litellm_params:\n      model: openai/gpt-5.5\n"
    )
    gateway = LiteLLMGateway(
        config_path=str(config),
        data_dir=tmp_path,
        managed=False,
        **_GATEWAY_KWARGS,
    )

    with (
        patch(f"{_MODULE}.docker_available", return_value=False),
        patch(f"{_MODULE}.run_docker", new_callable=AsyncMock) as docker,
        patch(f"{_MODULE}.wait_healthy", new_callable=AsyncMock) as wait_healthy,
        patch(f"{_MODULE}.LiteLLMResponsesAvailability.refresh", new_callable=AsyncMock),
    ):
        await gateway.start()

    docker.assert_not_awaited()
    request = wait_healthy.await_args.args[0]
    assert request.container_name is None
    assert request.url == "http://localhost:4000/health/readiness"
    assert request.headers == {"Authorization": "Bearer test-master-key"}


@pytest.mark.asyncio
async def test_start_uses_a_named_postgres_volume_for_a_non_default_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYNCHY_RUNTIME_NAMESPACE", "pynchy-test")
    gateway = _gateway(tmp_path)
    docker = AsyncMock(
        side_effect=[
            _docker_result(returncode=0),  # postgres run
            _docker_result(returncode=0),  # pg_isready
            _docker_result(returncode=0),  # LiteLLM run
        ]
    )

    patches = _startup_patches(gateway)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patch(f"{_MODULE}.run_docker", docker),
    ):
        await gateway.start()

    postgres_run = docker.await_args_list[0].args
    assert "-v" in postgres_run
    assert "pynchy-test-litellm-db-data:/var/lib/postgresql/data" in postgres_run


@pytest.mark.asyncio
async def test_start_reports_an_exited_postgres_sidecar(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)
    docker = AsyncMock(
        side_effect=[
            _docker_result(returncode=0),  # postgres run
            _docker_result(returncode=1),  # pg_isready
            _docker_result(returncode=0, stdout="false\n"),  # inspect
            _docker_result(returncode=0, stdout="database crashed\n"),  # logs
        ]
    )

    patches = _startup_patches(gateway)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patch(f"{_MODULE}.run_docker", docker),
        pytest.raises(RuntimeError, match="PostgreSQL container failed to start"),
    ):
        await gateway.start()


@pytest.mark.asyncio
async def test_start_times_out_when_postgres_never_becomes_ready(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)
    docker = AsyncMock(return_value=_docker_result(returncode=0))

    patches = _startup_patches(gateway)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patch(f"{_MODULE}.run_docker", docker),
        patch(f"{_MODULE}._POSTGRES_HEALTH_TIMEOUT", 0),
        pytest.raises(TimeoutError, match="PostgreSQL did not become ready"),
    ):
        await gateway.start()

    assert docker.await_count == 1
