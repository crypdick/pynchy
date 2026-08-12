"""Public behavior tests for MCP lifecycle edge cases."""

from __future__ import annotations

import json
import signal
import subprocess  # noqa: S404 - tests model configured process ownership.
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.host.container_manager.mcp.lifecycle import (
    build_env_args,
    build_stdio_env,
    ensure_docker_running,
    ensure_script_running,
    ensure_stdio_running,
    expand_arg_placeholders,
    kwargs_to_args,
    reap_stale_processes,
    terminate_process,
    warm_image_cache,
)
from pynchy.host.container_manager.mcp.resolution import McpInstance
from pynchy.plugins.api import McpServerConfig


def _instance(
    *,
    server_name: str = "tool",
    image: str | None = "image:latest",
    process: subprocess.Popen[bytes] | None = None,
    process_record_path: Path | None = None,
) -> McpInstance:
    return McpInstance(
        server_name=server_name,
        server_config=McpServerConfig(type="docker", image=image, port=8000),
        kwargs={},
        instance_id=server_name,
        container_name=f"pynchy-mcp-{server_name}",
        project_root=Path("/project"),
        port=9000,
        process=process,
        process_record_path=process_record_path,
    )


def _script_instance(*, process: subprocess.Popen[bytes] | None = None) -> McpInstance:
    return McpInstance(
        server_name="script",
        server_config=McpServerConfig(type="script", command="backend", port=8000),
        kwargs={},
        instance_id="script",
        container_name="unused",
        project_root=Path("/project"),
        port=9000,
        process=process,
    )


async def test_stdio_health_failure_terminates_the_owned_process():
    instance = McpInstance(
        server_name="stdio",
        server_config=McpServerConfig(
            type="stdio",
            command="bridge",
            port=8000,
            transport="streamable_http",
        ),
        kwargs={},
        instance_id="stdio",
        container_name="unused",
        project_root=Path("/project"),
        port=9000,
    )
    terminate = MagicMock()

    with (
        patch("pynchy.host.container_manager.mcp.lifecycle._start_owned_process"),
        patch(
            "pynchy.host.container_manager.mcp.lifecycle.wait_healthy",
            new=AsyncMock(side_effect=RuntimeError("bridge failed")),
        ),
        patch(
            "pynchy.host.container_manager.mcp.lifecycle.terminate_process",
            terminate,
        ),
        pytest.raises(RuntimeError, match="bridge failed"),
    ):
        await ensure_stdio_running(instance)

    terminate.assert_called_once_with(instance)


async def test_stdio_start_preserves_a_live_process():
    process = subprocess.Popen.__new__(subprocess.Popen)
    process.poll = MagicMock(return_value=None)
    instance = _script_instance(process=process)
    instance.server_config = McpServerConfig(
        type="stdio", command="backend", port=8000, transport="streamable_http"
    )

    with patch("pynchy.host.container_manager.mcp.lifecycle._start_owned_process") as start:
        await ensure_stdio_running(instance)

    start.assert_not_called()


async def test_stdio_start_requires_a_host_port():
    instance = McpInstance(
        server_name="stdio",
        server_config=McpServerConfig(
            type="stdio", command="bridge", port=8000, transport="streamable_http"
        ),
        kwargs={},
        instance_id="stdio",
        container_name="unused",
        project_root=Path("/project"),
        port=None,
    )

    with pytest.raises(RuntimeError, match="has no host port"):
        await ensure_stdio_running(instance)


async def test_stdio_start_builds_the_bridge_command_and_waits_for_health():
    instance = McpInstance(
        server_name="stdio",
        server_config=McpServerConfig(
            type="stdio",
            command="backend",
            args=["--workspace", "{workspace}"],
            port=8000,
            transport="streamable_http",
        ),
        kwargs={"workspace": "research"},
        instance_id="stdio",
        container_name="unused",
        project_root=Path("/project"),
        port=9000,
    )
    start = MagicMock()

    with (
        patch("pynchy.host.container_manager.mcp.lifecycle._start_owned_process", start),
        patch(
            "pynchy.host.container_manager.mcp.lifecycle.wait_healthy",
            new=AsyncMock(),
        ),
    ):
        await ensure_stdio_running(instance)

    command = start.call_args.args[1]
    assert command[:6] == [
        command[0],
        "-m",
        "pynchy.host.container_manager.mcp.stdio_bridge",
        "--port",
        "9000",
        "--",
    ]
    assert command[6:] == [
        "backend",
        "--workspace",
        "research",
        "--workspace",
        "research",
    ]


async def test_script_start_fails_when_process_supervision_shell_is_missing(monkeypatch):
    monkeypatch.setattr("pynchy.host.container_manager.mcp.lifecycle.shutil.which", lambda _: None)

    with pytest.raises(RuntimeError, match="requires sh"):
        await ensure_script_running(_script_instance())


async def test_script_start_uses_the_available_process_supervision_shell(monkeypatch):
    process = subprocess.Popen.__new__(subprocess.Popen)
    process.pid = 1234
    popen = MagicMock(return_value=process)
    wait_healthy = AsyncMock()
    monkeypatch.setattr(
        "pynchy.host.container_manager.mcp.lifecycle.shutil.which", lambda _: "/bin/sh"
    )
    monkeypatch.setattr("pynchy.host.container_manager.mcp.lifecycle.subprocess.Popen", popen)
    monkeypatch.setattr("pynchy.host.container_manager.mcp.lifecycle.wait_healthy", wait_healthy)

    instance = _script_instance()
    await ensure_script_running(instance)

    assert instance.process is process
    command = popen.call_args.args[0]
    assert command[:3] == ["/bin/sh", "-c", '"$@" &\nchild=$!\nwait "$child"\n']
    assert command[-1] == "backend"
    assert popen.call_args.kwargs["start_new_session"] is True
    assert wait_healthy.await_count == 1


async def test_script_start_terminates_process_when_record_persistence_fails(tmp_path: Path):
    process = subprocess.Popen.__new__(subprocess.Popen)
    process.pid = 1234
    instance = _script_instance()
    instance.process_record_path = tmp_path / "process.json"
    terminate = MagicMock()

    with (
        patch(
            "pynchy.host.container_manager.mcp.lifecycle._start_script_process",
            return_value=process,
        ),
        patch(
            "pynchy.host.container_manager.mcp.lifecycle.write_json_atomic",
            side_effect=OSError("disk full"),
        ),
        patch("pynchy.host.container_manager.mcp.lifecycle.terminate_process", terminate),
        pytest.raises(OSError, match="disk full"),
    ):
        await ensure_script_running(instance)

    terminate.assert_called_once_with(instance)


async def test_warm_image_cache_deduplicates_images_and_continues_after_failure():
    first = _instance(server_name="first")
    duplicate = _instance(server_name="duplicate")
    duplicate.server_config = first.server_config
    second = _instance(server_name="second", image="other:latest")
    warm = AsyncMock(side_effect=[RuntimeError("unavailable"), None])

    with patch(
        "pynchy.host.container_manager.mcp.lifecycle._ensure_mcp_image",
        warm,
    ):
        await warm_image_cache({"first": first, "duplicate": duplicate, "second": second})

    assert warm.await_args_list[0].args == (first.server_config, first.project_root)
    assert warm.await_args_list[1].args == (second.server_config, second.project_root)


async def test_warm_image_cache_builds_a_missing_local_dockerfile_image():
    instance = _instance(server_name="notebook")
    instance.server_config = McpServerConfig(
        type="docker",
        image="local/notebook:latest",
        dockerfile="src/Dockerfile",
        build_context="src/context",
        port=8000,
    )
    run_docker = AsyncMock(
        side_effect=[
            subprocess.CompletedProcess([], 1, stdout="", stderr="missing"),
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        ]
    )

    with patch("pynchy.host.container_manager.mcp.lifecycle.run_docker", run_docker):
        await warm_image_cache({"notebook": instance})

    assert run_docker.await_args_list[0].args == ("image", "inspect", "local/notebook:latest")
    assert run_docker.await_args_list[1].args == (
        "build",
        "-t",
        "local/notebook:latest",
        "-f",
        "/project/src/Dockerfile",
        "/project/src/context",
    )


async def test_warm_image_cache_pulls_a_registry_image_without_a_dockerfile():
    instance = _instance(server_name="browser")
    instance.server_config = McpServerConfig(
        type="docker",
        image="registry.example/browser:latest",
        port=8000,
    )
    ensure_image = AsyncMock()

    with patch("pynchy.host.container_manager.mcp.lifecycle.ensure_image", ensure_image):
        await warm_image_cache({"browser": instance})

    ensure_image.assert_awaited_once_with("registry.example/browser:latest")


def test_terminate_process_clears_an_already_exited_process_and_record(tmp_path: Path):
    record = tmp_path / "process.json"
    record.write_text("{}")
    process = subprocess.Popen.__new__(subprocess.Popen)
    process.poll = MagicMock(return_value=0)
    instance = _instance(process=process, process_record_path=record)

    terminate_process(instance)

    assert instance.process is None
    assert instance.process_marker is None
    assert not record.exists()


def test_terminate_process_signals_a_live_process_group():
    process = subprocess.Popen.__new__(subprocess.Popen)
    process.poll = MagicMock(return_value=None)
    instance = _instance(process=process)

    with patch(
        "pynchy.host.container_manager.mcp.lifecycle._terminate_process_group"
    ) as terminate_group:
        terminate_process(instance)

    terminate_group.assert_called_once_with(process)
    assert instance.process is None


def test_terminate_process_escalates_when_group_ignores_sigterm():
    process = subprocess.Popen.__new__(subprocess.Popen)
    process.pid = 1234
    process.poll = MagicMock(return_value=None)
    process.wait = MagicMock(side_effect=[subprocess.TimeoutExpired("backend", 5), None])
    instance = _instance(process=process)

    with patch("pynchy.host.container_manager.mcp.lifecycle.os.killpg") as killpg:
        terminate_process(instance)

    assert killpg.call_args_list[0].args == (1234, 15)
    assert killpg.call_args_list[1].args == (1234, 9)
    assert process.wait.call_count == 2


def test_reap_stale_processes_removes_invalid_records(tmp_path: Path):
    record_dir = tmp_path / "records"
    record_dir.mkdir()
    (record_dir / "broken.json").write_text("not json")
    (record_dir / "wrong-shape.json").write_text(json.dumps(["not", "a", "record"]))
    (record_dir / "invalid-pid.json").write_text(
        json.dumps({"pid": 1, "marker": "pynchy-mcp-" + "a" * 32})
    )

    assert reap_stale_processes(record_dir) == 0
    assert list(record_dir.iterdir()) == []


def test_reap_stale_processes_rejects_invalid_and_uninspectable_markers(
    monkeypatch, tmp_path: Path
):
    record_dir = tmp_path / "records"
    record_dir.mkdir()
    (record_dir / "invalid-marker.json").write_text(
        json.dumps({"pid": 1234, "marker": "not-owned"})
    )
    (record_dir / "missing-ps.json").write_text(
        json.dumps({"pid": 1234, "marker": "pynchy-mcp-" + "a" * 32})
    )
    monkeypatch.setattr("pynchy.host.container_manager.mcp.lifecycle.shutil.which", lambda _: None)

    assert reap_stale_processes(record_dir) == 0
    assert list(record_dir.iterdir()) == []


def test_reap_stale_processes_removes_records_when_process_inspection_fails(
    monkeypatch, tmp_path: Path
):
    record_dir = tmp_path / "records"
    record_dir.mkdir()
    marker = "pynchy-mcp-" + "a" * 32
    (record_dir / "uninspectable.json").write_text(json.dumps({"pid": 1234, "marker": marker}))
    monkeypatch.setattr("pynchy.host.container_manager.mcp.lifecycle.shutil.which", lambda _: "ps")
    monkeypatch.setattr(
        "pynchy.host.container_manager.mcp.lifecycle.subprocess.run",
        MagicMock(side_effect=OSError("ps unavailable")),
    )

    assert reap_stale_processes(record_dir) == 0
    assert not list(record_dir.iterdir())


def test_reap_stale_processes_escalates_after_a_group_survives_grace_period(
    monkeypatch, tmp_path: Path
):
    record_dir = tmp_path / "records"
    record_dir.mkdir()
    marker = "pynchy-mcp-" + "a" * 32
    (record_dir / "owned.json").write_text(json.dumps({"pid": 1234, "marker": marker}))
    monkeypatch.setattr("pynchy.host.container_manager.mcp.lifecycle.shutil.which", lambda _: "ps")
    monkeypatch.setattr(
        "pynchy.host.container_manager.mcp.lifecycle.subprocess.run",
        MagicMock(return_value=subprocess.CompletedProcess([], 0, marker, "")),
    )
    monkeypatch.setattr(
        "pynchy.host.container_manager.mcp.lifecycle.time.monotonic",
        MagicMock(side_effect=[0, 1, 6]),
    )
    monkeypatch.setattr(
        "pynchy.host.container_manager.mcp.lifecycle._process_group_exists",
        MagicMock(return_value=True),
    )
    sleep = MagicMock()
    monkeypatch.setattr("pynchy.host.container_manager.mcp.lifecycle.time.sleep", sleep)

    with patch("pynchy.host.container_manager.mcp.lifecycle.os.killpg") as killpg:
        assert reap_stale_processes(record_dir) == 1

    sleep.assert_called_once_with(0.1)
    assert killpg.call_args_list == [
        ((1234, signal.SIGTERM),),
        ((1234, signal.SIGKILL),),
    ]


def test_reap_stale_processes_treats_permission_denied_as_an_existing_group(
    monkeypatch, tmp_path: Path
):
    record_dir = tmp_path / "records"
    record_dir.mkdir()
    marker = "pynchy-mcp-" + "a" * 32
    (record_dir / "owned.json").write_text(json.dumps({"pid": 1234, "marker": marker}))
    monkeypatch.setattr("pynchy.host.container_manager.mcp.lifecycle.shutil.which", lambda _: "ps")
    monkeypatch.setattr(
        "pynchy.host.container_manager.mcp.lifecycle.subprocess.run",
        MagicMock(return_value=subprocess.CompletedProcess([], 0, marker, "")),
    )
    monkeypatch.setattr(
        "pynchy.host.container_manager.mcp.lifecycle.time.monotonic",
        MagicMock(side_effect=[0, 1, 6]),
    )
    monkeypatch.setattr("pynchy.host.container_manager.mcp.lifecycle.time.sleep", MagicMock())

    with patch(
        "pynchy.host.container_manager.mcp.lifecycle.os.killpg",
        side_effect=[None, PermissionError, None],
    ) as killpg:
        assert reap_stale_processes(record_dir) == 1

    assert killpg.call_args_list == [
        ((1234, signal.SIGTERM),),
        ((1234, 0),),
        ((1234, signal.SIGKILL),),
    ]


def test_reap_stale_processes_accepts_a_group_that_exits_during_cleanup(
    monkeypatch, tmp_path: Path
):
    record_dir = tmp_path / "records"
    record_dir.mkdir()
    marker = "pynchy-mcp-" + "a" * 32
    (record_dir / "owned.json").write_text(json.dumps({"pid": 1234, "marker": marker}))
    monkeypatch.setattr("pynchy.host.container_manager.mcp.lifecycle.shutil.which", lambda _: "ps")
    monkeypatch.setattr(
        "pynchy.host.container_manager.mcp.lifecycle.subprocess.run",
        MagicMock(return_value=subprocess.CompletedProcess([], 0, marker, "")),
    )

    with patch(
        "pynchy.host.container_manager.mcp.lifecycle.os.killpg",
        side_effect=[None, ProcessLookupError],
    ) as killpg:
        assert reap_stale_processes(record_dir) == 1

    killpg.assert_has_calls([((1234, signal.SIGTERM),), ((1234, 0),)])
    assert not list(record_dir.iterdir())


def test_reap_stale_processes_reaps_a_verified_owned_group(monkeypatch, tmp_path: Path):
    record_dir = tmp_path / "records"
    record_dir.mkdir()
    marker = "pynchy-mcp-" + "a" * 32
    (record_dir / "owned.json").write_text(json.dumps({"pid": 1234, "marker": marker}))
    monkeypatch.setattr("pynchy.host.container_manager.mcp.lifecycle.shutil.which", lambda _: "ps")
    monkeypatch.setattr(
        "pynchy.host.container_manager.mcp.lifecycle.subprocess.run",
        MagicMock(return_value=subprocess.CompletedProcess([], 0, marker + " -- backend", "")),
    )
    monkeypatch.setattr(
        "pynchy.host.container_manager.mcp.lifecycle._process_group_exists", lambda _: False
    )

    with patch("pynchy.host.container_manager.mcp.lifecycle.os.killpg") as killpg:
        assert reap_stale_processes(record_dir) == 1

    killpg.assert_called_once_with(1234, 15)
    assert not list(record_dir.iterdir())


def test_reap_stale_processes_ignores_a_missing_record_directory(tmp_path: Path):
    assert reap_stale_processes(tmp_path / "missing") == 0


async def test_warm_image_cache_skips_a_local_image_that_is_already_present():
    instance = _instance(server_name="notebook")
    instance.server_config = McpServerConfig(
        type="docker",
        image="local/notebook:latest",
        dockerfile="src/Dockerfile",
        port=8000,
    )
    inspect = AsyncMock(return_value=subprocess.CompletedProcess([], 0))

    with patch("pynchy.host.container_manager.mcp.lifecycle.run_docker", inspect):
        await warm_image_cache({"notebook": instance})

    inspect.assert_awaited_once_with("image", "inspect", "local/notebook:latest", check=False)


async def test_docker_mount_resolution_handles_existing_and_file_sources(tmp_path: Path):
    existing = tmp_path / "existing"
    existing.mkdir()
    file_source = tmp_path / "nested" / "settings.json"
    instance = _instance()
    instance.project_root = tmp_path
    instance.server_config = McpServerConfig(
        type="docker",
        image="image:latest",
        port=8000,
        volumes=[f"{existing}:/existing", f"{file_source}:/settings.json", "/anonymous"],
    )
    run_docker = AsyncMock(return_value=subprocess.CompletedProcess([], 0))

    with (
        patch(
            "pynchy.host.container_manager.mcp.lifecycle.is_container_running",
            new=AsyncMock(return_value=False),
        ),
        patch("pynchy.host.container_manager.mcp.lifecycle._ensure_mcp_image", new=AsyncMock()),
        patch("pynchy.host.container_manager.mcp.lifecycle.ensure_network", new=AsyncMock()),
        patch("pynchy.host.container_manager.mcp.lifecycle.remove_container", new=AsyncMock()),
        patch("pynchy.host.container_manager.mcp.lifecycle.run_docker", run_docker),
        patch("pynchy.host.container_manager.mcp.lifecycle.wait_healthy", new=AsyncMock()),
    ):
        await ensure_docker_running(instance)

    assert file_source.parent.is_dir()
    assert f"{existing}:/existing" in run_docker.await_args.args
    assert f"{file_source}:/settings.json" in run_docker.await_args.args
    assert "/anonymous" in run_docker.await_args.args


async def test_docker_health_failure_redacts_secret_values_from_diagnostics():
    instance = _instance()
    run_docker = AsyncMock(
        return_value=subprocess.CompletedProcess(
            [], 0, stdout="authorization: bearer-token", stderr="password=hunter2"
        )
    )
    error = MagicMock()

    with (
        patch(
            "pynchy.host.container_manager.mcp.lifecycle.is_container_running",
            new=AsyncMock(return_value=False),
        ),
        patch("pynchy.host.container_manager.mcp.lifecycle._ensure_mcp_image", new=AsyncMock()),
        patch("pynchy.host.container_manager.mcp.lifecycle.ensure_network", new=AsyncMock()),
        patch("pynchy.host.container_manager.mcp.lifecycle.remove_container", new=AsyncMock()),
        patch(
            "pynchy.host.container_manager.mcp.lifecycle._start_docker_container",
            new=AsyncMock(),
        ),
        patch(
            "pynchy.host.container_manager.mcp.lifecycle.wait_healthy",
            new=AsyncMock(side_effect=RuntimeError("not ready")),
        ),
        patch("pynchy.host.container_manager.mcp.lifecycle.run_docker", run_docker),
        patch("pynchy.host.container_manager.mcp.lifecycle.stop_container", new=AsyncMock()),
        patch("pynchy.host.container_manager.mcp.lifecycle.logger.error", error),
        pytest.raises(RuntimeError, match="not ready"),
    ):
        await ensure_docker_running(instance)

    log_tail = error.call_args.kwargs["log_tail"]
    assert "bearer-token" not in log_tail
    assert "hunter2" not in log_tail
    assert "redacted sensitive data" in log_tail


def test_expand_arg_placeholders_preserves_unknown_placeholders():
    assert expand_arg_placeholders(
        ["--workspace", "{workspace}", "--unknown", "{missing}"],
        {"workspace": "research"},
    ) == ["--workspace", "research", "--unknown", "{missing}"]


def test_kwargs_and_environment_helpers_are_deterministic_and_filtered(monkeypatch):
    assert kwargs_to_args({"workspace": "research", "port": "9000"}) == [
        "--port",
        "9000",
        "--workspace",
        "research",
    ]
    assert build_env_args({"ZED": "secret", "ALPHA": "value"}) == [
        "-e",
        "ALPHA",
        "-e",
        "ZED",
    ]

    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("UNRELATED_SECRET", "do-not-forward")
    config = McpServerConfig(
        type="stdio",
        command="bridge",
        port=8000,
        transport="streamable_http",
        env={"STATIC": "value"},
    )
    with patch(
        "pynchy.host.container_manager.mcp.lifecycle.filtered_process_environment",
        side_effect=lambda env: env,
    ):
        environment = build_stdio_env(config, {"SELECTED": "token"})

    assert environment == {
        "STATIC": "value",
        "SELECTED": "token",
    }
