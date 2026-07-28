"""Contract tests for the hermetic deterministic runtime harness."""

from __future__ import annotations

import json
import subprocess  # noqa: S404 - test helpers record mocked subprocess results only.
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


def _record_docker_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def run_docker(docker: str, *args: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        command = [docker, *args]
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(harness.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr("scripts.runtime_harness._run_docker", run_docker)
    return calls


def _wait_for_process_group_to_disappear(process_group_id: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            harness.os.killpg(process_group_id, 0)
        except (PermissionError, ProcessLookupError):
            return
        time.sleep(0.05)
    pytest.fail("SIGKILL did not remove the harness-owned process group")
