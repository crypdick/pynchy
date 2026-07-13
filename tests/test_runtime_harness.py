"""Contract tests for the hermetic deterministic runtime harness."""

from __future__ import annotations

import argparse
import json
import stat
import subprocess  # noqa: S404, RUF100 - test helpers record mocked subprocess results only.
from typing import TYPE_CHECKING

import pytest
from scripts import runtime_harness as harness

if TYPE_CHECKING:
    from pathlib import Path


def _runtime_root(tmp_path: Path, name: str = "runtime") -> Path:
    root = tmp_path / name
    root.mkdir()
    root.joinpath("pyproject.toml").write_text("[project]\nname = 'pynchy-runtime-test'\n")
    return root


def _spec(root: Path) -> harness.RuntimeSpec:
    return harness.RuntimeSpec(
        root=root,
        namespace="pynchy-runtime-test",
        server_port=18484,
        gateway_port=14010,
        temporal_port=17233,
    )


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
    assert state["model"] == "pynchy-deterministic"
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


def test_remove_runtime_resources_removes_only_owned_namespace_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def run_docker(docker: str, *args: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        command = [docker, *args]
        calls.append(command)
        stdout = (
            "first-owned-container\nsecond-owned-container\n" if args[:2] == ("ps", "-aq") else ""
        )
        return subprocess.CompletedProcess(command, 0, stdout=stdout)

    monkeypatch.setattr(harness.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(harness, "_run_docker", run_docker)

    harness._remove_runtime_resources("pynchy-runtime-test")

    assert calls == [
        [
            "/usr/bin/docker",
            "ps",
            "-aq",
            "--filter",
            r"name=^/pynchy\-runtime\-test-",
        ],
        ["/usr/bin/docker", "rm", "-f", "first-owned-container"],
        ["/usr/bin/docker", "rm", "-f", "second-owned-container"],
        ["/usr/bin/docker", "network", "rm", "pynchy-runtime-test-litellm-net"],
        ["/usr/bin/docker", "volume", "rm", "pynchy-runtime-test-litellm-db-data"],
    ]


def test_remove_runtime_resources_ignores_unsafe_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harness.shutil, "which", lambda _name: pytest.fail("docker must not run"))

    harness._remove_runtime_resources("pynchy/../../production")


def test_remove_runtime_resources_escapes_dotted_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def run_docker(docker: str, *args: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        command = [docker, *args]
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(harness.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(harness, "_run_docker", run_docker)

    harness._remove_runtime_resources("pynchy-runtime.test")

    assert calls[0][-1] == r"name=^/pynchy\-runtime\.test-"


def test_stop_pid_reaps_a_terminated_harness_child(monkeypatch: pytest.MonkeyPatch) -> None:
    process_group_signals: list[tuple[int, int]] = []

    monkeypatch.setattr(
        harness.os,
        "killpg",
        lambda pid, signal: process_group_signals.append((pid, signal)),
    )
    monkeypatch.setattr(harness.os, "waitpid", lambda pid, _flags: (pid, 0))
    monkeypatch.setattr(harness.os, "kill", lambda *_args: pytest.fail("signal probe is unused"))

    harness._stop_pid(1234)

    assert process_group_signals == [(1234, harness.signal.SIGTERM)]


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
