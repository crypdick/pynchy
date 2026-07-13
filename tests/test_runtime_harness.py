"""Contract tests for the hermetic deterministic runtime harness."""

from __future__ import annotations

import argparse
import contextlib
import json
import stat
import subprocess  # noqa: S404, RUF100 - test helpers record mocked subprocess results only.
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


def test_generated_runtime_config_is_deterministic_and_credential_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _runtime_root(tmp_path)
    spec = _spec(root)
    monkeypatch.setenv("OPENAI_API_KEY", "paid-provider-secret")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "channel-secret")

    state = harness._write_runtime_config(spec)

    dotenv = _dotenv_values(root / ".env")
    litellm_config = root.joinpath("litellm_config.yaml").read_text(encoding="utf-8")
    config = root.joinpath("config.toml").read_text(encoding="utf-8")

    assert set(dotenv) == {
        "GATEWAY__MASTER_KEY",
        "PYNCHY_DISABLE_SERVICE_INSTALL",
        "PYNCHY_RUNTIME_HARNESS",
        "PYNCHY_RUNTIME_NAMESPACE",
    }
    assert dotenv["GATEWAY__MASTER_KEY"].startswith("sk-")
    assert dotenv["PYNCHY_RUNTIME_HARNESS"] == "1"
    assert stat.S_IMODE((root / ".env").stat().st_mode) == 0o600
    assert "model_name: pynchy-deterministic" in litellm_config
    assert f"api_base: http://{spec.fake_container_name}:8080/v1" in litellm_config
    assert "api_key: deterministic" in litellm_config
    assert "public_routes" not in litellm_config
    assert 'default_core = "openai"' in config
    assert 'model = "pynchy-deterministic"' in config
    assert f'image = "pynchy-runtime-agent:{spec.namespace}-' in config
    assert state["model"] == "pynchy-deterministic"
    assert str(state["agent_image"]).startswith(f"pynchy-runtime-agent:{spec.namespace}-")
    assert state["agent_source_digest"] == harness._runtime_agent_source_digest(spec)
    assert state["version"] == harness._STATE_VERSION
    assert state["fake_container"] == spec.fake_container_name
    assert state["network"] == spec.network_name

    generated = "\n".join(
        (root / filename).read_text(encoding="utf-8")
        for filename in (
            ".env",
            "config.toml",
            "litellm_config.yaml",
        )
    )
    assert "paid-provider-secret" not in generated
    assert "channel-secret" not in generated
    assert "OPENAI_API_KEY" not in generated
    assert "SLACK_BOT_TOKEN" not in generated


def test_runtime_agent_source_digest_changes_when_its_source_changes(tmp_path: Path) -> None:
    root = _runtime_root(tmp_path)
    spec = _spec(root)

    initial_digest = harness._runtime_agent_source_digest(spec)
    source = root / "src" / "pynchy" / "agent" / "agent_runner" / "src" / "agent_runner" / "main.py"
    source.write_text("print('changed agent')\n")

    assert harness._runtime_agent_source_digest(spec) != initial_digest
    assert harness._runtime_agent_image(spec).endswith(
        harness._runtime_agent_source_digest(spec)[: harness._RUNTIME_AGENT_IMAGE_DIGEST_LENGTH]
    )


def test_runtime_agent_source_digest_ignores_local_python_bytecode(tmp_path: Path) -> None:
    root = _runtime_root(tmp_path)
    spec = _spec(root)
    initial_digest = harness._runtime_agent_source_digest(spec)
    bytecode = (
        root
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

    assert harness._runtime_agent_source_digest(spec) == initial_digest


def test_runtime_agent_image_builds_from_pinned_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_runtime_root(tmp_path))
    image = harness._runtime_agent_image(spec)
    source_digest = harness._runtime_agent_source_digest(spec)
    ensured_images: list[tuple[str, str]] = []
    docker_commands: list[list[str]] = []

    def ensure_image(docker: str, candidate: str) -> None:
        ensured_images.append((docker, candidate))

    def run_docker(docker: str, *args: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        command = [docker, *args]
        docker_commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(harness, "_executable", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(harness, "_ensure_docker_image", ensure_image)
    monkeypatch.setattr(harness, "_run_docker", run_docker)

    harness._ensure_runtime_agent_image(spec, {"agent_image": image})

    assert ensured_images == [
        ("/usr/bin/docker", harness._RUNTIME_AGENT_BASE_IMAGE),
        ("/usr/bin/docker", harness._RUNTIME_AGENT_UV_IMAGE),
    ]
    assert docker_commands == [
        [
            "/usr/bin/docker",
            "build",
            "--pull=false",
            "--tag",
            image,
            "--label",
            f"{harness._RUNTIME_AGENT_SOURCE_LABEL}={source_digest}",
            "--file",
            str(spec.root / harness._RUNTIME_AGENT_DOCKERFILE),
            str(spec.root / harness._RUNTIME_AGENT_ROOT),
        ]
    ]


@pytest.mark.parametrize(
    ("filename", "contents"),
    [
        (".env", "OPENAI_API_KEY=not-owned-by-harness\n"),
        ("config.toml", "[server]\nport = 8484\n"),
        ("litellm_config.yaml", "model_list: []\n"),
    ],
)
def test_runtime_harness_refuses_to_overwrite_unmanaged_runtime_files(
    tmp_path: Path, filename: str, contents: str
) -> None:
    root = _runtime_root(tmp_path, filename.replace(".", "_"))
    root.joinpath(filename).write_text(contents, encoding="utf-8")

    with pytest.raises(RuntimeError, match="Refusing to replace"):
        harness._ensure_safe_runtime_root(_spec(root))


def test_runtime_environment_excludes_ambient_provider_and_channel_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_runtime_root(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "paid-provider-secret")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "channel-secret")
    monkeypatch.setenv("HOME", "/host/home")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/host/config")
    monkeypatch.setenv("PATH", "/test/bin")

    environment = harness._runtime_environment(spec, {"gateway_key": "sk-runtime-key"})

    assert environment["PATH"] == "/test/bin"
    assert environment["GATEWAY__MASTER_KEY"] == "sk-runtime-key"
    assert environment["PYNCHY_DISABLE_SERVICE_INSTALL"] == "1"
    assert environment["PYNCHY_RUNTIME_HARNESS"] == "1"
    assert environment["PYNCHY_RUNTIME_NAMESPACE"] == spec.namespace
    assert environment["HOME"] == str(spec.home_dir)
    assert environment["XDG_CONFIG_HOME"] == str(spec.home_dir / "config")
    assert environment["XDG_CONFIG_HOME"] != "/host/config"
    assert "OPENAI_API_KEY" not in environment
    assert "SLACK_BOT_TOKEN" not in environment


def test_runtime_status_redacts_the_ephemeral_gateway_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = _spec(_runtime_root(tmp_path))
    harness._write_state(spec, {"gateway_key": "sk-runtime-key", "namespace": spec.namespace})

    harness._run_command(argparse.Namespace(command="status"), spec.root)

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
    harness._write_state(spec, state)
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
    harness._write_state(
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
    assert harness._runtime_ready(status)

    status[section][key] = value

    assert not harness._runtime_ready(status)


def test_start_fake_openai_uses_private_network_and_fixed_sidecar_argv(
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
    state_writes: list[dict[str, object]] = []

    def run_docker(docker: str, *args: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        command = [docker, *args]
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(harness, "_executable", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(
        harness,
        "_ensure_docker_network",
        lambda docker, network: network_calls.append((docker, network)),
    )
    monkeypatch.setattr(
        harness,
        "_ensure_docker_image",
        lambda docker, image: image_calls.append((docker, image)),
    )
    monkeypatch.setattr(harness, "_run_docker", run_docker)
    monkeypatch.setattr(harness, "_wait_for_fake_openai", lambda *_args: None)
    monkeypatch.setattr(
        harness,
        "_write_state",
        lambda _spec, state: state_writes.append(dict(state)),
    )
    state: dict[str, object] = {}

    harness._start_fake_openai(spec, state)

    assert network_calls == [("/usr/bin/docker", spec.network_name)]
    assert image_calls == [("/usr/bin/docker", harness._LITELLM_IMAGE)]
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
            harness._LITELLM_IMAGE,
            "/runtime/deterministic_openai_server.py",
            "--port",
            "8080",
        ],
    ]
    assert state["fake_container"] == spec.fake_container_name
    assert state_writes == [state]


def test_remove_runtime_resources_removes_only_exact_owned_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run_docker(docker: str, *args: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        command = [docker, *args]
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(harness.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(harness, "_run_docker", run_docker)

    harness._remove_runtime_resources("pynchy-runtime-test")

    assert calls == [
        ["/usr/bin/docker", "rm", "-f", "pynchy-runtime-test-pynchy"],
        ["/usr/bin/docker", "rm", "-f", "pynchy-runtime-test-litellm"],
        ["/usr/bin/docker", "rm", "-f", "pynchy-runtime-test-litellm-db"],
        ["/usr/bin/docker", "rm", "-f", "pynchy-runtime-test-deterministic-openai"],
        ["/usr/bin/docker", "network", "rm", "pynchy-runtime-test-litellm-net"],
        ["/usr/bin/docker", "volume", "rm", "pynchy-runtime-test-litellm-db-data"],
    ]


def test_remove_runtime_resources_ignores_unsafe_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harness.shutil, "which", lambda _name: pytest.fail("docker must not run"))

    harness._remove_runtime_resources("pynchy/../../production")


def test_remove_runtime_resources_does_not_touch_a_longer_prefix_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run_docker(docker: str, *args: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        command = [docker, *args]
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(harness.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(harness, "_run_docker", run_docker)

    harness._remove_runtime_resources("pynchy-runtime")

    foreign_namespace = "pynchy-runtime-secondary"
    assert all(foreign_namespace not in argument for call in calls for argument in call)


def test_remove_runtime_agent_container_removes_only_the_interactive_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run_docker(docker: str, *args: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        command = [docker, *args]
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(harness.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(harness, "_run_docker", run_docker)

    harness._remove_runtime_agent_container("pynchy-runtime-test")

    assert calls == [["/usr/bin/docker", "rm", "-f", "pynchy-runtime-test-pynchy"]]


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
    harness._write_state(spec, legacy_state)
    monkeypatch.setattr(harness, "_stop_pid", lambda *_args: pytest.fail("must not signal"))
    monkeypatch.setattr(
        harness,
        "_remove_runtime_resources",
        lambda *_args: pytest.fail("must not remove resources"),
    )

    with pytest.raises(RuntimeError, match="older harness"):
        harness.stop(spec.root)

    assert harness._read_state(spec.root) == legacy_state


def test_remove_runtime_agent_image_removes_only_its_namespace_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run_docker(docker: str, *args: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        command = [docker, *args]
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(harness.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(harness, "_run_docker", run_docker)

    harness._remove_runtime_agent_image(
        "pynchy-runtime-test", "pynchy-runtime-agent:pynchy-runtime-test-0123456789abcdef"
    )

    assert calls == [
        [
            "/usr/bin/docker",
            "image",
            "rm",
            "pynchy-runtime-agent:pynchy-runtime-test-0123456789abcdef",
        ]
    ]


def test_remove_runtime_agent_image_ignores_a_foreign_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harness.shutil, "which", lambda _name: pytest.fail("docker must not run"))

    harness._remove_runtime_agent_image(
        "pynchy-runtime-test", "pynchy-runtime-agent:unrelated-runtime-0123456789abcdef"
    )


def test_stop_clears_stale_pids_when_preserving_failure_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(_runtime_root(tmp_path))
    pynchy_marker = _process_marker("pynchy")
    temporal_marker = _process_marker("temporal")
    harness._write_state(
        spec,
        {
            "version": harness._STATE_VERSION,
            "namespace": spec.namespace,
            "pynchy_pid": 1234,
            "temporal_pid": 5678,
            "pynchy_marker": pynchy_marker,
            "temporal_marker": temporal_marker,
            "agent_image": harness._runtime_agent_image(spec),
        },
    )
    stopped_pids: list[tuple[object, object, bool]] = []
    removed_resources: list[object] = []
    removed_images: list[tuple[object, object]] = []
    monkeypatch.setattr(
        harness,
        "_stop_pid",
        lambda pid, marker, *, after_term=None: stopped_pids.append(
            (pid, marker, after_term is not None)
        ),
    )
    monkeypatch.setattr(harness, "_remove_runtime_resources", removed_resources.append)
    monkeypatch.setattr(
        harness,
        "_remove_runtime_agent_image",
        lambda namespace, image: removed_images.append((namespace, image)),
    )

    harness.stop(spec.root, preserve_state=True)

    preserved_state = harness._read_state(spec.root)
    assert preserved_state is not None
    assert "pynchy_pid" not in preserved_state
    assert "temporal_pid" not in preserved_state
    assert "pynchy_marker" not in preserved_state
    assert "temporal_marker" not in preserved_state
    assert stopped_pids == [
        (1234, pynchy_marker, True),
        (5678, temporal_marker, False),
    ]
    assert removed_resources == [spec.namespace]
    assert removed_images == [(spec.namespace, harness._runtime_agent_image(spec))]


def test_stop_pid_reaps_a_terminated_marked_harness_child(monkeypatch: pytest.MonkeyPatch) -> None:
    process_group_signals: list[tuple[int, int]] = []
    marker = _process_marker("pynchy")

    monkeypatch.setattr(harness, "_process_has_marker", lambda _pid, _marker: True)
    monkeypatch.setattr(harness, "_process_group_has_live_member", lambda _pid: False)
    monkeypatch.setattr(
        harness.os,
        "killpg",
        lambda pid, signal: process_group_signals.append((pid, signal)),
    )
    monkeypatch.setattr(harness.os, "waitpid", lambda pid, _flags: (pid, 0))

    harness._stop_pid(1234, marker)

    assert process_group_signals == [(1234, harness.signal.SIGTERM)]


def test_stop_pid_never_signals_a_reused_or_unmarked_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    marker = _process_marker("pynchy")
    checks: list[tuple[int, str]] = []

    def process_has_marker(pid: int, value: str) -> bool:
        checks.append((pid, value))
        return False

    monkeypatch.setattr(harness, "_process_has_marker", process_has_marker)
    monkeypatch.setattr(harness.os, "killpg", lambda *_args: pytest.fail("must not signal"))

    harness._stop_pid(1234, marker)

    assert checks == [(1234, marker)]


def test_stop_pid_runs_its_post_term_cleanup_before_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = _process_marker("pynchy")
    events: list[str] = []

    monkeypatch.setattr(harness, "_process_has_marker", lambda _pid, _marker: True)
    monkeypatch.setattr(harness, "_process_group_has_live_member", lambda _pid: False)
    monkeypatch.setattr(
        harness.os,
        "killpg",
        lambda _pid, _signal: events.append("term"),
    )

    harness._stop_pid(1234, marker, after_term=lambda: events.append("cleanup"))

    assert events == ["term", "cleanup"]


def test_process_group_with_only_a_zombie_leader_is_not_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="1234 Zs\n")

    monkeypatch.setattr(harness.os, "killpg", lambda *_args: None)
    monkeypatch.setattr(harness.shutil, "which", lambda _name: "/usr/bin/ps")
    monkeypatch.setattr(harness.subprocess, "run", run)

    assert not harness._process_group_has_live_member(1234)
    assert calls == [["/usr/bin/ps", "-eo", "pgid=,stat="]]


def test_stop_pid_escalates_for_a_term_ignoring_group_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead shell leader must not hide a still-running child from teardown."""
    marker = _process_marker("pynchy")
    process = harness._start_process(
        ["/bin/sh", "-c", "trap '' TERM; sleep 30 & wait"],
        tmp_path / "term-ignoring.log",
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        process_marker=marker,
    )
    monkeypatch.setattr(harness, "_STOP_TIMEOUT_SECONDS", 0.5)

    try:
        harness._stop_pid(process.pid, marker)
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


def test_start_process_supervises_a_child_with_its_unique_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = _process_marker("temporal")
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 1234

    def popen(command: list[str], **kwargs: object) -> FakeProcess:
        captured["command"] = command
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(harness, "_executable", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(harness.subprocess, "Popen", popen)

    process = harness._start_process(
        ["/usr/bin/temporal", "server", "start-dev"],
        tmp_path / "temporal.log",
        cwd=tmp_path,
        env={"PATH": "/usr/bin"},
        process_marker=marker,
    )

    assert process.pid == 1234
    assert captured["command"] == [
        "/usr/bin/sh",
        "-c",
        harness._PROCESS_SUPERVISOR_SCRIPT,
        marker,
        "/usr/bin/temporal",
        "server",
        "start-dev",
    ]
    assert captured["start_new_session"] is True


def test_process_marker_checks_the_wide_process_command(monkeypatch: pytest.MonkeyPatch) -> None:
    marker = _process_marker("pynchy")
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=f"/bin/sh -c ... {marker} ...")

    monkeypatch.setattr(harness.shutil, "which", lambda _name: "/usr/bin/ps")
    monkeypatch.setattr(harness.subprocess, "run", run)

    assert harness._process_has_marker(1234, marker)
    assert commands == [["/usr/bin/ps", "-ww", "-p", "1234", "-o", "command="]]


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
