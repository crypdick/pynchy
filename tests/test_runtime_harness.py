"""Contract tests for the hermetic deterministic runtime harness."""

from __future__ import annotations

import argparse
import contextlib
import json
import stat
import subprocess  # noqa: S404, RUF100 - test helpers record mocked subprocess results only.
import sys
import time
from typing import TYPE_CHECKING

import pytest
from scripts import runtime_harness as harness

if TYPE_CHECKING:
    from pathlib import Path


def _runtime_root(tmp_path: Path, name: str = "runtime") -> Path:
    root = tmp_path / name
    root.mkdir()
    root.joinpath("pyproject.toml").write_text("[project]\nname = 'pynchy-runtime-test'\n")
    agent_root = root / "src" / "pynchy" / "agent"
    runner_root = agent_root / "agent_runner"
    runner_source = runner_root / "src" / "agent_runner"
    runner_source.mkdir(parents=True)
    agent_root.joinpath("runtime.Dockerfile").write_text("FROM scratch\n")
    agent_root.joinpath(".dockerignore").write_text("**/__pycache__/\n")
    agent_root.joinpath("runtime_entrypoint.sh").write_text("#!/bin/sh\n")
    runner_root.joinpath("pyproject.toml").write_text("[project]\nname = 'agent-runner'\n")
    runner_root.joinpath("uv.lock").write_text("version = 1\n")
    runner_source.joinpath("main.py").write_text("print('agent')\n")
    return root


def _spec(root: Path) -> harness.RuntimeSpec:
    return harness.RuntimeSpec(
        root=root,
        namespace="pynchy-runtime-test",
        server_port=18484,
        gateway_port=14010,
        temporal_port=17233,
    )


def _process_marker(name: str) -> str:
    """Build a regex-valid but visibly synthetic harness process marker."""
    return f"pynchy-runtime-{name}-{'0' * 32}"


def _dotenv_values(path: Path) -> dict[str, str]:
    return {
        key: json.loads(value)
        for line in path.read_text(encoding="utf-8").splitlines()
        for key, value in [line.split("=", maxsplit=1)]
    }


def _setup_without_starting_services(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive setup's generated-file contract without launching local services."""
    monkeypatch.setattr("scripts.runtime_harness._initialize_runtime_data", lambda _spec: None)
    monkeypatch.setattr("scripts.runtime_harness._start_runtime_services", lambda *_: None)


def _write_runtime_state(spec: harness.RuntimeSpec, state: dict[str, object]) -> None:
    spec.state_path.parent.mkdir(parents=True)
    spec.state_path.write_text(json.dumps(state), encoding="utf-8")


def test_generated_runtime_config_is_deterministic_and_credential_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _runtime_root(tmp_path)
    spec = _spec(root)
    monkeypatch.setenv("OPENAI_API_KEY", "paid-provider-secret")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "channel-secret")
    _setup_without_starting_services(monkeypatch)

    state = harness.setup(spec)

    dotenv = _dotenv_values(root / ".env")
    personalization = root / "data" / "personalization"
    litellm_config = personalization.joinpath("litellm.yaml").read_text(encoding="utf-8")
    config = personalization.joinpath("pynchy.toml").read_text(encoding="utf-8")

    assert set(dotenv) == {
        "GATEWAY__MASTER_KEY",
        "PYNCHY_DETERMINISTIC_API_KEY",
        "PYNCHY_DISABLE_SERVICE_INSTALL",
        "PYNCHY_RUNTIME_HARNESS",
        "PYNCHY_RUNTIME_NAMESPACE",
    }
    assert dotenv["GATEWAY__MASTER_KEY"].startswith("sk-")
    assert dotenv["PYNCHY_RUNTIME_HARNESS"] == "1"
    assert stat.S_IMODE((root / ".env").stat().st_mode) == 0o600
    assert "model_name: pynchy-deterministic" in litellm_config
    assert f"api_base: http://{spec.fake_container_name}:8080/v1" in litellm_config
    assert "api_key: os.environ/PYNCHY_DETERMINISTIC_API_KEY" in litellm_config
    assert "public_routes" not in litellm_config
    assert 'default_core = "openai"' in config
    assert 'model = "pynchy-deterministic"' in config
    assert f'image = "pynchy-runtime-agent:{spec.namespace}-' in config
    assert "[workspaces.pynchy]" in config
    assert state["model"] == "pynchy-deterministic"
    assert str(state["agent_image"]).startswith(f"pynchy-runtime-agent:{spec.namespace}-")
    assert isinstance(state["agent_source_digest"], str)
    assert len(state["agent_source_digest"]) == 64
    assert state["version"] == 2
    assert state["fake_container"] == spec.fake_container_name
    assert state["network"] == spec.network_name
    assert state["database_path"] == str(root / "data" / "messages.db")

    generated = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            root / ".env",
            personalization / "pynchy.toml",
            personalization / "litellm.yaml",
        )
    )
    assert "paid-provider-secret" not in generated
    assert "channel-secret" not in generated
    assert "OPENAI_API_KEY" not in generated
    assert "SLACK_BOT_TOKEN" not in generated


def test_setup_rebuilds_the_agent_image_identity_when_source_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _runtime_root(tmp_path, "initial")
    _setup_without_starting_services(monkeypatch)
    initial = harness.setup(_spec(root))

    changed_root = _runtime_root(tmp_path, "changed")
    source = (
        changed_root
        / "src"
        / "pynchy"
        / "agent"
        / "agent_runner"
        / "src"
        / "agent_runner"
        / "main.py"
    )
    source.write_text("print('changed agent')\n")
    changed = harness.setup(_spec(changed_root))

    assert changed["agent_source_digest"] != initial["agent_source_digest"]
    assert changed["agent_image"] != initial["agent_image"]


def test_setup_agent_identity_ignores_local_python_bytecode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _runtime_root(tmp_path, "source")
    _setup_without_starting_services(monkeypatch)
    initial = harness.setup(_spec(root))
    bytecode_root = _runtime_root(tmp_path, "bytecode")
    bytecode = (
        bytecode_root
        / "src"
        / "pynchy"
        / "agent"
        / "agent_runner"
        / "src"
        / "agent_runner"
        / "__pycache__"
        / "main.cpython-313.pyc"
    )
    bytecode.parent.mkdir()
    bytecode.write_bytes(b"transient bytecode")
    with_bytecode = harness.setup(_spec(bytecode_root))

    assert with_bytecode["agent_source_digest"] == initial["agent_source_digest"]


def test_fresh_setup_removes_only_runtime_database_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _runtime_root(tmp_path)
    data_dir = root / "data"
    data_dir.mkdir()
    for name in ("messages.db", "memories.db", "temporal.db"):
        for suffix in ("", "-shm", "-wal"):
            data_dir.joinpath(f"{name}{suffix}").write_text("stale")
    data_dir.joinpath("keep.txt").write_text("unrelated")
    _setup_without_starting_services(monkeypatch)

    harness.setup(_spec(root))

    assert not any(
        data_dir.joinpath(f"{name}{suffix}").exists()
        for name in ("messages.db", "memories.db", "temporal.db")
        for suffix in ("", "-shm", "-wal")
    )
    assert data_dir.joinpath("keep.txt").read_text() == "unrelated"


def test_restart_preserves_runtime_database_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _runtime_root(tmp_path)
    spec = _spec(root)
    database = root / "data" / "messages.db"
    database.parent.mkdir()
    database.write_text("preserve")
    _write_runtime_state(
        spec,
        {
            "namespace": spec.namespace,
            "server_port": spec.server_port,
            "gateway_port": spec.gateway_port,
            "temporal_port": spec.temporal_port,
        },
    )
    monkeypatch.setattr(harness, "stop", lambda _root: spec.state_path.unlink())
    _setup_without_starting_services(monkeypatch)

    harness.restart(root, argparse.Namespace())

    assert database.read_text() == "preserve"


def test_setup_builds_the_runtime_agent_from_pinned_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_runtime_root(tmp_path))
    ensured_images: list[tuple[str, str]] = []
    docker_commands: list[list[str]] = []

    def ensure_image(docker: str, candidate: str) -> None:
        ensured_images.append((docker, candidate))

    def run_docker(docker: str, *args: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        command = [docker, *args]
        docker_commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="")

    class FakeProcess:
        pid = 1234

        def poll(self) -> None:
            return None

    monkeypatch.setattr("scripts.runtime_harness._initialize_runtime_data", lambda _spec: None)
    monkeypatch.setattr("scripts.runtime_harness._start_fake_openai", lambda *_: None)
    monkeypatch.setattr("scripts.runtime_harness._executable", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr("scripts.runtime_harness._ensure_docker_image", ensure_image)
    monkeypatch.setattr("scripts.runtime_harness._run_docker", run_docker)
    monkeypatch.setattr(
        "scripts.runtime_harness._start_process", lambda *_args, **_kwargs: FakeProcess()
    )
    monkeypatch.setattr("scripts.runtime_harness._wait_for_port", lambda *_args: None)
    monkeypatch.setattr("scripts.runtime_harness._wait_for_runtime", lambda *_args: None)

    state = harness.setup(spec)

    assert [docker for docker, _image in ensured_images] == ["/usr/bin/docker"] * 2
    assert [image.split("@sha256:", maxsplit=1)[0] for _docker, image in ensured_images] == [
        "python:3.13.12-slim-bookworm",
        "ghcr.io/astral-sh/uv:0.11.14",
    ]
    assert all("@sha256:" in image for _docker, image in ensured_images)
    assert docker_commands == [
        [
            "/usr/bin/docker",
            "build",
            "--pull=false",
            "--tag",
            state["agent_image"],
            "--label",
            f"io.pynchy.runtime-agent-source-sha256={state['agent_source_digest']}",
            "--file",
            str(spec.root / "src/pynchy/agent/runtime.Dockerfile"),
            str(spec.root / "src/pynchy/agent"),
        ]
    ]


def test_setup_failure_archives_bounded_logs_before_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_runtime_root(tmp_path))
    spec.log_dir.mkdir(parents=True)
    spec.log_dir.joinpath("pynchy.general.log").write_text("startup failed\n")
    control_root = tmp_path / "control"
    control_root.mkdir()
    archive_root = (
        control_root
        / ".new-feature"
        / "diagnostics"
        / "runtime-setup-failures"
        / "diagnostic-feature"
    )
    expected_archive_limit = 5
    for index in range(expected_archive_limit):
        old_archive = archive_root / f"20260724T00000{index}000000Z"
        old_archive.mkdir(parents=True)
        old_archive.joinpath("old.log").write_text("old\n")

    monkeypatch.setenv("NEW_FEATURE_REPO_ROOT", str(control_root))
    monkeypatch.setenv("NEW_FEATURE_SLUG", "diagnostic-feature")
    monkeypatch.setattr(harness, "_write_runtime_config", lambda _spec: {})
    monkeypatch.setattr(harness, "_write_state", lambda *_args: None)
    monkeypatch.setattr(
        harness,
        "_initialize_runtime_data",
        lambda _spec: (_ for _ in ()).throw(RuntimeError("startup failed")),
    )
    stops: list[Path] = []

    def record_stop(root: Path) -> None:
        stops.append(root)

    monkeypatch.setattr(harness, "stop", record_stop)

    with pytest.raises(RuntimeError, match="startup failed") as raised:
        harness.setup(spec)

    archives = sorted(path for path in archive_root.iterdir() if path.is_dir())
    assert len(archives) == expected_archive_limit
    assert archives[0].name != "20260724T000000000000Z"
    assert archives[-1].joinpath("pynchy.general.log").read_text() == "startup failed\n"
    assert raised.value.__notes__ == [
        f"Runtime setup logs preserved at {archives[-1]}",
    ]
    assert stops == [spec.root]


@pytest.mark.parametrize(
    ("relative_path", "contents"),
    [
        (".env", "OPENAI_API_KEY=not-owned-by-harness\n"),
        ("data/personalization/pynchy.toml", "[server]\nport = 8484\n"),
        ("data/personalization/litellm.yaml", "model_list: []\n"),
    ],
)
def test_runtime_harness_refuses_to_overwrite_unmanaged_runtime_files(
    tmp_path: Path, relative_path: str, contents: str
) -> None:
    root = _runtime_root(tmp_path, relative_path.replace("/", "_").replace(".", "_"))
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(RuntimeError, match="Refusing to replace"):
        harness.setup(_spec(root))


def test_runtime_status_redacts_the_ephemeral_gateway_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = _spec(_runtime_root(tmp_path))
    _write_runtime_state(spec, {"gateway_key": "sk-runtime-key", "namespace": spec.namespace})
    monkeypatch.setattr(sys, "argv", ["runtime_harness.py", "--root", str(spec.root), "status"])

    harness.main()

    output = capsys.readouterr().out
    assert "sk-runtime-key" not in output
    assert '"gateway_key": "<redacted>"' in output


def test_exec_runs_a_command_against_the_existing_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _runtime_root(tmp_path)
    spec = _spec(root)
    state = {
        "namespace": spec.namespace,
        "server_port": spec.server_port,
        "gateway_port": spec.gateway_port,
        "temporal_port": spec.temporal_port,
        "gateway_key": "sk-runtime-key",
        "pynchy_pid": 1234,
        "temporal_pid": 5678,
        "pynchy_marker": _process_marker("pynchy"),
        "temporal_marker": _process_marker("temporal"),
    }
    _write_runtime_state(spec, state)
    captured: dict[str, object] = {}

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 7)

    monkeypatch.setattr(harness, "_process_has_marker", lambda _pid, _marker: True)
    monkeypatch.setattr(harness.subprocess, "run", run)

    assert harness.execute(root, ["pytest", "-m", "runtime"]) == 7
    assert captured["command"] == ["pytest", "-m", "runtime"]
    assert captured["cwd"] == root
    environment = captured["env"]
    assert isinstance(environment, dict)
    assert environment["PYNCHY_RUNTIME_STATE"] == str(spec.state_path)


def test_exec_requires_an_existing_runtime(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="Runtime is not running"):
        harness.execute(_runtime_root(tmp_path), ["pytest"])


def test_exec_rejects_dead_diagnostic_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _runtime_root(tmp_path)
    spec = _spec(root)
    _write_runtime_state(
        spec,
        {
            "namespace": spec.namespace,
            "server_port": spec.server_port,
            "gateway_port": spec.gateway_port,
            "temporal_port": spec.temporal_port,
            "gateway_key": "sk-runtime-key",
        },
    )
    monkeypatch.setattr(
        harness.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("exec must not run against stopped services"),
    )

    with pytest.raises(RuntimeError, match="diagnostic only"):
        harness.execute(root, ["pytest"])


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("service", "status", "starting"),
        ("gateway", "litellm_container", "exited"),
        ("gateway", "postgres_container", "exited"),
        ("gateway", "ready", False),
        ("gateway", "database", "disconnected"),
        ("temporal", "cluster_healthy", False),
        ("temporal", "worker_running", False),
    ],
)
def test_runtime_readiness_requires_every_critical_subsystem(
    section: str, key: str, value: object
) -> None:
    status = {
        "service": {"status": "ok"},
        "gateway": {
            "litellm_container": "running",
            "postgres_container": "running",
            "ready": True,
            "database": "connected",
        },
        "temporal": {"cluster_healthy": True, "worker_running": True},
    }
    assert harness.is_runtime_ready(status)

    status[section][key] = value

    assert not harness.is_runtime_ready(status)


def test_setup_starts_the_deterministic_openai_sidecar_on_its_private_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _runtime_root(tmp_path)
    server_path = root / "scripts" / "deterministic_openai_server.py"
    server_path.parent.mkdir()
    server_path.touch()
    spec = _spec(root)
    calls: list[list[str]] = []
    network_calls: list[tuple[str, str]] = []
    image_calls: list[tuple[str, str]] = []

    def run_docker(docker: str, *args: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        command = [docker, *args]
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="")

    class FakeProcess:
        pid = 1234

        def poll(self) -> None:
            return None

    monkeypatch.setattr("scripts.runtime_harness._initialize_runtime_data", lambda _spec: None)
    monkeypatch.setattr("scripts.runtime_harness._executable", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(
        "scripts.runtime_harness._ensure_docker_network",
        lambda docker, network: network_calls.append((docker, network)),
    )
    monkeypatch.setattr(
        "scripts.runtime_harness._ensure_docker_image",
        lambda docker, image: image_calls.append((docker, image)),
    )
    monkeypatch.setattr("scripts.runtime_harness._run_docker", run_docker)
    monkeypatch.setattr("scripts.runtime_harness._wait_for_fake_openai", lambda *_args: None)
    monkeypatch.setattr("scripts.runtime_harness._ensure_runtime_agent_image", lambda *_: None)
    monkeypatch.setattr(
        "scripts.runtime_harness._start_process", lambda *_args, **_kwargs: FakeProcess()
    )
    monkeypatch.setattr("scripts.runtime_harness._wait_for_port", lambda *_args: None)
    monkeypatch.setattr("scripts.runtime_harness._wait_for_runtime", lambda *_args: None)

    state = harness.setup(spec)

    assert network_calls == [("/usr/bin/docker", spec.network_name)]
    assert len(image_calls) == 1
    assert image_calls[0][0] == "/usr/bin/docker"
    litellm_image = image_calls[0][1]
    assert litellm_image.startswith("ghcr.io/berriai/litellm@sha256:")
    assert calls == [
        ["/usr/bin/docker", "rm", "-f", spec.fake_container_name],
        [
            "/usr/bin/docker",
            "run",
            "-d",
            "--init",
            "--name",
            spec.fake_container_name,
            "--network",
            spec.network_name,
            "--restart",
            "no",
            "-v",
            f"{server_path}:/runtime/deterministic_openai_server.py:ro",
            "-e",
            "PYNCHY_DETERMINISTIC_RESPONSE=Pynchy deterministic response.",
            "--entrypoint",
            "python",
            litellm_image,
            "/runtime/deterministic_openai_server.py",
            "--port",
            "8080",
        ],
    ]
    assert state["fake_container"] == spec.fake_container_name


def _record_docker_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def run_docker(docker: str, *args: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        command = [docker, *args]
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(harness.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr("scripts.runtime_harness._run_docker", run_docker)
    return calls


def test_stop_removes_only_exact_owned_runtime_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_runtime_root(tmp_path))
    _write_runtime_state(spec, {"version": 2, "namespace": spec.namespace})
    calls = _record_docker_calls(monkeypatch)

    harness.stop(spec.root)

    assert calls == [
        ["/usr/bin/docker", "rm", "-f", "pynchy-runtime-test-pynchy"],
        ["/usr/bin/docker", "rm", "-f", "pynchy-runtime-test-litellm"],
        ["/usr/bin/docker", "rm", "-f", "pynchy-runtime-test-litellm-db"],
        ["/usr/bin/docker", "rm", "-f", "pynchy-runtime-test-deterministic-openai"],
        ["/usr/bin/docker", "network", "rm", "pynchy-runtime-test-litellm-net"],
        ["/usr/bin/docker", "volume", "rm", "pynchy-runtime-test-litellm-db-data"],
    ]


def test_stop_ignores_unsafe_runtime_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_runtime_root(tmp_path))
    _write_runtime_state(spec, {"version": 2, "namespace": "pynchy/../../production"})
    monkeypatch.setattr(harness.shutil, "which", lambda _name: pytest.fail("docker must not run"))

    harness.stop(spec.root)


def test_stop_does_not_touch_a_longer_prefix_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_runtime_root(tmp_path))
    _write_runtime_state(spec, {"version": 2, "namespace": "pynchy-runtime"})
    calls = _record_docker_calls(monkeypatch)

    harness.stop(spec.root)

    foreign_namespace = "pynchy-runtime-secondary"
    assert all(foreign_namespace not in argument for call in calls for argument in call)


def test_stop_removes_the_interactive_agent_before_tearing_down_the_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_runtime_root(tmp_path))
    _write_runtime_state(
        spec,
        {
            "version": 2,
            "namespace": spec.namespace,
            "pynchy_pid": 1234,
            "pynchy_marker": _process_marker("pynchy"),
            "temporal_pid": 5678,
            "temporal_marker": _process_marker("temporal"),
        },
    )
    calls = _record_docker_calls(monkeypatch)
    monkeypatch.setattr("scripts.runtime_harness._process_has_marker", lambda *_: True)
    monkeypatch.setattr("scripts.runtime_harness._process_group_has_live_member", lambda _: False)
    monkeypatch.setattr(harness.os, "killpg", lambda *_: None)

    harness.stop(spec.root)

    assert calls[0] == ["/usr/bin/docker", "rm", "-f", f"{spec.namespace}-pynchy"]
    assert ["/usr/bin/docker", "network", "rm", f"{spec.namespace}-litellm-net"] in calls


def test_stop_refuses_legacy_state_with_unverified_host_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_runtime_root(tmp_path))
    legacy_state = {
        "version": 1,
        "namespace": spec.namespace,
        "pynchy_pid": 1234,
        "temporal_pid": 5678,
    }
    _write_runtime_state(spec, legacy_state)
    monkeypatch.setattr(
        "scripts.runtime_harness._stop_pid", lambda *_args: pytest.fail("must not signal")
    )
    monkeypatch.setattr(
        "scripts.runtime_harness._remove_runtime_resources",
        lambda *_args: pytest.fail("must not remove resources"),
    )

    with pytest.raises(RuntimeError, match="older harness"):
        harness.stop(spec.root)

    assert json.loads(spec.state_path.read_text()) == legacy_state


def test_stop_removes_only_its_owned_runtime_agent_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_runtime_root(tmp_path))
    image = "pynchy-runtime-agent:pynchy-runtime-test-0123456789abcdef"
    _write_runtime_state(spec, {"version": 2, "namespace": spec.namespace, "agent_image": image})
    calls = _record_docker_calls(monkeypatch)

    harness.stop(spec.root)

    assert calls[-1] == ["/usr/bin/docker", "image", "rm", image]


def test_stop_keeps_a_foreign_runtime_agent_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_runtime_root(tmp_path))
    _write_runtime_state(
        spec,
        {
            "version": 2,
            "namespace": spec.namespace,
            "agent_image": "pynchy-runtime-agent:unrelated-runtime-0123456789abcdef",
        },
    )
    calls = _record_docker_calls(monkeypatch)

    harness.stop(spec.root)

    assert all(call[1:3] != ["image", "rm"] for call in calls)


def test_stop_clears_stale_pids_when_preserving_failure_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_runtime_root(tmp_path))
    pynchy_marker = _process_marker("pynchy")
    temporal_marker = _process_marker("temporal")
    image = "pynchy-runtime-agent:pynchy-runtime-test-0123456789abcdef"
    _write_runtime_state(
        spec,
        {
            "version": 2,
            "namespace": spec.namespace,
            "pynchy_pid": 1234,
            "temporal_pid": 5678,
            "pynchy_marker": pynchy_marker,
            "temporal_marker": temporal_marker,
            "agent_image": image,
        },
    )
    stopped_pids: list[tuple[object, object, bool]] = []
    removed_resources: list[object] = []
    removed_images: list[tuple[object, object]] = []
    monkeypatch.setattr(
        "scripts.runtime_harness._stop_pid",
        lambda pid, marker, *, after_term=None: stopped_pids.append(
            (pid, marker, after_term is not None)
        ),
    )
    monkeypatch.setattr(
        "scripts.runtime_harness._remove_runtime_resources", removed_resources.append
    )
    monkeypatch.setattr(
        "scripts.runtime_harness._remove_runtime_agent_image",
        lambda namespace, image: removed_images.append((namespace, image)),
    )

    harness.stop(spec.root, preserve_state=True)

    preserved_state = json.loads(spec.state_path.read_text())
    assert "pynchy_pid" not in preserved_state
    assert "temporal_pid" not in preserved_state
    assert "pynchy_marker" not in preserved_state
    assert "temporal_marker" not in preserved_state
    assert stopped_pids == [
        (1234, pynchy_marker, True),
        (5678, temporal_marker, False),
    ]
    assert removed_resources == [spec.namespace]
    assert removed_images == [(spec.namespace, image)]


def test_stop_reaps_a_terminated_marked_harness_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_runtime_root(tmp_path))
    marker = _process_marker("pynchy")
    _write_runtime_state(
        spec,
        {
            "version": 2,
            "namespace": "pynchy/../../production",
            "pynchy_pid": 1234,
            "pynchy_marker": marker,
        },
    )
    process_group_signals: list[tuple[int, int]] = []

    monkeypatch.setattr("scripts.runtime_harness._process_has_marker", lambda *_: True)
    monkeypatch.setattr("scripts.runtime_harness._process_group_has_live_member", lambda _: False)
    monkeypatch.setattr(
        harness.os,
        "killpg",
        lambda pid, signal: process_group_signals.append((pid, signal)),
    )
    monkeypatch.setattr(harness.os, "waitpid", lambda pid, _flags: (pid, 0))

    harness.stop(spec.root)

    assert process_group_signals == [(1234, harness.signal.SIGTERM)]


def test_stop_never_signals_a_reused_or_unmarked_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_runtime_root(tmp_path))
    marker = _process_marker("pynchy")
    _write_runtime_state(
        spec,
        {
            "version": 2,
            "namespace": "pynchy/../../production",
            "pynchy_pid": 1234,
            "pynchy_marker": marker,
        },
    )
    checks: list[tuple[int, str]] = []

    def process_has_marker(pid: int, value: str) -> bool:
        checks.append((pid, value))
        return False

    monkeypatch.setattr("scripts.runtime_harness._process_has_marker", process_has_marker)
    monkeypatch.setattr(harness.os, "killpg", lambda *_args: pytest.fail("must not signal"))

    harness.stop(spec.root)

    assert checks == [(1234, marker)]


def test_stop_removes_the_interactive_agent_before_waiting_for_its_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_runtime_root(tmp_path))
    marker = _process_marker("pynchy")
    events: list[str] = []
    _write_runtime_state(
        spec,
        {
            "version": 2,
            "namespace": spec.namespace,
            "pynchy_pid": 1234,
            "pynchy_marker": marker,
        },
    )

    monkeypatch.setattr("scripts.runtime_harness._process_has_marker", lambda *_: True)
    monkeypatch.setattr("scripts.runtime_harness._process_group_has_live_member", lambda _: False)
    monkeypatch.setattr(
        harness.os,
        "killpg",
        lambda _pid, _signal: events.append("term"),
    )
    monkeypatch.setattr(
        "scripts.runtime_harness._remove_runtime_agent_container",
        lambda _: events.append("cleanup"),
    )
    monkeypatch.setattr("scripts.runtime_harness._remove_runtime_resources", lambda _: None)
    monkeypatch.setattr("scripts.runtime_harness._remove_runtime_agent_image", lambda *_: None)

    harness.stop(spec.root)

    assert events == ["term", "cleanup"]


def test_stop_treats_a_process_group_with_only_a_zombie_leader_as_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_runtime_root(tmp_path))
    marker = _process_marker("pynchy")
    _write_runtime_state(
        spec,
        {
            "version": 2,
            "namespace": "pynchy/../../production",
            "pynchy_pid": 1234,
            "pynchy_marker": marker,
        },
    )
    calls: list[list[str]] = []
    signals: list[int] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[:2] == ["/usr/bin/ps", "-ww"]:
            return subprocess.CompletedProcess(command, 0, stdout=f"/bin/sh -c ... {marker} ...")
        return subprocess.CompletedProcess(command, 0, stdout="1234 Zs\n")

    monkeypatch.setattr(harness.os, "killpg", lambda _pid, signal: signals.append(signal))
    monkeypatch.setattr(harness.shutil, "which", lambda _name: "/usr/bin/ps")
    monkeypatch.setattr(harness.subprocess, "run", run)

    harness.stop(spec.root)

    assert calls == [
        ["/usr/bin/ps", "-ww", "-p", "1234", "-o", "command="],
        ["/usr/bin/ps", "-eo", "pgid=,stat="],
    ]
    assert signals == [harness.signal.SIGTERM, 0]


def test_stop_escalates_for_a_term_ignoring_group_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead shell leader must not hide a still-running child from teardown."""
    marker = _process_marker("pynchy")
    root = _runtime_root(tmp_path)
    spec = _spec(root)
    process = subprocess.Popen(  # noqa: S603 - fixed local test supervisor command.
        [
            "/bin/sh",
            "-c",
            '"$@" &\nchild=$!\nwait "$child"\n',
            marker,
            "/bin/sh",
            "-c",
            "trap '' TERM; sleep 30 & wait",
        ],
        cwd=root,
        env={"PATH": "/usr/bin:/bin"},
        start_new_session=True,
    )
    _write_runtime_state(
        spec,
        {
            "version": 2,
            "namespace": "pynchy/../../production",
            "pynchy_pid": process.pid,
            "pynchy_marker": marker,
        },
    )
    monkeypatch.setattr("scripts.runtime_harness._STOP_TIMEOUT_SECONDS", 0.5)

    try:
        harness.stop(root)
        _wait_for_process_group_to_disappear(process.pid)
    finally:
        with contextlib.suppress(ProcessLookupError):
            harness.os.killpg(process.pid, harness.signal.SIGKILL)
        with contextlib.suppress(ChildProcessError):
            harness.os.waitpid(process.pid, 0)


def _wait_for_process_group_to_disappear(process_group_id: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            harness.os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    pytest.fail("SIGKILL did not remove the harness-owned process group")


def test_setup_supervises_runtime_children_with_unique_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_runtime_root(tmp_path))
    captured: list[dict[str, object]] = []

    class FakeProcess:
        pid = 1234

        def poll(self) -> None:
            return None

    def popen(command: list[str], **kwargs: object) -> FakeProcess:
        captured.append({"command": command, **kwargs})
        return FakeProcess()

    monkeypatch.setattr("scripts.runtime_harness._initialize_runtime_data", lambda _: None)
    monkeypatch.setattr("scripts.runtime_harness._start_fake_openai", lambda *_: None)
    monkeypatch.setattr("scripts.runtime_harness._ensure_runtime_agent_image", lambda *_: None)
    monkeypatch.setattr("scripts.runtime_harness._wait_for_port", lambda *_: None)
    monkeypatch.setattr("scripts.runtime_harness._wait_for_runtime", lambda *_: None)
    monkeypatch.setattr("scripts.runtime_harness._executable", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(harness.subprocess, "Popen", popen)

    harness.setup(spec)

    assert len(captured) == 2
    for process in captured:
        command = process["command"]
        assert isinstance(command, list)
        assert command[:3] == ["/usr/bin/sh", "-c", '"$@" &\nchild=$!\nwait "$child"\n']
        assert str(command[3]).startswith("pynchy-runtime-")
        assert process["start_new_session"] is True


def test_execute_checks_the_wide_process_command_for_its_owned_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_runtime_root(tmp_path))
    marker = _process_marker("pynchy")
    commands: list[list[str]] = []
    temporal_marker = _process_marker("temporal")
    _write_runtime_state(
        spec,
        {
            "version": 2,
            "namespace": spec.namespace,
            "server_port": spec.server_port,
            "gateway_port": spec.gateway_port,
            "temporal_port": spec.temporal_port,
            "gateway_key": "sk-runtime-key",
            "pynchy_pid": 1234,
            "pynchy_marker": marker,
            "temporal_pid": 5678,
            "temporal_marker": temporal_marker,
        },
    )

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[:2] == ["/usr/bin/ps", "-ww"]:
            value = marker if command[3] == "1234" else temporal_marker
            return subprocess.CompletedProcess(command, 0, stdout=f"/bin/sh -c ... {value} ...")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(harness.shutil, "which", lambda _name: "/usr/bin/ps")
    monkeypatch.setattr(harness.subprocess, "run", run)

    assert harness.execute(spec.root, ["pytest"]) == 0
    assert commands[:2] == [
        ["/usr/bin/ps", "-ww", "-p", "1234", "-o", "command="],
        ["/usr/bin/ps", "-ww", "-p", "5678", "-o", "command="],
    ]


def test_run_exposes_runtime_state_to_command_and_cleans_up_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_runtime_root(tmp_path))
    state: dict[str, object] = {"gateway_key": "sk-runtime-key"}
    calls: list[tuple[list[str], Path, dict[str, str]]] = []
    stops: list[tuple[Path, bool]] = []

    def run_command(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        assert check is False
        calls.append((command, cwd, env))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(harness, "setup", lambda _spec: state)
    monkeypatch.setattr(harness.subprocess, "run", run_command)
    monkeypatch.setattr(
        harness,
        "stop",
        lambda root, *, preserve_state=False: stops.append((root, preserve_state)),
    )

    assert harness.run(spec, ["uv", "run", "pytest", "-m", "runtime"]) == 0

    assert calls[0][0] == ["uv", "run", "pytest", "-m", "runtime"]
    assert calls[0][1] == spec.root
    assert calls[0][2]["PYNCHY_RUNTIME_STATE"] == str(spec.state_path)
    assert calls[0][2]["PYNCHY_RUNTIME_URL"] == "http://127.0.0.1:18484"
    assert calls[0][2]["PYNCHY_RUNTIME_GATEWAY_URL"] == "http://127.0.0.1:14010"
    assert stops == [(spec.root, False)]


def test_run_stops_live_resources_but_preserves_diagnostics_on_command_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_runtime_root(tmp_path))
    state: dict[str, object] = {"gateway_key": "sk-runtime-key"}
    stops: list[tuple[Path, bool]] = []

    monkeypatch.setattr(harness, "setup", lambda _spec: state)
    monkeypatch.setattr(
        harness.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 1),
    )
    monkeypatch.setattr(
        harness,
        "stop",
        lambda root, *, preserve_state=False: stops.append((root, preserve_state)),
    )

    assert harness.run(spec, ["pytest", "-m", "runtime"]) == 1

    assert stops == [(spec.root, True)]
