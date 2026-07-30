"""Public behavior tests for MCP lifecycle edge cases."""

from __future__ import annotations

import json
import subprocess  # noqa: S404 - tests model configured process ownership.
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pynchy.host.container_manager.mcp.lifecycle import (
    build_env_args,
    build_stdio_env,
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


async def test_script_start_fails_when_process_supervision_shell_is_missing(monkeypatch):
    monkeypatch.setattr("pynchy.host.container_manager.mcp.lifecycle.shutil.which", lambda _: None)

    with pytest.raises(RuntimeError, match="requires sh"):
        await ensure_script_running(_script_instance())


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
